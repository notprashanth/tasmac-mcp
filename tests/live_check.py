#!/usr/bin/env python3
"""Live canary against the real TASMAC API.

The offline tests prove our code is right. This proves the API still behaves
the way our code assumes. It exists because on 2026-08-08 the upstream started
duplicating rows on two endpoints and a shop lookup slowed from under a second
to thirty three, all without warning and all producing plausible-looking
output.

Two kinds of finding, deliberately separated:

  FAIL   our contract with the API is broken, or our output is now wrong.
         Someone has to change something.
  NOTE   upstream changed in a way we already absorb. Worth knowing, not
         worth waking up for.

Run:  python3 tests/live_check.py
Exit: 0 all good, 1 at least one FAIL.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tasmac_mcp import core

SHOP = "4107"                 # Sholinganallur, large and busy, good canary
PINCODE = "600119"
PRODUCT = "Sula Brut"
SLOW_SECONDS = 45             # the shop endpoint hit 33s on 2026-08-08

fails: list = []
notes: list = []


def fail(check, detail):
    fails.append(check)
    print(f"FAIL  {check}\n      {detail}")


def note(check, detail):
    notes.append(check)
    print(f"NOTE  {check}\n      {detail}")


def ok(check, detail=""):
    print(f"ok    {check}" + (f"  ({detail})" if detail else ""))


RETRIES = 3
RETRY_WAIT = 15


def raw_post(path, payload):
    req = urllib.request.Request(
        f"{core.API_ROOT}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": core.USER_AGENT},
        method="POST")
    with urllib.request.urlopen(req, timeout=core.TIMEOUT) as r:
        return json.loads(r.read().decode())


def retrying(fn, *args, **kwargs):
    """Run fn, retrying transient gateway failures.

    TASMAC's heavy endpoints return 502/503/504 under load and time out when
    slow. A daily canary that fails on those is noise nobody reads, so a
    failure only counts once it survives several attempts spread over a minute.
    """
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            return fn(*args, **kwargs), None
        except (urllib.error.HTTPError, urllib.error.URLError,
                RuntimeError, TimeoutError, OSError) as e:
            last = e
            transient = (
                isinstance(e, urllib.error.HTTPError) and e.code in (429, 500, 502, 503, 504)
                or isinstance(e, (urllib.error.URLError, TimeoutError, OSError))
                or "50" in str(e)[:80] and "API 5" in str(e)
            )
            if not transient or attempt == RETRIES:
                break
            print(f"      transient {type(e).__name__} on attempt {attempt}, "
                  f"retrying in {RETRY_WAIT}s")
            time.sleep(RETRY_WAIT)
    return None, last


# --------------------------------------------------------------------------
# 1. The parameter contract. These names are undocumented and unguessable, so
#    a rename upstream silently breaks everything.
# --------------------------------------------------------------------------

def check_parameter_contract():
    cases = [
        ("liquor/get-stockDetailsBy-shopNumber", {"i_ShopNumber": SHOP}),
        ("rv-shop/get-shopListBy-ShopNumber", {"i_ShopNumber": SHOP}),
        ("rv-shop/get-districtList", {}),
        ("rv-shop/get-talukList", {}),
        ("rv-shop/get-Nearby-ShopDetails", {"p_latitude": "12.90", "p_longitude": "80.22"}),
        ("liquor/get-productList", {}),
    ]
    for path, payload in cases:
        body, err = retrying(raw_post, path, payload)
        if err is not None:
            code = getattr(err, "code", None)
            if code in (429, 500, 502, 503, 504):
                note(f"contract {path}", f"upstream {code} on every attempt, endpoint is down "
                                         f"rather than changed")
            else:
                fail(f"contract {path}", f"{type(err).__name__}: {str(err)[:160]}")
        elif not body.get("status"):
            fail(f"contract {path}", f"status false: {body.get('message')}")
        elif not body.get("data"):
            fail(f"contract {path}", "returned no data")
        else:
            ok(f"contract {path}")


# --------------------------------------------------------------------------
# 2. Our output must never contain duplicates, whatever upstream sends.
# --------------------------------------------------------------------------

def check_shop_lookup():
    started = time.time()
    data, err = retrying(core.fetch_shop, SHOP, write_history=False)
    elapsed = time.time() - started
    if err is not None:
        code = getattr(err, "code", None) or ("504" if "504" in str(err) else None)
        if code:
            note("shop lookup", f"upstream {code} on every attempt after {elapsed:.0f}s. "
                                f"The endpoint is unavailable, not broken by a change.")
        else:
            fail("shop lookup", f"{type(err).__name__}: {str(err)[:160]}")
        return

    items = data["items"]
    ids = [i["product_id"] for i in items if i["product_id"] is not None]
    if len(ids) != len(set(ids)):
        dupes = len(ids) - len(set(ids))
        fail("shop lookup dedupe", f"{dupes} repeated product_id in our own output")
    else:
        ok("shop lookup dedupe", f"{len(items)} unique SKUs")

    in_stock = [i for i in items if i["stock"] > 0]
    if not in_stock:
        fail("shop stock", f"shop {SHOP} reports nothing in stock at all")
    elif len(items) < 500:
        fail("shop catalogue", f"only {len(items)} SKUs listed, expected over 500")
    else:
        ok("shop stock", f"{len(in_stock)} in stock of {len(items)}")

    priced = [i for i in in_stock if not i["mrp"] or i["mrp"] <= 0]
    if priced:
        fail("mrp present", f"{len(priced)} in-stock items have no usable MRP")
    else:
        ok("mrp present")

    if elapsed > SLOW_SECONDS:
        note("shop endpoint slow", f"{elapsed:.0f}s, over the {SLOW_SECONDS}s mark")
    else:
        ok("shop endpoint speed", f"{elapsed:.1f}s")

    # Upstream duplication is absorbed, but report the ratio so a change shows up.
    try:
        body = raw_post("liquor/get-stockDetailsBy-shopNumber", {"i_ShopNumber": SHOP})
        rows = body["data"][0]["Stock_details"]
        uniq = len({r["productId"] for r in rows})
        if len(rows) != uniq:
            note("upstream duplication", f"{len(rows)} rows for {uniq} products "
                                         f"(x{len(rows) / uniq:.1f}), absorbed by dedupe")
    except Exception:
        pass


def check_product_search():
    try:
        res = core.find_product(PRODUCT, pincode=PINCODE, limit=10)
    except Exception as e:
        fail("product search", f"{type(e).__name__}: {e}")
        return
    if res.get("error"):
        fail("product search", res["error"])
        return

    shops = res["shops"]
    if not shops:
        note("product search", f"'{PRODUCT}' not stocked near {PINCODE} today")
        return

    pairs = [(s["shop"], s["product"]) for s in shops]
    if len(pairs) != len(set(pairs)):
        fail("product search dedupe",
             f"{len(pairs) - len(set(pairs))} repeated (shop, product) rows in our output")
    else:
        ok("product search dedupe", f"{len(shops)} rows")

    kms = [s["km"] for s in shops if s["km"] is not None]
    if kms != sorted(kms):
        fail("product search order", "results are not sorted by distance")
    else:
        ok("product search order")

    if any(s["stock"] <= 0 for s in shops):
        fail("product search stock", "a result has zero stock but was returned anyway")
    else:
        ok("product search stock")


def check_shop_finder():
    try:
        res = core.find_shops(pincode=PINCODE, limit=10)
    except Exception as e:
        fail("shop finder", f"{type(e).__name__}: {e}")
        return
    if res.get("error"):
        fail("shop finder", res["error"])
        return

    shops = res["shops"]
    if not shops:
        fail("shop finder", f"no shops found near {PINCODE}")
        return

    nums = [s["shop"] for s in shops]
    if len(nums) != len(set(nums)):
        fail("shop finder dedupe", "the same shop appears twice")
    else:
        ok("shop finder", f"{len(shops)} shops, nearest {shops[0]['km']} km")

    if not any(s["address"] for s in shops):
        fail("shop finder addresses", "no result carried a street address")
    else:
        ok("shop finder addresses")


def check_masters():
    try:
        d, t = core.districts(), core.taluks()
    except Exception as e:
        fail("master lists", f"{type(e).__name__}: {e}")
        return
    if len(d) < 30:
        fail("district list", f"only {len(d)} districts, expected 38ish")
    else:
        ok("district list", f"{len(d)} districts")
    if len(t) < 100:
        fail("taluk list", f"only {len(t)} taluks")
    else:
        ok("taluk list", f"{len(t)} taluks")

    try:
        catalogue = core.products(force_refresh=True)
    except Exception as e:
        fail("catalogue", f"{type(e).__name__}: {e}")
        return
    if len(catalogue) < 1000:
        fail("catalogue", f"only {len(catalogue)} variants, expected over 2000")
    else:
        ok("catalogue", f"{len(catalogue)} variants")


def check_recommend():
    """Ranking still works against the real catalogue.

    The offline tests stub products(), because ranking is not the catalogue's
    parser and the unit suite must not touch the network. That stub is only
    honest if something else checks the join it papers over: reference_prices
    and rarity are keyed by product_id, so a rename of pkProductId or
    mrpPerBottle leaves the variant COUNT healthy - which is all check_masters
    looks at - while every id silently stops matching and recommend() ranks
    nothing. This is the check that would catch that.
    """
    for axis in ("value", "rare"):
        try:
            res = core.recommend(prefer=axis, limit=3)
        except Exception as e:
            fail(f"recommend {axis}", f"{type(e).__name__}: {e}")
            continue

        if "error" in res:
            fail(f"recommend {axis}", res["error"])
            continue
        picks = res.get("picks") or []
        if not picks:
            fail(f"recommend {axis}", "ranked nothing, so no id matched the data files")
            continue

        key = "reference" if axis == "value" else "rarity"
        unjoined = [p for p in picks if not p.get(key)]
        if unjoined:
            fail(f"recommend {axis}",
                 f"{len(unjoined)} of {len(picks)} picks carry no {key}")
        else:
            ok(f"recommend {axis}",
               f"{len(picks)} picks from {res.get('considered')} ranked")


def main():
    print(f"TASMAC live canary  ·  shop {SHOP}  ·  pincode {PINCODE}\n")
    # check_recommend last: it reuses the catalogue check_masters just fetched.
    for fn in (check_parameter_contract, check_shop_lookup, check_product_search,
               check_shop_finder, check_masters, check_recommend):
        try:
            fn()
        except Exception as e:                       # never let the canary itself crash
            fail(fn.__name__, f"canary error {type(e).__name__}: {e}")
        print()

    print("-" * 60)
    if fails:
        print(f"{len(fails)} FAIL, {len(notes)} note")
        print("failed: " + ", ".join(fails))
        return 1
    print(f"all checks passed, {len(notes)} note" + ("s" if len(notes) != 1 else ""))
    if notes:
        print("noted: " + ", ".join(notes))
    return 0


if __name__ == "__main__":
    # History writes are off here: the canary runs daily and must not pollute
    # the snapshot archive that tasmac_changes reads.
    sys.exit(main())
