#!/usr/bin/env python3
"""Re-survey shops and regenerate tasmac_mcp/data/tiers.json.

TASMAC's "elite" flag is a licence class. Surveyed statewide it is about 40%
noise: 63 of 157 elite shops stock nothing above Rs 3,000. So the tier a user
sees is computed from what a shop actually carries, and this script is what
computes it.

Two modes, because the two jobs have different costs:

  --scope nightly   the shops already known to be premium or better, plus the
                    standard tier as a watch list. Around 80 shops, under a
                    minute. This is what the schedule runs.
  --scope elite     every elite-tagged shop statewide, about 200, a few
                    minutes. Catches shops that have moved up into the premium
                    segment since the last full pass. Worth running monthly.

Deliberately absent: a full 4,852-shop pass. That is a sustained daily scrape
of a government service that fell over under its own traffic on 2026-08-08.
Run it by hand, rarely, if the assumption below ever needs re-testing.

The assumption: elite is a superset of premium. In Chennai, where every one of
375 shops was checked, no shop with 40+ premium lines lacked the elite tag.
That is one district's evidence, so it is written down rather than forgotten.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TASMAC_NO_HISTORY", "1")
os.environ.setdefault("TASMAC_CACHE_TTL", "0")

from tasmac_mcp import core

PREMIUM_MRP, LUXURY_MRP = 3000, 10000
DELAY = 0.15
SLOW, SLOW_STREAK = 8.0, 8
IST = timezone(timedelta(hours=5, minutes=30))
OUT = Path(__file__).resolve().parent.parent / "tasmac_mcp" / "data" / "tiers.json"


def tier_of(premium: int, luxury: int) -> str:
    if premium >= 120 and luxury >= 8:
        return "flagship"
    if premium >= 40:
        return "premium"
    if premium >= 5:
        return "standard"
    return "basic"


def shops_to_survey(scope: str, existing: dict) -> list[str]:
    if scope == "nightly":
        keep = {"flagship", "premium", "standard"}
        return [s for s, v in existing.items() if v.get("tier") in keep]
    seen, out = set(), []
    for t in core.taluks():
        try:
            for s in core.shops_in_taluk(t["code"]):
                if s["elite"] and s["shop"] not in seen:
                    seen.add(s["shop"])
                    out.append(s["shop"])
        except RuntimeError:
            continue
        time.sleep(0.05)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", choices=["nightly", "elite"], default="nightly")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doc = json.loads(OUT.read_text()) if OUT.exists() else {"shops": {}}
    existing = doc.get("shops", {})
    targets = shops_to_survey(args.scope, existing)
    print(f"scope={args.scope}: {len(targets)} shops", flush=True)

    updated, failed, slow_streak = 0, 0, 0
    started = time.time()

    for i, shop in enumerate(targets, 1):
        t0 = time.time()
        try:
            data = core.fetch_shop(shop, write_history=False)
            items = [it for it in data["items"] if it["stock"] > 0]
        except LookupError:
            existing[shop] = {**existing.get(shop, {"shop": shop}), "tier": "no_stock"}
            failed += 1
            time.sleep(DELAY)
            continue
        except RuntimeError as e:
            print(f"  {shop}: {str(e)[:70]}", flush=True)
            failed += 1
            time.sleep(DELAY)
            continue

        if time.time() - t0 > SLOW:
            slow_streak += 1
            if slow_streak >= SLOW_STREAK:
                print(f"Backing off at {i}/{len(targets)}: the endpoint is degraded. "
                      "Keeping what was collected.", flush=True)
                break
        else:
            slow_streak = 0

        priced = [it for it in items if it["mrp"]]
        premium = [it for it in priced if it["mrp"] > PREMIUM_MRP]
        luxury = sum(1 for it in priced if it["mrp"] > LUXURY_MRP)
        prev = existing.get(shop, {})
        existing[shop] = {
            "shop": shop,
            "tier": tier_of(len(premium), luxury),
            "premium": len(premium),
            "luxury": luxury,
            "lines": len(items),
            "max_mrp": max((it["mrp"] for it in priced), default=0),
            "district": prev.get("district") or data.get("district", ""),
            "taluka": prev.get("taluka") or data.get("taluka", ""),
            "marquee": [it["name"][:42] for it in sorted(premium, key=lambda x: -x["mrp"])[:3]],
        }
        updated += 1
        if i % 25 == 0:
            print(f"  {i}/{len(targets)}  {time.time()-started:.0f}s", flush=True)
        time.sleep(DELAY)

    doc.update({
        "surveyed_on": datetime.now(IST).date().isoformat(),
        "thresholds": {"premium_mrp": PREMIUM_MRP, "luxury_mrp": LUXURY_MRP,
                       "flagship": "premium>=120 and luxury>=8",
                       "premium": "premium>=40", "standard": "premium>=5"},
        "coverage": ("Every shop in Chennai district, plus every elite-tagged shop "
                     "statewide. Non-elite shops outside Chennai were not surveyed and "
                     "carry no tier: absence here means unknown, not basic."),
        "shops": dict(sorted(existing.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)),
    })

    if args.dry_run:
        print(f"dry run: would update {updated} shops, {failed} failed")
        return 0

    OUT.write_text(json.dumps(doc, separators=(",", ":")))
    counts: dict = {}
    for v in existing.values():
        counts[v.get("tier", "?")] = counts.get(v.get("tier", "?"), 0) + 1
    print(f"\nupdated {updated} shops in {(time.time()-started)/60:.1f}m, {failed} failed")
    print("  " + "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
