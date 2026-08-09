#!/usr/bin/env python3
"""Build a duty-free reference price for TASMAC's premium bottles.

Why duty-free rather than US retail: 447 of TASMAC's 451 lines above Rs 3,000
are imported, so coverage is not the deciding factor. Duty-free is the buyer's
actual alternative (the next flight), it is quoted in rupees so no exchange
rate drifts underneath the ratio, and it is roughly the product without excise,
which makes TASMAC price divided by duty-free price mean something: the markup.

Source: Delhi Duty Free's own Magento GraphQL endpoint. robots.txt disallows
only /catalogsearch/ and /search/, neither of which this touches. Run rarely
and commit the result: the MCP server reads the snapshot and never calls out.

MATCHING IS THE HARD PART, NOT FETCHING.

A naive first-hit match produced Louis XIII at 99.6x, because the search for
"Remy Martin" returned a cheaper Remy. Glenfiddich 18 came back at 0.4x for the
same reason. Confident wrong numbers are worse than gaps, which is the exact
failure the improvement spec caught in SEO price sites. So a match is only
accepted when:

  - every distinctive brand token in the TASMAC name appears in the candidate
  - age statements agree exactly, including both being absent
  - both sides declare a volume, and prices are compared per litre so a 1L
    duty-free bottle is never mistaken for a 750ml one

Anything else is left blank. A bottle with no reference price says so.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tasmac_mcp import core

ENDPOINT = "https://www.delhidutyfree.co.in/graphql"
UA = "tasmac-mcp price reference (github.com/notprashanth/tasmac-mcp)"
OUT = Path(__file__).resolve().parent.parent / "tasmac_mcp" / "data" / "reference_prices.json"
IST = timezone(timedelta(hours=5, minutes=30))
DELAY = 0.4
PREMIUM_MRP = 3000

# Words that say what a drink is, not which drink it is. Matching on these
# makes every scotch look like every other scotch.
GENERIC = {
    "whisky", "whiskey", "scotch", "single", "malt", "blended", "blend", "highland",
    "islay", "speyside", "cognac", "brandy", "vodka", "gin", "rum", "tequila", "wine",
    "liqueur", "liquor", "premium", "reserve", "special", "edition", "the", "and",
    "aged", "years", "year", "yo", "yrs", "vs", "vsop", "xo", "de", "of", "no",
    "bottled", "finish", "cask", "original", "classic", "select", "extra",
    "old", "bottle", "litre", "liter", "ltr", "pure", "fine",
}


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def brand_tokens(name: str) -> list[str]:
    """The words that identify which bottle this is.

    No truncation: the distinguishing word is often last (Honey, Fire, Apple),
    and cutting the list turned three flavoured whiskies into the plain one.
    Single characters go too, since an apostrophe leaves a stray "s".
    """
    return sorted({t for t in norm(name).split()
                   if t not in GENERIC and not t.isdigit() and len(t) > 1})


# Two shapes of listing that cannot be scaled to a 750ml price.
#
# Miniatures: Lagavulin 16 is listed at 75ml for Rs 9,730. Multiplying to
# 750ml gives Rs 97,300 and makes TASMAC look ten times cheaper. Small
# formats are priced per millilitre far above full bottles, so the scaling
# assumption simply does not hold.
#
# Multipacks: "Jack Daniels Twin Pack 2X1L" declares weight 1000 for two
# litres, halving the apparent unit price and inflating every Jack Daniel's
# multiple. Bundles like "Blue Label & Gold" are two different products.
SANE_ML = (500, 1500)
BUNDLE_MARKERS = ("twin", "pack", "2x", "3x", "gift", "set", "combo", "duo",
                  "trio", "&", " and ")


def is_bundle(name: str) -> bool:
    low = " " + (name or "").lower() + " "
    return any(m in low for m in BUNDLE_MARKERS)


GRADES = ("xo", "vsop", "vs", "extra")


def grade_of(name: str) -> str | None:
    """VS, VSOP and XO are different bottles at very different prices."""
    words = norm(name).split()
    for g in GRADES:
        if g in words:
            return g
    return None


def age_of(name: str) -> int | None:
    n = norm(name)
    m = re.search(r"\b(\d{1,2})\s*(?:yo|yr|yrs|year|years)\b", n)
    if m:
        return int(m.group(1))
    m = re.search(r"\baged\s+(\d{1,2})\b", n)
    return int(m.group(1)) if m else None


def volume_ml(text: str) -> int | None:
    n = norm(text)
    m = re.search(r"\b(\d{3,4})\s*ml\b", n)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d)\s*l\b", n)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r"\b(\d{2,3})\s*cl\b", n)
    return int(m.group(1)) * 10 if m else None


def search(term: str) -> list[tuple[str, float, int | None]]:
    # Bottle size is not in the product name ("Hibiki 12 YO"), it is the
    # weight field, in millilitres. Without it every match was rejected for
    # having no declared volume.
    query = ('{ products(search: "%s", pageSize: 8) { items { name '
             '... on SimpleProduct { weight } '
             'price_range { minimum_price { final_price { value } } } } } }' % term)
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        body = json.loads(urllib.request.urlopen(req, timeout=40).read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    items = ((body.get("data") or {}).get("products") or {}).get("items") or []
    out = []
    for i in items:
        try:
            price = float(i["price_range"]["minimum_price"]["final_price"]["value"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            ml = int(float(i.get("weight") or 0)) or None
        except (TypeError, ValueError):
            ml = None
        out.append((i["name"], price, ml))
    return out


def best_match(name: str, unit: str,
               candidates: list[tuple[str, float, int | None]]) -> dict | None:
    want_tokens = brand_tokens(name)
    if not want_tokens:
        return None                       # nothing distinctive to match on
    want_age = age_of(name)
    want_grade = grade_of(name)
    want_ml = volume_ml(unit) or volume_ml(name)
    if not want_ml:
        return None

    want_set = set(want_tokens)
    for cand_name, price, cand_declared_ml in candidates:
        # Equality, not containment. Containment let a candidate carry extra
        # words that make it a different and usually rarer bottle.
        if set(brand_tokens(cand_name)) != want_set:
            continue
        if age_of(cand_name) != want_age:
            continue                      # Dalmore 12 is not Dalmore 15
        if grade_of(cand_name) != want_grade:
            continue                      # Hennessy VS is not Hennessy XO
        if is_bundle(cand_name):
            continue
        cand_ml = cand_declared_ml or volume_ml(cand_name)
        if not cand_ml or price <= 0:
            continue
        if not (SANE_ML[0] <= cand_ml <= SANE_ML[1]):
            continue
        # compare per litre, so a 1L duty-free bottle is never read as 750ml
        return {"ref_name": cand_name, "ref_price": round(price),
                "ref_ml": cand_ml, "per_750": round(price / cand_ml * 750)}
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="stop after N bottles (for a trial run)")
    ap.add_argument("--min-mrp", type=int, default=PREMIUM_MRP)
    args = ap.parse_args()

    prem = [p for p in core.products() if (p["mrp"] or 0) > args.min_mrp]
    # One query per distinct bottle, not per pack size.
    by_key: dict = {}
    for p in prem:
        by_key.setdefault((tuple(brand_tokens(p["name"])), age_of(p["name"]),
                       grade_of(p["name"])), []).append(p)
    keys = list(by_key)
    if args.limit:
        keys = keys[:args.limit]
    print(f"{len(prem)} premium lines, {len(keys)} distinct bottles to look up", flush=True)

    refs, matched, skipped = {}, 0, 0
    for i, key in enumerate(keys, 1):
        group = by_key[key]
        tokens = key[0]
        if not tokens:
            skipped += len(group)
            continue
        hits = search(" ".join(tokens))
        for p in group:
            m = best_match(p["name"], p["unit"], hits)
            if m:
                tasmac_ml = volume_ml(p["unit"]) or 750
                tasmac_per_750 = p["mrp"] / tasmac_ml * 750
                mult = round(tasmac_per_750 / m["per_750"], 2)
                refs[str(p["product_id"])] = {
                    **m, "tasmac_mrp": p["mrp"], "tasmac_name": p["name"],
                    "multiple": mult,
                }
                matched += 1
        if i % 25 == 0:
            print(f"  {i}/{len(keys)}  matched {matched}", flush=True)
        time.sleep(DELAY)

    OUT.write_text(json.dumps({
        "source": "Delhi Duty Free (delhidutyfree.co.in), prices in INR",
        "fetched_on": datetime.now(IST).date().isoformat(),
        "method": ("Matched on brand tokens, exact age statement and declared volume; "
                   "compared per litre. Unmatched bottles are omitted rather than "
                   "guessed, so a missing entry means unknown, not fairly priced."),
        "prices": refs,
    }, separators=(",", ":")))

    print(f"\nmatched {matched} of {len(prem)} premium lines ({100*matched/max(1,len(prem)):.0f}%)")
    if refs:
        mults = sorted(v["multiple"] for v in refs.values())
        mid = mults[len(mults) // 2]
        print(f"median multiple vs duty free: {mid:.1f}x   range {mults[0]:.1f}x to {mults[-1]:.1f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
