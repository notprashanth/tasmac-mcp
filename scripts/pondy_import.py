#!/usr/bin/env python3
"""Rebuild data/pondy_prices.json from the Puducherry Excise MRP list.

Puducherry is 150km away, sells the same bottles under a different duty regime,
and publishes what they cost. That makes it the reference worth ranking TASMAC
against: not "is this fair by world prices" but "is this worth the drive".

    python3 scripts/pondy_import.py            # fetch, parse, match, write
    python3 scripts/pondy_import.py --dry-run  # report, write nothing

Needs `pdftotext` (poppler) and a network. This is a refresh job run by hand
when Puducherry republishes the list, not something the server ever calls.

Two things here were learned the hard way and are the reason this file exists
rather than a one-liner.

PARSING. The PDF is one wide table, a column per bottle size. Parsing the
character offsets of `pdftotext -layout` drifts across the 49 pages and files
prices under the neighbouring size, silently - and brand names that open with
digits (1943 BLACK & GOLD, 100 BARRELS, 3 COINS) parse as row numbers. So this
reads real coordinates from `pdftotext -bbox-layout` and assigns each number to
the size column whose x-centre is nearest.

MATCHING. The two governments name the same bottle differently, so pairing has
to be on words. Loose pairing does not fail loudly - it invents bargains. Early
attempts matched Macallan to "Old Oak Premium Whisky" at -93%, Meukow XO to
Meukow VSOP at -76%, and a Pinot Noir to "Old Monte Rum". A wrong pair reads
exactly like the headline finding. So a pair is only kept when the identifying
words are identical after stripping category and filler, the bottle size is
equal, and the grade matches exactly - an ungraded "King Brandy" is a different
and much cheaper bottle than "King Napoleon XO Brandy".
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tasmac_mcp import core

PDF_URL = ("https://exciseportal.py.gov.in/puduvaicalal1/pdf/InfoService/"
           "MRPofBrands10.06.2026.pdf")
OUT = Path(__file__).resolve().parent.parent / "tasmac_mcp" / "data" / "pondy_prices.json"
NS = "{http://www.w3.org/1999/xhtml}"

SIZES = {25, 50, 60, 90, 100, 180, 200, 250, 275, 280, 325, 330, 350, 355, 375,
         500, 650, 700, 720, 750, 1000, 1500, 2000, 3000, 4500, 15000, 30000, 50000}
# The serial column sits at x=56, the brand column at x=83. That gap is all that
# separates a row number from a brand that opens with digits.
SERIAL_X = 70

NOISE = {"THE", "OF", "AND", "IN", "PREMIUM", "SUPER", "FINE", "DELUXE", "GOLD",
         "ORIGINAL", "CLASSIC", "SPECIAL", "RESERVE", "BOTTLED", "PET", "BOTTLE",
         "EXPORT", "INDIAN", "INDIA", "IMPORTED", "YEARS", "YEAR", "OLD", "AGE",
         "SELECT", "CHOICE", "SMOOTH", "EXTRA", "STRONG", "NO"}
CATEGORY = {"WHISKY", "WHISKEY", "BRANDY", "RUM", "VODKA", "GIN", "BEER", "WINE",
            "COGNAC", "LAGER", "MALT", "SCOTCH", "SINGLE", "BLENDED", "LIQUEUR",
            "TEQUILA", "PORT", "CREAM", "GRAIN", "HIGHLAND", "ISLAY", "SPEYSIDE",
            "FLAVOURED", "FLAVORED", "DRY", "LIGHT", "MATURED"}
GRADES = {"XO", "VSOP", "VS", "XXX", "XXXX", "VVSOP", "NAPOLEON"}
KINDS = {"RUM": {"RUM"}, "WHISKY": {"WHISKY", "WHISKEY", "SCOTCH"},
         "BRANDY": {"BRANDY", "COGNAC"}, "VODKA": {"VODKA"}, "GIN": {"GIN"},
         "BEER": {"BEER", "LAGER"},
         "WINE": {"WINE", "MERLOT", "CHARDONNAY", "SAUVIGNON", "SHIRAZ", "NOIR"}}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def fetch(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT})
    import ssl
    # The portal serves an incomplete certificate chain. It is a public price
    # list on a government host and the alternative is not fetching it at all.
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=120, context=ctx) as r, open(dest, "wb") as fh:
        fh.write(r.read())


def words_by_page(xml: Path):
    tree = ET.parse(xml)
    for page in tree.iter(NS + "page"):
        out = []
        for w in page.iter(NS + "word"):
            text = (w.text or "").strip()
            if text:
                out.append({"text": text,
                            "x": (float(w.get("xMin")) + float(w.get("xMax"))) / 2,
                            "x0": float(w.get("xMin")),
                            "y": (float(w.get("yMin")) + float(w.get("yMax"))) / 2})
        yield out


def visual_lines(words, tol: float = 4.0):
    lines = []
    for w in sorted(words, key=lambda w: (w["y"], w["x"])):
        if lines and abs(w["y"] - lines[-1][0]["y"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(l, key=lambda w: w["x"]) for l in lines]


def size_columns(lines):
    for line in lines:
        nums = [w for w in line if w["text"].isdigit() and int(w["text"]) in SIZES]
        if len(nums) >= 15:
            return {int(w["text"]): w["x"] for w in nums}
    return None


def parse_pdf(pdf: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "mrp.xml"
        subprocess.run(["pdftotext", "-bbox-layout", str(pdf), str(xml)], check=True)
        rows, cols = [], None
        for words in words_by_page(xml):
            lines = visual_lines(words)
            # The header prints on page 1 only; the geometry is the same on all
            # 49 pages, so carry the columns forward.
            cols = size_columns(lines) or cols
            if not cols:
                continue
            edge = min(cols.values()) - 8
            pending: list[str] = []
            for line in lines:
                nums = [w for w in line if w["text"].isdigit() and w["x"] > edge]
                if len(nums) >= 15:
                    continue                       # the header row
                left = [w for w in line if w["x"] <= edge]
                if left and left[0]["text"].isdigit() and left[0]["x0"] < SERIAL_X:
                    left = left[1:]
                text = " ".join(w["text"] for w in left).strip()
                if not nums:
                    if text:
                        pending.append(text)       # a wrapped brand name
                    continue
                name = " ".join(pending + ([text] if text else [])).strip()
                pending = []
                if not name:
                    continue
                for w in nums:
                    ml, dist = min(((ml, abs(w["x"] - x)) for ml, x in cols.items()),
                                   key=lambda t: t[1])
                    price = int(w["text"])
                    if dist > 12 or price <= 0:    # 0 means registered, not priced
                        continue
                    rows.append({"brand": name, "ml": ml, "mrp": price})
        return rows


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def tokens(name: str) -> set:
    # Dots stripped BEFORE splitting: "X.O." and "V.S.O.P" are grades, and
    # splitting on the dots leaves single letters that get discarded, which is
    # what let XO match VSOP.
    flat = re.sub(r"\.", "", (name or "").upper())
    return {w for w in re.sub(r"[^A-Z0-9 ]", " ", flat).split()
            if len(w) > 1 and w not in NOISE}


def identity(toks): return toks - CATEGORY - GRADES
def grade(toks): return toks & GRADES


def kind(toks):
    for name, words in KINDS.items():
        if toks & words:
            return name
    return None


def ml_of(unit: str):
    m = re.search(r"(\d+)", unit or "")
    return int(m.group(1)) if m else None


def match(pondy: list[dict], catalogue: list[dict]) -> dict:
    by_size: dict[int, list] = {}
    for r in pondy:
        by_size.setdefault(r["ml"], []).append((tokens(r["brand"]), r))

    prices = {}
    for p in catalogue:
        pid, ml, mrp = p.get("product_id"), ml_of(p.get("unit")), p.get("mrp") or 0
        if pid is None or not ml or mrp <= 0 or ml not in by_size:
            continue
        ttoks = tokens(p["name"])
        tid, tgrade = identity(ttoks), grade(ttoks)
        if not tid:
            continue
        for ptoks, prow in by_size[ml]:
            if identity(ptoks) != tid or grade(ptoks) != tgrade:
                continue
            if kind(ttoks) and kind(ptoks) and kind(ttoks) != kind(ptoks):
                continue
            prices[str(pid)] = {
                "ref_name": prow["brand"].title(),
                "ref_price": prow["mrp"],
                "ref_ml": prow["ml"],
                "tasmac_mrp": mrp,
                "tasmac_name": p["name"],
                # >1 means TASMAC charges more, which is the usual direction.
                "multiple": round(mrp / prow["mrp"], 2),
            }
            break
    return prices


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--pdf", help="use a local PDF instead of fetching")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(args.pdf) if args.pdf else Path(tmp) / "mrp.pdf"
        if not args.pdf:
            print(f"fetching {PDF_URL}")
            fetch(PDF_URL, pdf)
        rows = parse_pdf(pdf)

    brands = {r["brand"] for r in rows}
    print(f"parsed   {len(rows)} prices across {len(brands)} brands")
    if len(brands) < 800:
        print("REFUSING: too few brands parsed, the layout has probably changed",
              file=sys.stderr)
        return 1

    prices = match(rows, core.products())
    cheaper = [v for v in prices.values() if v["multiple"] > 1]
    print(f"matched  {len(prices)} bottles to the TASMAC catalogue")
    print(f"cheaper in Pondicherry: {len(cheaper)} of {len(prices)}")

    payload = {
        "source": f"Puducherry Excise Department, {PDF_URL.rsplit('/', 1)[-1]}",
        "fetched_on": datetime.now(core.IST).date().isoformat(),
        "method": ("Parsed by word coordinates, matched to the TASMAC catalogue only "
                   "where the identifying words are identical, the bottle size is equal "
                   "and the grade agrees. Conservative on purpose: a wrong pair invents "
                   "a saving rather than reporting an error."),
        "prices": prices,
    }
    if args.dry_run:
        print("dry run, nothing written")
        return 0
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    print(f"wrote    {OUT.relative_to(OUT.parent.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
