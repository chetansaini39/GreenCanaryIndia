#!/usr/bin/env python3
"""
Trim strike data on existing GEX snapshot documents to the storage bands.

Index (asset_type=index): ±20% of spot_price
Stock (asset_type=stock): ±50% of spot_price

Use after deploying strike filtering so historical Mongo documents shrink
without re-fetching option chains from Schwab/CBOE.

Usage:
  python scripts/retrim_gex_snapshots.py              # dry-run (default)
  python scripts/retrim_gex_snapshots.py --apply      # write changes
  python scripts/retrim_gex_snapshots.py --apply --symbol SPY
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

from app.models import gex_intraday, gex_monthly_opex, gex_rolling_5d, gex_weekly
from data_sources.strike_filter import trim_gex_result

_COLLECTIONS = (
    gex_intraday.COLLECTION,
    gex_weekly.COLLECTION,
    gex_monthly_opex.COLLECTION,
    gex_rolling_5d.COLLECTION,
)

_DETAIL_FIELDS = (
    "net_gex",
    "call_wall",
    "put_wall",
    "gex_by_strike",
    "gex_by_expiration",
    "gex_by_expiry_strike",
    "options_slice",
)


def _count_strikes(doc: dict) -> int:
    return len(doc.get("gex_by_strike") or [])


def _trim_doc(doc: dict) -> dict | None:
    spot = float(doc.get("spot_price") or 0.0)
    asset_type = doc.get("asset_type") or "index"
    if spot <= 0 or not doc.get("gex_by_strike"):
        return None

    gex_input = {field: doc.get(field) for field in _DETAIL_FIELDS if field in doc}
    before = len(gex_input.get("gex_by_strike") or [])
    trimmed = trim_gex_result(gex_input, spot, asset_type)
    after = len(trimmed.get("gex_by_strike") or [])
    if after == before:
        return None
    return trimmed


def retrim(db, *, apply: bool, symbol: str | None) -> dict:
    stats = {"scanned": 0, "updated": 0, "strikes_before": 0, "strikes_after": 0}

    for coll_name in _COLLECTIONS:
        coll = db[coll_name]
        query = {"symbol": symbol.upper()} if symbol else {}
        for doc in coll.find(query):
            stats["scanned"] += 1
            before = _count_strikes(doc)
            stats["strikes_before"] += before

            patch = _trim_doc(doc)
            if patch is None:
                stats["strikes_after"] += before
                continue

            after = len(patch.get("gex_by_strike") or [])
            stats["strikes_after"] += after
            stats["updated"] += 1

            if apply:
                coll.update_one({"_id": doc["_id"]}, {"$set": patch})

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Trim GEX snapshot strike ranges in MongoDB.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write trimmed documents (default is dry-run).",
    )
    parser.add_argument(
        "--symbol",
        help="Process one symbol only (e.g. SPY).",
    )
    args = parser.parse_args()

    client = MongoClient(os.environ["MONGO_URI"])
    db = client.get_default_database()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] Retrimming GEX snapshots …")
    if args.symbol:
        print(f"  symbol filter: {args.symbol.upper()}")

    stats = retrim(db, apply=args.apply, symbol=args.symbol)
    client.close()

    print(
        f"  scanned: {stats['scanned']}  "
        f"updated: {stats['updated']}  "
        f"strikes: {stats['strikes_before']} → {stats['strikes_after']}"
    )
    if not args.apply and stats["updated"]:
        print("  Re-run with --apply to persist changes.")


if __name__ == "__main__":
    main()
