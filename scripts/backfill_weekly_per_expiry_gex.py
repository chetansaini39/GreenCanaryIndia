#!/usr/bin/env python3
"""
Backfill per-expiry GEX on existing gex_weekly documents.

Historically ``run_eod()`` computed GEX once over the whole multi-expiry option
chain and wrote that identical blended result to every tracked Friday, so every
weekly tab showed the same numbers. Each doc still stores ``options_slice`` (the
full raw chain that was fetched), so we can re-slice it to the doc's OWN
``expiry_date`` and recompute the GEX in place — no re-fetch from Schwab needed.

Docs whose expiry has no matching contracts in their stored chain (the fabricated
far-out weeks) recompute to an empty/zero snapshot, which is correct — those
weeks genuinely had no contracts.

Idempotent: after a run each doc's ``options_slice`` holds only its own expiry,
so re-running recomputes the same values.

Usage:
  python scripts/backfill_weekly_per_expiry_gex.py                 # dry-run (default)
  python scripts/backfill_weekly_per_expiry_gex.py --apply         # write changes
  python scripts/backfill_weekly_per_expiry_gex.py --apply --symbol TSLA
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from pymongo import MongoClient

import gex_engine
from app.models import gex_weekly
from scheduler.jobs.gex_collection import _normalise_expiry, _slice_options_by_expiry

# Fields produced by gex_engine.compute() that describe the snapshot's GEX.
_DETAIL_FIELDS = (
    "net_gex",
    "call_wall",
    "put_wall",
    "gex_by_strike",
    "gex_by_expiration",
    "gex_by_expiry_strike",
    "options_slice",
)


def _recompute_doc(doc: dict) -> dict | None:
    """Return the corrected per-expiry GEX fields for one doc, or None if it
    can't be backfilled (no raw contracts / no spot)."""
    options = doc.get("options_slice") or []
    spot = doc.get("spot_price")
    if not options or spot is None:
        return None

    expiry = _normalise_expiry(doc.get("expiry_date"))
    sliced = _slice_options_by_expiry(options, expiry)
    result = gex_engine.compute(sliced, spot)
    return {field: result[field] for field in _DETAIL_FIELDS if field in result}


def _differs(doc: dict, patch: dict) -> bool:
    """True if the recomputed snapshot meaningfully differs from what's stored."""
    return (
        round(doc.get("net_gex") or 0, 4) != round(patch.get("net_gex") or 0, 4)
        or len(doc.get("gex_by_strike") or []) != len(patch.get("gex_by_strike") or [])
        or doc.get("call_wall") != patch.get("call_wall")
        or doc.get("put_wall") != patch.get("put_wall")
    )


def backfill(db, *, apply: bool, symbol: str | None, sample: int = 8) -> dict:
    stats = {"scanned": 0, "changed": 0, "skipped_no_slice": 0, "zeroed": 0}
    samples: list[str] = []

    coll = db[gex_weekly.COLLECTION]
    query = {"symbol": symbol.upper()} if symbol else {}
    for doc in coll.find(query):
        stats["scanned"] += 1
        patch = _recompute_doc(doc)
        if patch is None:
            stats["skipped_no_slice"] += 1
            continue
        if not (patch.get("gex_by_strike") or []):
            stats["zeroed"] += 1

        if not _differs(doc, patch):
            continue

        stats["changed"] += 1
        if len(samples) < sample:
            exp = _normalise_expiry(doc.get("expiry_date"))
            samples.append(
                f"    {doc.get('symbol')} exp={exp} "
                f"net_gex {doc.get('net_gex'):.4g} → {patch.get('net_gex'):.4g}  "
                f"strikes {len(doc.get('gex_by_strike') or [])} → {len(patch.get('gex_by_strike') or [])}"
            )

        if apply:
            coll.update_one({"_id": doc["_id"]}, {"$set": patch})

    stats["_samples"] = samples
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill per-expiry GEX on gex_weekly docs.")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run).")
    parser.add_argument("--symbol", help="Process one symbol only (e.g. TSLA).")
    args = parser.parse_args()

    client = MongoClient(os.environ["MONGO_URI"])
    db = client.get_default_database()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] Backfilling per-expiry GEX on gex_weekly …")
    if args.symbol:
        print(f"  symbol filter: {args.symbol.upper()}")

    stats = backfill(db, apply=args.apply, symbol=args.symbol)
    client.close()

    if stats["_samples"]:
        print("  sample corrections:")
        print("\n".join(stats["_samples"]))
    print(
        f"  scanned: {stats['scanned']}  "
        f"changed: {stats['changed']}  "
        f"zeroed (no contracts): {stats['zeroed']}  "
        f"skipped (no options_slice): {stats['skipped_no_slice']}"
    )
    if not args.apply and stats["changed"]:
        print("  Re-run with --apply to persist changes.")


if __name__ == "__main__":
    main()
