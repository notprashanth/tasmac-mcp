#!/usr/bin/env python3
"""
TASMAC stock core. Transport agnostic, zero dependencies (stdlib only).

The tasmac.tn.gov.in site is a thin client over a public, unauthenticated JSON
API. There is no login and no age gate on the API itself, so a single HTTP POST
replaces the whole browser flow.

    POST /api/liquor/get-stockDetailsBy-shopNumber  {"i_ShopNumber": "4107"}
    POST /api/liquor/get-stockDetailsBy-DistrictId  {"p_districtId": 3}

Parameter names are load bearing and unguessable: the API 409s on shopNumber,
p_shopNumber and p_shop_number, and only accepts i_ShopNumber. Likewise
districtId is rejected in favour of p_districtId.

Only the shopNumber endpoint carries mrpPerBottle, so it is the one we use.

A local SQLite file caches the product catalogue, which is a 360KB call that
changes rarely. Nothing else is stored.

CLI:
    python3 tasmac_core.py 4107                     # everything in stock
    python3 tasmac_core.py 4107 --category wine     # just wine
    python3 tasmac_core.py 4107 --category wine --max-price 2500
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

API_ROOT = "https://dashboard-api.tasmace2e.in/api"
# The full-catalogue endpoint is the slow one and it degrades: measured under a
# second on 2026-08-08 morning, then 33 seconds the same afternoon. A 30 second
# ceiling turned that into an outright failure, so the default is generous and
# overridable rather than tight.
TIMEOUT = int(os.environ.get("TASMAC_TIMEOUT", "90"))
# No new attempt starts once this much time has passed. Deliberately shorter
# than a single TIMEOUT, which splits the two failure modes the right way: a
# fast failure (connection refused, an instant 502) still gets all three
# attempts inside a few seconds, while a gateway that sits for a minute before
# answering 504 is abandoned after one try instead of three. Retrying something
# that takes a minute to fail is how a lookup turns into a three minute hang.
DEADLINE = int(os.environ.get("TASMAC_DEADLINE", "45"))
IST = timezone(timedelta(hours=5, minutes=30))

# Area and pincode lookup needs a geocoder. TASMAC has no pincode anywhere in
# its API, so we resolve the place to a coordinate first, then ask TASMAC what
# is near it. Nominatim asks for a real User-Agent and one request per second.
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "tasmac-cli/1.1 (personal project)"

def _default_db() -> Path:
    """Where the cached product catalogue lives.

    Installed from PyPI, the module sits in site-packages, which must never be
    written to. So the default is a user data directory. A checkout that
    already has a history.db at its root keeps using it, so cloning and
    installing do not fight over the same file.
    """
    env = os.environ.get("TASMAC_DB")
    if env:
        return Path(env).expanduser()
    checkout = Path(__file__).resolve().parent.parent / "history.db"
    if checkout.exists():
        return checkout
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "tasmac-mcp" / "history.db"


DB_PATH = _default_db()

# brandName arrives prefixed: "WINE", "IFL-WINE", "OTHER-WINE" are all wine.
# The prefixes are undocumented; IFL appears to mean imported and OTHER
# out-of-state, but treat that as a guess and keep the raw value around.
KNOWN_PREFIXES = ("IFL", "OTHER")

CATEGORIES = ("WINE", "WHISKY", "BRANDY", "RUM", "GIN", "VODKA", "BEER", "LIQUOR",
              "TEQUILA", "SOJU")

# TASMAC has no TEQUILA category, so 41 tequilas sit in LIQUOR, WHISKY and WINE,
# and every soju is filed as WINE. Someone filtering category=WINE gets tequila;
# someone looking for tequila cannot find it at all. These rules are ours, not
# TASMAC's, so raw_category always keeps whatever they actually said.
DERIVED_CATEGORIES = (
    ("TEQUILA", ("tequila", "mezcal", "mescal")),
    ("SOJU", ("soju",)),
)


def _derive_category(name: str, tasmac_category: str) -> str:
    low = (name or "").lower()
    for category, needles in DERIVED_CATEGORIES:
        if any(n in low for n in needles):
            return category
    return tasmac_category


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

RETRY_CODES = (429, 500, 502, 503, 504)
RETRY_BACKOFF = (2, 5)          # waits between attempts; length + 1 = attempts

# Upstream responses are cached for a short window. TASMAC refreshes its stock
# roughly once a day, so a few minutes of staleness costs nothing real, while
# the saving is large: one hosted server answering many people would otherwise
# hammer a government API that already returns 504 under normal load. Set
# TASMAC_CACHE_TTL=0 to disable.
CACHE_TTL = int(os.environ.get("TASMAC_CACHE_TTL", "900"))
CACHE_MAX = 64                  # full shop payloads are ~550KB, so bound it
_response_cache: dict = {}


def _cache_key(path: str, payload: dict) -> tuple:
    return (path, json.dumps(payload, sort_keys=True))


def _cache_get(key):
    hit = _response_cache.get(key)
    if not hit:
        return None
    expires, value = hit
    if time.monotonic() > expires:
        _response_cache.pop(key, None)
        return None
    return value


def _cache_put(key, value):
    if CACHE_TTL <= 0:
        return
    if len(_response_cache) >= CACHE_MAX:
        oldest = min(_response_cache, key=lambda k: _response_cache[k][0])
        _response_cache.pop(oldest, None)
    _response_cache[key] = (time.monotonic() + CACHE_TTL, value)


def _post(path: str, payload: dict) -> dict:
    """POST to the API. `path` is family-qualified, e.g. 'liquor/get-stockDetails'.

    Retries gateway failures. The heavy endpoints fall over under load: the
    shop catalogue went from under a second to 33 seconds to an outright 504
    over the course of 2026-08-08, and a single 504 is usually gone a few
    seconds later.

    A 409 is never retried: it means the parameter names are wrong, which is a
    contract change no amount of waiting fixes.

    DEADLINE bounds the whole call. No new attempt starts once it has passed,
    so the worst case is DEADLINE plus one attempt's TIMEOUT rather than every
    attempt running to the end.
    """
    key = _cache_key(path, payload)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    url = f"{API_ROOT}/{path}"
    body = json.dumps(payload).encode()
    started = time.monotonic()
    last = ""

    for attempt in range(len(RETRY_BACKOFF) + 1):
        if attempt and time.monotonic() - started > DEADLINE:
            last = f"{last}, gave up at the {DEADLINE}s deadline"
            break
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                parsed = json.loads(resp.read().decode())
                _cache_put(key, parsed)
                return parsed
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code not in RETRY_CODES:
                raise RuntimeError(f"TASMAC API {e.code} on {path}: {detail}") from None
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {getattr(e, 'reason', e)}"

        if attempt == len(RETRY_BACKOFF):
            break
        wait = RETRY_BACKOFF[attempt]
        if time.monotonic() - started + wait > DEADLINE:
            last = f"{last}, gave up at the {DEADLINE}s deadline"
            break
        time.sleep(wait)

    raise RuntimeError(
        f"TASMAC API unreachable on {path} after {attempt + 1} attempt(s): {last}. "
        "The endpoint is usually back within a few minutes."
    ) from None


def _split_category(brand_name: str) -> tuple[str, str]:
    """'OTHER-WINE' -> ('WINE', 'OTHER'). 'WINE' -> ('WINE', '')."""
    raw = (brand_name or "").strip().upper()
    for prefix in KNOWN_PREFIXES:
        if raw.startswith(prefix + "-"):
            return raw[len(prefix) + 1:], prefix
    return raw, ""


def _shop_buyback(rows: list[dict]) -> int | None:
    """The refundable bottle deposit, which is a property of the shop.

    Sampled over 19 shops in 8 districts: each shop charges one rate on every
    SKU it carries, either Rs 10 or nothing, never a mix. 197 of 612 distinct
    products turned up at both rates depending on which shop was asked, so the
    bottle does not decide this and the rate cannot be hardcoded. Returns None
    if a shop ever breaks the pattern, so callers stay quiet rather than quote
    a number that does not hold for the whole basket.
    """
    seen = {r.get("BuyBackamt") for r in rows if r.get("BuyBackamt") is not None}
    return seen.pop() if len(seen) == 1 else None


def fetch_shop(shop_number: str | int) -> dict:
    """Fetch one shop's full catalogue. Returns {shop, district, items: [...]}."""
    shop = str(shop_number).strip()
    body = _post("liquor/get-stockDetailsBy-shopNumber", {"i_ShopNumber": shop})
    rows = body.get("data") or []
    if not rows:
        # TASMAC's shop directory and its stock table disagree for some shops:
        # 4511 and 971 both carry addresses and coordinates in the directory
        # and return nothing at all from stock. Saying "no such shop" about a
        # shop the same API just described is not useful, so check.
        listed = None
        try:
            listed = shop_info(shop)
        except (RuntimeError, LookupError):
            pass
        if listed:
            where = listed["address"] or listed["taluka"] or "no address given"
            raise LookupError(
                f"Shop {shop} is in TASMAC's shop directory ({where}) but has no stock "
                "record. Their directory and stock tables disagree for some shops, "
                "which usually means it is closed or not yet stocked. The shop is real; "
                "the inventory is missing.")
        raise LookupError(f"No TASMAC shop found with number {shop}")

    rec = rows[0]
    items = []
    # The API sometimes returns a product twice, and as of 2026-08-08 it
    # duplicates every in-stock row: 2,419 rows for 2,158 distinct products at
    # shop 4107, with the extra copies identical. Left alone that doubles every
    # count and every table, so collapse on productId and keep the first.
    seen: set = set()
    for it in rec.get("Stock_details") or []:
        pid = it.get("productId")
        if pid is not None:
            if pid in seen:
                continue
            seen.add(pid)
        category, origin = _split_category(it.get("brandName", ""))
        name = (it.get("productName") or "").strip()
        category = _derive_category(name, category)
        items.append({
            "product_id": it.get("productId"),
            "name": name,
            "category": category,
            "origin": origin,
            "raw_category": (it.get("brandName") or "").strip(),
            "unit": it.get("unitName") or "",
            "mrp": it.get("mrpPerBottle"),
            "stock": it.get("currentStock") or 0,
            "pack_size": it.get("packSize"),
            "supplier": (it.get("supplierName") or "").strip(),
        })

    result = {
        "shop": rec.get("shopNumber") or shop,
        "district": rec.get("districtName") or "",
        "district_id": rec.get("districtId"),
        "source_updated": rec.get("last_updated_time"),
        "buyback": _shop_buyback(rec.get("Stock_details") or []),
        "fetched_at": datetime.now(IST).isoformat(timespec="seconds"),
        "items": items,
    }
    try:                                   # street address is a separate, tiny call
        info = shop_info(shop)
        if info:
            result.update({"address": info["address"], "taluka": info["taluka"],
                           "elite": info["elite"], "lat": info["lat"], "lon": info["lon"]})
    except (RuntimeError, KeyError, IndexError):
        pass

    return result


def filter_items(items: list[dict], category: str | None = None,
                 in_stock_only: bool = True, max_price: int | None = None,
                 min_price: int | None = None, query: str | None = None,
                 sort: str = "price") -> list[dict]:
    out = list(items)
    if category:
        want = category.strip().upper()
        out = [i for i in out if i["category"] == want]
    if in_stock_only:
        out = [i for i in out if i["stock"] > 0]
    if max_price is not None:
        out = [i for i in out if (i["mrp"] or 0) <= max_price]
    if min_price is not None:
        out = [i for i in out if (i["mrp"] or 0) >= min_price]
    if query:
        q = query.lower()
        out = [i for i in out if q in i["name"].lower()]

    keys = {
        "price": lambda i: (i["mrp"] or 0, i["name"]),
        "price_desc": lambda i: (-(i["mrp"] or 0), i["name"]),
        "stock": lambda i: (-i["stock"], i["name"]),
        "name": lambda i: i["name"].lower(),
    }
    return sorted(out, key=keys.get(sort, keys["price"]))


# --------------------------------------------------------------------------
# Shop finder: area, district or pincode -> shop numbers
# --------------------------------------------------------------------------

_cache: dict = {}


def districts() -> list[dict]:
    """All revenue districts. [{'id': 3, 'name': 'Chennai'}, ...]"""
    if "districts" not in _cache:
        rows = _post("rv-shop/get-districtList", {}).get("data") or []
        _cache["districts"] = [{"id": r["revenue_district_id"], "name": r["revenue_district_name"]}
                               for r in rows]
    return _cache["districts"]


def taluks() -> list[dict]:
    """All taluks. [{'code': 27, 'name': 'Sholinganallur', 'district_id': 3}, ...]"""
    if "taluks" not in _cache:
        rows = _post("rv-shop/get-talukList", {}).get("data") or []
        _cache["taluks"] = [{"code": r["taluk_code"], "name": r["taluk"],
                             "district_id": r["district_code"]} for r in rows]
    return _cache["taluks"]


def resolve_district(name_or_id: str | int) -> dict | None:
    q = str(name_or_id).strip().lower()
    for d in districts():
        if q == str(d["id"]) or q == d["name"].lower():
            return d
    for d in districts():                       # fall back to substring
        if q and q in d["name"].lower():
            return d
    return None


# --------------------------------------------------------------------------
# Computed tiers
# --------------------------------------------------------------------------

# TASMAC's own "elite" flag is a licence class, not an inventory descriptor.
# Surveyed statewide it is roughly 40% noise: 63 of 157 elite shops stock
# nothing at all above Rs 3,000, so a user sent to the nearest elite shop
# often finds two fortified wines. The tier below is computed from what a shop
# actually carries, which is the question people are really asking.
TIERS_FILE = Path(__file__).resolve().parent / "data" / "tiers.json"
TIER_ORDER = ("flagship", "premium", "standard", "basic", "no_stock")


def tiers() -> dict:
    """The survey: shop number -> tier and the counts behind it."""
    if "tiers" not in _cache:
        try:
            _cache["tiers"] = json.loads(TIERS_FILE.read_text())
        except (OSError, ValueError):
            _cache["tiers"] = {"shops": {}, "surveyed_on": None, "coverage": ""}
    return _cache["tiers"]


def tier_for(shop_number: str | int) -> dict | None:
    """What the survey found at this shop, or None if it was never surveyed.

    None means unknown, not basic. Only Chennai was covered exhaustively;
    elsewhere only elite-tagged shops were, so most of the state is absent.
    """
    return tiers()["shops"].get(str(shop_number))


# Puducherry is 150km away, sells the same bottles under a different duty
# regime, and publishes what they cost. That makes it the reference worth
# ranking against: not "is this fair by world prices" but "is this worth the
# drive". Rebuild with scripts/pondy_import.py when they republish the list.
REFERENCE_FILE = Path(__file__).resolve().parent / "data" / "pondy_prices.json"


def reference_prices() -> dict:
    if "refprices" not in _cache:
        try:
            _cache["refprices"] = json.loads(REFERENCE_FILE.read_text())
        except (OSError, ValueError):
            _cache["refprices"] = {"prices": {}}
    return _cache["refprices"]


def reference_for(product_id: int | str) -> dict | None:
    """What this bottle costs in Puducherry, and TASMAC's multiple of it.

    MRP alone ranks a Rs 19,120 Yamazaki above a Rs 10,120 Bowmore while being
    both the worse whisky and the worse buy. The multiple says whether a price
    is worth paying here rather than merely large: 1.5 means TASMAC charges half
    again what the same bottle costs 150km away.

    Absent means no confident match was found, not that the price is fair.
    Coverage is deliberately thin: pairing is strict because a wrong comparison
    does not fail loudly, it invents a bargain.
    """
    return reference_prices().get("prices", {}).get(str(product_id))


RARITY_FILE = Path(__file__).resolve().parent / "data" / "rarity.json"


def rarity_data() -> dict:
    if "rarity" not in _cache:
        try:
            _cache["rarity"] = json.loads(RARITY_FILE.read_text())
        except (OSError, ValueError):
            _cache["rarity"] = {"carried": {}, "shops_surveyed": 0}
    return _cache["rarity"]


def rarity_for(product_id: int | str) -> dict | None:
    """How widely a bottle is carried, across surveyed shops only.

    "A bottle I cannot get elsewhere" is a real question the API cannot answer:
    it would take one call per shop. The survey already opens every elite shop
    in the state, so the count comes free.

    Absent means not seen in any surveyed shop, which is not the same as
    nowhere in Tamil Nadu.
    """
    d = rarity_data()
    total = d.get("shops_surveyed") or 0
    n = d.get("carried", {}).get(str(product_id))
    if not total or n is None:
        return None
    share = n / total
    band = ("rare" if n <= 3 else
            "uncommon" if share <= 0.15 else
            "common" if share <= 0.5 else "everywhere")
    return {"shops": n, "of": total, "band": band}


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    (lat1, lon1), (lat2, lon2) = a, b
    p = math.pi / 180
    h = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 12742 * math.asin(min(1.0, h ** 0.5))


def _flag_misfiled(shops: list[dict], radius_km: float = 150) -> list[dict]:
    """Mark shops whose coordinates sit far from the rest of their own group.

    TASMAC's district and taluka tags are wrong for a slice of its shops: shop
    57 carries a Chennai address and Chennai coordinates while being filed
    under Ariyalur, and its revenue_district_id is null in the master list. The
    address and coordinates agree with each other, so they are the trustworthy
    pair and the administrative tag is the odd one out. Rather than silently
    serve a shop 300km from where the user asked, flag it.
    """
    pts = [(float(s["lat"]), float(s["lon"])) for s in shops if s.get("lat") and s.get("lon")]
    if len(pts) < 3:
        return shops
    mid = (sorted(p[0] for p in pts)[len(pts) // 2], sorted(p[1] for p in pts)[len(pts) // 2])
    for s in shops:
        s["misfiled"] = bool(s.get("lat") and s.get("lon")
                             and _haversine((float(s["lat"]), float(s["lon"])), mid) > radius_km)
    return shops


def _shop_rows(rows: list[dict]) -> list[dict]:
    """Normalise an rv-shop payload into our shop shape."""
    by_district = {d["id"]: d["name"] for d in districts()}
    out = []
    for r in rows:
        out.append({
            "shop": str(r.get("RVShopsNo")),
            "address": " ".join((r.get("Address") or "").split()),
            "taluka": (r.get("talukaName") or "").strip(),
            "taluka_id": r.get("talukaId"),
            "district": by_district.get(r.get("district_code") or r.get("districtId"), ""),
            "district_id": r.get("district_code") or r.get("districtId"),
            "lat": r.get("latitude"),
            "lon": r.get("longitude"),
            "elite": bool(r.get("isMallShop")),
            "km": r.get("km"),
        })
    for row in out:
        found = tier_for(row["shop"])
        if found:
            row["tier"] = found["tier"]
            row["premium_lines"] = found.get("premium")
            row["marquee"] = found.get("marquee") or []
    return out


def shop_info(shop_number: str | int) -> dict | None:
    """Address and coordinates for one shop."""
    key = ("shop_info", str(shop_number))
    if key not in _cache:
        rows = _post("rv-shop/get-shopListBy-ShopNumber",
                     {"i_ShopNumber": str(shop_number)}).get("data") or []
        _cache[key] = _shop_rows(rows)[0] if rows else None
    return _cache[key]


def shops_in_taluk(taluk_code: int) -> list[dict]:
    key = ("taluk_shops", taluk_code)
    if key not in _cache:
        rows = _post("rv-shop/get-shopListBy-TalukId", {"i_TalukaId": taluk_code}).get("data") or []
        _cache[key] = _shop_rows(rows)
    return _cache[key]


def geocode(place: str) -> tuple[float, float, str]:
    """Turn an area name or pincode into a coordinate. Tamil Nadu is assumed."""
    q = place.strip()
    if "tamil nadu" not in q.lower():
        q = f"{q}, Tamil Nadu, India"
    url = f"{NOMINATIM}?{urllib.parse.urlencode({'format': 'json', 'limit': 1, 'countrycodes': 'in', 'q': q})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            hits = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise RuntimeError(f"Could not reach the geocoder: {e}") from None
    if not hits:
        raise LookupError(f"Could not place '{place}'. Try a nearby landmark, or a district name.")
    return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("display_name", q)


def nearby_shops(lat: float, lon: float, limit: int = 10,
                 with_address: bool = True) -> list[dict]:
    """Shops nearest a coordinate, closest first, with distance in km."""
    rows = _post("rv-shop/get-Nearby-ShopDetails",
                 {"p_latitude": str(lat), "p_longitude": str(lon)}).get("data") or []
    shops = sorted(_shop_rows(rows), key=lambda s: s["km"] if s["km"] is not None else 9e9)[:limit]
    if with_address and shops:
        # The nearby endpoint omits Address. Pull it per taluk rather than per
        # shop, since the nearest results usually share only one or two taluks.
        addr: dict[str, str] = {}
        for tid in {s["taluka_id"] for s in shops if s["taluka_id"]}:
            try:
                addr.update({s["shop"]: s["address"] for s in shops_in_taluk(tid)})
            except RuntimeError:
                pass
        for s in shops:
            s["address"] = s["address"] or addr.get(s["shop"], "")
    return shops


def _apply_tier_filter(shops: list[dict], tier: str) -> list[dict]:
    """Keep shops at or above the named tier, and say what was dropped.

    Unsurveyed shops are excluded rather than assumed basic: absence from the
    survey means unknown.
    """
    want = tier.strip().lower()
    if want not in TIER_ORDER:
        return shops
    cutoff = TIER_ORDER.index(want)
    return [s for s in shops
            if s.get("tier") and TIER_ORDER.index(s["tier"]) <= cutoff]


def find_shops(area: str = "", district: str = "", pincode: str = "",
               tier: str = "", limit: int = 10) -> dict:
    """Find shop numbers by area name, district name or pincode.

    Area and pincode both resolve to a coordinate and return shops ordered by
    distance. A bare taluka name skips the geocoder and lists that taluka
    directly. District returns every shop in the district, grouped by taluka.
    """
    if district:
        d = resolve_district(district)
        if not d:
            return {"error": f"No district matching '{district}'. "
                             f"Known: {', '.join(x['name'] for x in districts())}"}
        shops: list[dict] = []
        for t in [t for t in taluks() if t["district_id"] == d["id"]]:
            try:
                shops.extend(shops_in_taluk(t["code"]))
            except RuntimeError:
                pass
        found = _flag_misfiled(sorted(shops, key=lambda s: (s["taluka"], s["shop"])))
        if tier:
            found = _apply_tier_filter(found, tier)
            found.sort(key=lambda s: -(s.get("premium_lines") or 0))
        return {"mode": "district", "label": f"{d['name']} district"
                                             + (f", {tier} and above" if tier else ""),
                "shops": found}

    place = (area or pincode or "").strip()
    if not place:
        return {"error": "Give an area, a district or a pincode."}

    # Distance ordering is what someone asking about an area actually wants, and
    # the nearest shop often sits just over a taluka boundary. So geocode first
    # and only fall back to the taluka name match if the geocoder is no help.
    try:
        lat, lon, label = geocode(place)
        # A tier filter needs a wider net first: the nearest ten shops are
        # mostly basic, so filtering after a small fetch returns nothing.
        found = nearby_shops(lat, lon, limit if not tier else 120)
        if tier:
            found = _apply_tier_filter(found, tier)[:limit]
        return {"mode": "nearby", "label": label + (f", {tier} and above" if tier else ""),
                "lat": lat, "lon": lon, "shops": found}
    except (LookupError, RuntimeError) as geo_error:
        for t in taluks():
            if place.lower() == t["name"].lower():
                return {"mode": "taluka", "label": f"{t['name']} taluka (whole taluka, "
                                                   "not ordered by distance)",
                        "shops": _flag_misfiled(sorted(shops_in_taluk(t["code"]),
                                                       key=lambda s: s["shop"]))}
        raise geo_error


# --------------------------------------------------------------------------
# Product-first search: which shop near me has this bottle
# --------------------------------------------------------------------------

PRODUCT_CACHE_DAYS = 7
# Each product variant costs one API call to locate, so cap the fan-out.
MAX_PRODUCT_QUERIES = 6
# Each candidate costs one call to check availability, so a recommendation
# walks a bounded distance down the ranking looking for something buyable.
MAX_RECOMMEND_LOOKUPS = 12


def products(force_refresh: bool = False) -> list[dict]:
    """The statewide product catalogue, about 2100 SKUs.

    The API nests this three deep (supplier -> product name -> pack variants),
    so it is flattened to one row per sellable variant. Cached on disk because
    it is a 360KB call that changes rarely.
    """
    if "products" in _cache and not force_refresh:
        return _cache["products"]

    if not force_refresh:
        cached = _load_cached_products()
        if cached:
            _cache["products"] = cached
            return cached

    body = _post("liquor/get-productList", {})
    out = []
    for supplier in body.get("data") or []:
        sup = (supplier.get("Supplier_Name") or "").strip()
        for prod in supplier.get("Product_Name") or []:
            name = (prod.get("productName") or "").strip()
            for v in prod.get("productDetails") or []:
                category, origin = _split_category(v.get("brandName", ""))
                category = _derive_category(name, category)
                out.append({
                    "product_id": v.get("pkProductId"),
                    "name": name,
                    "category": category,
                    "origin": origin,
                    "unit": v.get("unitName") or "",
                    "mrp": v.get("mrpPerBottle"),
                    "pack_size": v.get("packSize"),
                    "supplier": sup,
                    "supplier_type": v.get("supplierType") or "",
                })
    _cache["products"] = out
    try:
        _save_cached_products(out)
    except sqlite3.Error:
        pass
    return out


def _load_cached_products() -> list[dict] | None:
    try:
        with _db() as conn:
            row = conn.execute("SELECT max(cached_on) FROM products").fetchone()
            if not row or not row[0]:
                return None
            age = (datetime.now(IST).date() - datetime.fromisoformat(row[0]).date()).days
            if age > PRODUCT_CACHE_DAYS:
                return None
            cols = ["product_id", "name", "category", "origin", "unit", "mrp",
                    "pack_size", "supplier", "supplier_type"]
            return [dict(zip(cols, r)) for r in
                    conn.execute(f"SELECT {','.join(cols)} FROM products")]
    except sqlite3.Error:
        return None


def _save_cached_products(rows: list[dict]) -> None:
    today = datetime.now(IST).date().isoformat()
    with _db() as conn:
        conn.execute("DELETE FROM products")
        conn.executemany(
            "INSERT OR REPLACE INTO products (product_id, name, category, origin, unit, mrp,"
            " pack_size, supplier, supplier_type, cached_on) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(r["product_id"], r["name"], r["category"], r["origin"], r["unit"], r["mrp"],
              r["pack_size"], r["supplier"], r["supplier_type"], today)
             for r in rows if r["product_id"] is not None])


def _norm(s: str) -> str:
    """Lowercase, drop punctuation, collapse spaces.

    TASMAC's catalogue writes "JACOB S CREEK" with no apostrophe, so a literal
    match on what a person would actually type finds nothing.
    """
    return " ".join("".join(c if c.isalnum() else " " for c in s.lower()).split())


def search_products(query: str, category: str = "", limit: int = 12) -> list[dict]:
    """Match a product by name. Every word in the query must appear, in any
    order, so "sula sauvignon" finds "Sula Vineyards Sauvignon Blanc"."""
    q = _norm(query)
    if not q:
        return []
    words = q.split()
    # The catalogue spells the same brand both "JACOB S CREEK" and "Jacobs
    # Creek", so also compare with spaces removed.
    qc = q.replace(" ", "")
    hits = []
    for p in products():
        n = _norm(p["name"])
        if all(w in n for w in words) or qc in n.replace(" ", ""):
            hits.append((n, p))
    if category:
        want = category.strip().upper()
        hits = [(n, p) for n, p in hits if p["category"] == want]
    hits.sort(key=lambda t: (t[0] != q, not t[0].startswith(q), len(t[0]), t[1]["unit"]))
    return [p for _, p in hits][:limit]


def product_shops(product_id: int, lat: float, lon: float, limit: int = 10) -> list[dict]:
    """Shops near a coordinate that currently stock this product, nearest first."""
    rows = _post("liquor/get-stockDetailsBy-ProductId/lat-long",
                 {"p_latitude": str(lat), "p_longitude": str(lon),
                  "p_productId": str(product_id)}).get("data") or []
    out = []
    # Same upstream duplication as the shop endpoint, worse here: this one
    # repeated every (shop, product) pair three times on 2026-08-08. Dedupe on
    # the pair, since one product legitimately appears at many shops.
    seen: set = set()
    for r in rows:
        sd = r.get("Stock_details") or {}
        key = (r.get("shopNumber"), sd.get("productId"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "shop": str(r.get("shopNumber")),
            "km": r.get("km"),
            "taluka": (r.get("talukaName") or "").strip(),
            "taluka_id": r.get("talukaId"),
            "district": (r.get("districtName") or "").strip(),
            "stock": sd.get("currentStock") or 0,
            "product": (sd.get("productName") or "").strip(),
            "unit": sd.get("unitName") or "",
            "address": "",
        })
    out = [s for s in out if s["stock"] > 0]
    out.sort(key=lambda s: s["km"] if s["km"] is not None else 9e9)
    out = out[:limit]

    addr: dict[str, str] = {}
    for tid in {s["taluka_id"] for s in out if s["taluka_id"]}:
        try:
            addr.update({s["shop"]: s["address"] for s in shops_in_taluk(tid)})
        except RuntimeError:
            pass
    for s in out:
        s["address"] = addr.get(s["shop"], "")
    return out


def find_product(query: str, area: str = "", pincode: str = "",
                 lat: float | None = None, lon: float | None = None,
                 category: str = "", limit: int = 10) -> dict:
    """Find shops near a place that stock a given bottle.

    The catalogue is statewide, so a product existing says nothing about it
    being on a shelf near you. That is what the per-product location call
    answers, and it is the only endpoint that does.
    """
    place_label = ""
    if lat is None or lon is None:
        place = (area or pincode or "").strip()
        if not place:
            return {"error": "Give an area or pincode to search near."}
        lat, lon, place_label = geocode(place)
    else:
        place_label = f"{lat}, {lon}"

    matches = search_products(query, category)
    if not matches:
        return {"error": f"No product matching '{query}' in the TASMAC catalogue. "
                         "Try fewer words, or a brand name on its own."}

    # A name like "Old Monk" matches several distinct products, each in three
    # pack sizes. Taking the first few matches would spend the whole budget on
    # one product's sizes and wrongly report the others as unavailable, so pick
    # round robin across distinct names first and only then go wider on sizes.
    by_name: dict[str, list[dict]] = {}
    for p in matches:
        by_name.setdefault(p["name"], []).append(p)
    ordered, rank = [], 0
    while len(ordered) < len(matches):
        for variants in by_name.values():
            if rank < len(variants):
                ordered.append(variants[rank])
        rank += 1

    for p in ordered:
        p["rarity"] = rarity_for(p["product_id"])

    searched, shops = [], []
    for p in ordered[:MAX_PRODUCT_QUERIES]:
        try:
            found = product_shops(p["product_id"], lat, lon, limit)
        except RuntimeError:
            continue
        searched.append(p)
        for s in found:
            shops.append({**s, "mrp": p["mrp"], "product": s["product"] or p["name"],
                          "unit": s["unit"] or p["unit"]})

    shops.sort(key=lambda s: (s["km"] if s["km"] is not None else 9e9, s["product"]))
    searched_ids = {p["product_id"] for p in searched}
    return {"query": query, "near": place_label, "lat": lat, "lon": lon,
            "searched": searched,
            "other_matches": [p for p in matches if p["product_id"] not in searched_ids],
            "shops": shops[:limit]}


# --------------------------------------------------------------------------
# Recommendation
# --------------------------------------------------------------------------

def recommend(prefer: str = "value", category: str = "", max_price: int = 0,
              min_price: int = 0, area: str = "", pincode: str = "",
              limit: int = 5) -> dict:
    """Rank bottles by something other than price, then say where to buy them.

    Sorting by MRP answers "what is expensive", which nobody asks. The axes
    here are the ones people actually mean:

      value  cheapest relative to Puducherry, so a fair price rather than a
             low one. 231 bottles carry a reference, so this ranks a
             deliberately small set and says so.
      rare   carried by fewest surveyed shops, for when the point is to find
             something you cannot get at home.

    What is deliberately missing: taste. There is no grape, region, peat or
    body metadata in the catalogue and product names do not reliably encode
    it, so "a peaty Islay for the price" cannot be answered honestly and is
    not faked.
    """
    axis = (prefer or "value").strip().lower()
    if axis not in ("value", "rare"):
        return {"error": "prefer must be 'value' (best price against Puducherry) "
                         "or 'rare' (hardest to find)."}

    pool = []
    for p in products():
        mrp = p["mrp"] or 0
        if category and p["category"] != category.strip().upper():
            continue
        if max_price and mrp > max_price:
            continue
        if min_price and mrp < min_price:
            continue
        ref, rar = reference_for(p["product_id"]), rarity_for(p["product_id"])
        if axis == "value" and not ref:
            continue
        if axis == "rare" and not rar:
            continue
        pool.append({**p, "reference": ref, "rarity": rar})

    if not pool:
        hint = ("Only 231 bottles have a Puducherry reference. Try prefer='rare', "
                "or widen the filters."
                if axis == "value" else
                "Rarity covers surveyed shops only. Try widening the filters.")
        return {"error": f"Nothing to rank on {axis}. {hint}"}

    if axis == "value":
        pool.sort(key=lambda b: b["reference"]["multiple"])
    else:
        pool.sort(key=lambda b: (b["rarity"]["shops"], b["mrp"] or 0))
    picks = pool[:limit]

    # With a location, rank only what can actually be bought there. The first
    # version ranked purely on the axis and then reported that the top three
    # were not stocked anywhere nearby, which is a fact rather than a
    # recommendation. So walk down the ranking until enough of it is buyable.
    place = (area or pincode or "").strip()
    if not place:
        return {"prefer": axis, "near": "", "picks": pool[:limit], "considered": len(pool)}

    try:
        lat, lon, label = geocode(place)
    except (LookupError, RuntimeError):
        return {"prefer": axis, "near": place, "picks": pool[:limit],
                "considered": len(pool),
                "note": "Could not place that location, so these are not filtered by stock."}

    picks, checked = [], 0
    for b in pool:
        if len(picks) >= limit or checked >= MAX_RECOMMEND_LOOKUPS:
            break
        checked += 1
        try:
            shops = product_shops(b["product_id"], lat, lon, limit=3)
        except RuntimeError:
            continue
        if shops:
            picks.append({**b, "where": shops})
    note = None
    if len(picks) < limit:
        note = (f"Checked the top {checked} on {axis}; only {len(picks)} of them are "
                "stocked near there today.")
    return {"prefer": axis, "near": label, "picks": picks,
            "considered": len(pool), "checked": checked, "note": note}


def format_recommend(res: dict, limit: int = 5) -> str:
    if res.get("error"):
        return res["error"]
    axis = res["prefer"]
    head = (f"Ranked by {'price against Puducherry' if axis == 'value' else 'how hard to find'}"
            + (f", near {res['near']}" if res.get("near") else "")
            + f". {res['considered']} bottles had the data to rank.")
    lines = [head]
    if res.get("note"):
        lines.append(res["note"])
    for i, b in enumerate(res["picks"][:limit], 1):
        bits = [f"Rs {b['mrp']:,}"]
        if b.get("reference"):
            bits.append(f"{b['reference']['multiple']:.1f}x Puducherry")
        if b.get("rarity"):
            bits.append(f"{b['rarity']['shops']} of {b['rarity']['of']} shops")
        lines.append(f"\n{i}. {b['name']} {b['unit']}  ({', '.join(bits)})")
        for s in (b.get("where") or [])[:2]:
            lines.append(f"     shop {s['shop']}, {s['km']} km, {s['stock']} in stock"
                         + (f" - {s['address'][:38]}" if s.get("address") else ""))
        if not b.get("where") and res.get("near"):
            lines.append("     not stocked in any shop near there today")
    if axis == "value":
        lines.append("\nValue is TASMAC's price against Puducherry's, not quality. The "
                     "catalogue carries no taste data, so nothing here knows what it "
                     "tastes like.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            product_id    INTEGER PRIMARY KEY,
            name          TEXT,
            category      TEXT,
            origin        TEXT,
            unit          TEXT,
            mrp           INTEGER,
            pack_size     INTEGER,
            supplier      TEXT,
            supplier_type TEXT,
            cached_on     TEXT
        );
    """)
    return conn


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in ([headers] + rows)) for i in range(len(headers))]
    line = lambda r: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip()
    return "\n".join([line(headers), "  ".join("-" * w for w in widths)] + [line(r) for r in rows])


def format_shops(result: dict, limit: int = 25) -> str:
    if result.get("error"):
        return result["error"]
    shops = result["shops"]
    head = f"{len(shops)} shops, {result['label']}"
    if result["mode"] == "nearby":
        head += " (nearest first)"
    if not shops:
        return head + "\n(none found)"
    shown = shops[:limit]
    has_km = result["mode"] == "nearby"
    rows = [[s["shop"], (f"{s['km']} km" if has_km else s["taluka"]),
             s.get("tier") or "-",
             str(s.get("premium_lines")) if s.get("premium_lines") is not None else "-",
             (s["address"] or "-")[:44],
             " ".join(x for x in ["elite" if s["elite"] else "",
                                  "(?)" if s.get("misfiled") else ""] if x)] for s in shown]
    out = head + "\n\n" + _table(
        rows, ["SHOP", "KM" if has_km else "TALUKA", "TIER", ">3K", "ADDRESS", "LICENCE"])
    if len(shops) > limit:
        out += f"\n... {len(shops) - limit} more"
    flagged = sum(1 for s in shown if s.get("misfiled"))
    if flagged:
        out += (f"\n\n(?) {flagged} shop(s) sit far from the rest of this group. TASMAC files "
                "them here but their address and coordinates say otherwise. Trust the address.")
    return out + "\n\nLook up stock with the shop number."


def format_product_search(result: dict, limit: int = 15) -> str:
    if result.get("error"):
        return result["error"]
    searched = result["searched"]
    names = {p["name"] for p in searched}
    label = next(iter(names)) if len(names) == 1 else result["query"]
    head = f"'{label}' near {result['near']}"
    if len(names) > 1:
        head += f"\nMatched {len(names)} products: " + ", ".join(sorted(names))
    shops = result["shops"]
    if not shops:
        out = (head + f"\n\nIn the catalogue but not stocked in any shop near here.\n"
               f"Searched: " + ", ".join(f"{p['name']} {p['unit']}" for p in searched))
    else:
        rows = [[s["shop"], f"{s['km']} km", s["stock"],
                 f"{s['mrp']}" if s.get("mrp") else "?",
                 f"{s['product']} {s['unit']}".strip()[:34],
                 (s["address"] or s["taluka"] or "-")[:42]] for s in shops[:limit]]
        out = head + "\n\n" + _table(rows, ["SHOP", "KM", "STOCK", "MRP", "PRODUCT", "WHERE"])
        if len(shops) > limit:
            out += f"\n... {len(shops) - limit} more"
    rare = [p for p in searched if (p.get("rarity") or {}).get("band") in ("rare", "uncommon")]
    if rare:
        out += "\n\n" + "\n".join(
            f"{p['name']} {p['unit']}: carried by {p['rarity']['shops']} of "
            f"{p['rarity']['of']} surveyed shops ({p['rarity']['band']})" for p in rare[:3])
    others = result.get("other_matches") or []
    if others:
        out += ("\n\nOther catalogue matches not searched: "
                + ", ".join(f"{p['name']} {p['unit']}" for p in others[:6]))
    return out


def format_stock(shop_data: dict, items: list[dict], limit: int = 60) -> str:
    where = shop_data.get("address") or shop_data.get("taluka") or ""
    stamp = shop_data.get("source_updated")
    head = (f"Shop #{shop_data['shop']}"
            + (f", {where}" if where else "")
            + f", {shop_data['district']} district. "
            + f"{len(items)} matching. "
            + (f"TASMAC updated this shop's stock at {stamp}. "
               if stamp else "TASMAC gave no stock timestamp for this shop. ")
            + f"Fetched {shop_data['fetched_at']}.")
    if not items:
        return head + "\n(nothing matched)"
    shown = items[:limit]
    refs = {i["product_id"]: reference_for(i["product_id"]) for i in shown}
    has_ref = any(refs.values())
    rows = []
    for i in shown:
        r = refs.get(i["product_id"])
        row = [f"{i['mrp']}" if i["mrp"] else "?", i["name"], i["unit"], i["stock"],
               i["origin"] or "-"]
        if has_ref:
            row.append(f"{r['multiple']:.1f}x" if r else "-")
        rows.append(row)
    cols = ["MRP", "PRODUCT", "SIZE", "STOCK", "ORIGIN"] + (["VS PY"] if has_ref else [])
    out = head + "\n\n" + _table(rows, cols)
    if has_ref:
        out += ("\n\nVS PY is TASMAC's price against Puducherry's, same bottle. Blank means "
                "no confident match, not a fair price.")
    deposit = shop_data.get("buyback")
    if deposit:
        out += (f"\n\nThis shop adds Rs {deposit} per bottle as a refundable deposit, "
                "returned when you bring the empty back. Some shops charge nothing, "
                "so it is read per shop rather than assumed.")
    elif deposit == 0:
        out += "\n\nThis shop charges no bottle deposit."
    if len(items) > limit:
        out += f"\n... {len(items) - limit} more (raise --limit)"
    if any(i["stock"] >= 12 for i in shown):
        out += ("\n\nStock is one reading, not a rate. It cannot tell you how fast "
                "something sells, and it is not a guarantee of what is on the shelf "
                "when you arrive.")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="TASMAC shop stock lookup and shop finder.")
    p.add_argument("shop", nargs="?", help="Shop number, e.g. 4107")
    p.add_argument("--find", metavar="AREA_OR_PINCODE",
                   help="Find shops by area name or pincode, e.g. --find 600119")
    p.add_argument("--district", metavar="NAME", help="List every shop in a district")
    p.add_argument("--tier", metavar="TIER",
                   help="Only shops at this computed tier or better: "
                        "flagship, premium, standard, basic")
    p.add_argument("--near", metavar="LAT,LON", help="Find shops near a coordinate")
    p.add_argument("--product", metavar="NAME",
                   help="Find shops near you stocking this bottle. Pair with --find or --near")
    p.add_argument("-c", "--category", help=f"One of: {', '.join(c.lower() for c in CATEGORIES)}")
    p.add_argument("-q", "--query", help="Substring match on product name")
    p.add_argument("--max-price", type=int)
    p.add_argument("--min-price", type=int)
    p.add_argument("--all", action="store_true", help="Include out of stock items")
    p.add_argument("--sort", default="price", choices=["price", "price_desc", "stock", "name"])
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.product:
        lat = lon = None
        if args.near:
            lat, lon = (float(x) for x in args.near.split(",", 1))
        place = args.find or ""
        res = find_product(args.product, area="" if place.isdigit() else place,
                           pincode=place if place.isdigit() else "",
                           lat=lat, lon=lon, category=args.category or "", limit=args.limit)
        print(json.dumps(res, indent=2) if args.json else format_product_search(res, args.limit))
        return

    if args.find or args.district or args.near:
        if args.near:
            lat, lon = (float(x) for x in args.near.split(",", 1))
            res = {"mode": "nearby", "label": f"{lat}, {lon}",
                   "shops": nearby_shops(lat, lon, args.limit)}
        else:
            place = args.find or ""
            res = find_shops(area="" if place.isdigit() else place,
                             pincode=place if place.isdigit() else "",
                             district=args.district or "", tier=args.tier or "",
                             limit=args.limit)
        print(json.dumps(res, indent=2) if args.json else format_shops(res, args.limit))
        return

    if not args.shop:
        p.error("give a shop number, or use --find / --district / --near")

    data = fetch_shop(args.shop)
    items = filter_items(data["items"], category=args.category, in_stock_only=not args.all,
                         max_price=args.max_price, min_price=args.min_price,
                         query=args.query, sort=args.sort)
    if args.json:
        print(json.dumps({**data, "items": items}, indent=2))
    else:
        print(format_stock(data, items, args.limit))


if __name__ == "__main__":
    main()
