#!/usr/bin/env python3
"""Initialize the isolated Zerodha market database and NIFTY symbol config."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.config import BaseConfig  # noqa: E402
from app.models import ensure_zerodha_market_indexes, symbols_config  # noqa: E402
from app.models.db import database_name, validate_distinct_market_databases  # noqa: E402
from app.utils.time import mongo_client_kwargs  # noqa: E402


def main() -> int:
    primary_uri = BaseConfig.MONGO_URI
    zerodha_uri = BaseConfig.ZERODHA_MONGO_URI
    try:
        validate_distinct_market_databases(primary_uri, zerodha_uri)
        primary_client = MongoClient(primary_uri, serverSelectionTimeoutMS=5000, **mongo_client_kwargs())
        zerodha_client = MongoClient(zerodha_uri, serverSelectionTimeoutMS=5000, **mongo_client_kwargs())
        primary_client.admin.command("ping")
        zerodha_client.admin.command("ping")
        primary_db = primary_client.get_default_database()
        zerodha_db = zerodha_client.get_default_database()
        ensure_zerodha_market_indexes(zerodha_db)
        symbols_config.ensure_indexes(primary_db)
        symbols_config.upsert(
            primary_db, {**symbols_config.NIFTY_CONFIG, "active": True}
        )
    except Exception as exc:
        print(f"Zerodha initialization failed: {exc}", file=sys.stderr)
        return 1

    print("Zerodha storage initialized successfully.")
    print(f"  Primary application database: {database_name(primary_uri)}")
    print(f"  Zerodha market database:      {database_name(zerodha_uri)}")
    print("  NIFTY symbol configuration:   active (paid)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
