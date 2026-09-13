"""
Migration: rewrite UTC-midnight trade_date / week_of / expiry_date fields to
Central Time midnight.

Before this migration the scheduler wrote e.g.
    trade_date = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)
which PyMongo stores as 2026-06-22T00:00:00Z.

The API now queries with CT midnight:
    datetime(2026, 6, 22, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
which is stored as 2026-06-22T05:00:00Z (CDT, UTC-5).

Because the stored value (T00:00Z) != the query value (T05:00Z) no documents
were ever matched after the timezone fix, causing all granularity views to
show "data not available."

This script:
  1. Reads each affected document.
  2. Extracts the calendar date from the stored (naive) datetime — the date
     is correct even though the offset was wrong.
  3. Rewrites the field as a tz-aware CT midnight datetime.

Collections / fields migrated:
  gex_intraday   : trade_date
  gex_weekly     : week_of, expiry_date
  gex_rolling_5d : trade_date
  gex_monthly_opex: expiry_date  (0 docs currently, included for completeness)

Run:
    python scripts/migrate_trade_dates_to_ct.py [--dry-run]
"""
import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

CT = ZoneInfo("America/Chicago")


def ct_midnight(naive_or_aware: datetime) -> datetime:
    """Return a CT-aware midnight for the calendar date encoded in the argument."""
    d = naive_or_aware.date() if hasattr(naive_or_aware, "date") else naive_or_aware
    return datetime(d.year, d.month, d.day, tzinfo=CT)


def migrate_single_field(db, collection: str, field: str, dry_run: bool) -> int:
    """Migrate one field in one collection. Returns number of documents updated."""
    col = db[collection]
    # Only touch docs where the field exists and is a naive datetime
    # (naive = no tzinfo; PyMongo returns UTC-stored datetimes as naive by default)
    docs = list(col.find({field: {"$exists": True, "$type": "date"}}))
    updated = 0

    for doc in docs:
        old_val = doc[field]
        if old_val is None:
            continue
        # If already tz-aware and in CT, skip (shouldn't happen pre-migration,
        # but safe to guard against re-running the script)
        if getattr(old_val, "tzinfo", None) is not None:
            continue

        new_val = ct_midnight(old_val)
        if not dry_run:
            col.update_one({"_id": doc["_id"]}, {"$set": {field: new_val}})
        updated += 1

    return updated


def main():
    parser = argparse.ArgumentParser(description="Migrate trade_date fields from UTC midnight to CT midnight.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    args = parser.parse_args()

    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        print("ERROR: MONGO_URI is not set.", file=sys.stderr)
        sys.exit(1)

    client = MongoClient(mongo_uri)
    db = client.get_default_database()

    plan = [
        ("gex_intraday",    "trade_date"),
        ("gex_weekly",      "week_of"),
        ("gex_weekly",      "expiry_date"),
        ("gex_rolling_5d",  "trade_date"),
        ("gex_monthly_opex","expiry_date"),
    ]

    label = "[DRY RUN] " if args.dry_run else ""
    total = 0
    for collection, field in plan:
        n = migrate_single_field(db, collection, field, dry_run=args.dry_run)
        print(f"{label}{collection}.{field}: {n} document(s) {'would be ' if args.dry_run else ''}updated")
        total += n

    print(f"\n{label}Total: {total} document(s) {'would be ' if args.dry_run else ''}updated")
    if args.dry_run:
        print("Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
