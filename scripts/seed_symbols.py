#!/usr/bin/env python3
"""
Seed the symbols_config collection with the default symbol catalog.

The dashboard API falls back to these defaults when symbols_config is empty,
but the GEX pipeline scheduler only reads from MongoDB — it will process 0
symbols until this collection is populated.

Usage:
    python scripts/seed_symbols.py
    python scripts/seed_symbols.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.config import BaseConfig  # noqa: E402
from app.models import symbols_config  # noqa: E402

# Keep in sync with app/routes/api.py _DEFAULT_CATALOG.
# US symbols (SPY/QQQ/TSLA/NVDA/SPX/NDX/IWM/AAPL/MSFT/AMZN/META/GOOGL) were
# removed with the Schwab/CBOE/yfinance pipeline in this India-only fork.
DEFAULT_SYMBOLS = [
    symbols_config.NIFTY_CONFIG,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed symbols_config with defaults.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print symbols that would be upserted without writing to MongoDB.",
    )
    args = parser.parse_args()

    if args.dry_run:
        for doc in DEFAULT_SYMBOLS:
            print(f"  {doc['symbol']:6} tier={doc['tier']:4} type={doc['asset_type']}")
        print(f"\n{len(DEFAULT_SYMBOLS)} symbols (dry run — no writes).")
        return 0

    client = MongoClient(BaseConfig.MONGO_URI)
    db_name = BaseConfig.MONGO_URI.rsplit("/", 1)[-1].split("?")[0] or "greencanaryindia"
    db = client[db_name]
    symbols_config.ensure_indexes(db)

    for doc in DEFAULT_SYMBOLS:
        symbols_config.upsert(db, {**doc, "active": True})

    active = symbols_config.find_all_active(db)
    print(f"Seeded {len(active)} active symbols into symbols_config.")
    for row in active:
        print(f"  {row['symbol']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
