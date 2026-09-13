#!/usr/bin/env python3
"""Read-only readiness check for the isolated Zerodha integration."""
from __future__ import annotations

import os
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
from app.models.db import database_name, validate_distinct_market_databases  # noqa: E402
from app.utils.time import mongo_client_kwargs  # noqa: E402
from data_sources import zerodha_client  # noqa: E402


MARKET_COLLECTIONS = (
    "gex_intraday", "gex_weekly", "gex_monthly_opex", "gex_rolling_21d"
)


def main() -> int:
    errors: list[str] = []
    primary_uri = BaseConfig.MONGO_URI
    zerodha_uri = BaseConfig.ZERODHA_MONGO_URI
    try:
        validate_distinct_market_databases(primary_uri, zerodha_uri)
        print(f"[ok] databases are distinct: {database_name(primary_uri)} / {database_name(zerodha_uri)}")
    except Exception as exc:
        errors.append(str(exc))

    try:
        primary_client = MongoClient(primary_uri, serverSelectionTimeoutMS=3000, **mongo_client_kwargs())
        primary_client.admin.command("ping")
        primary_db = primary_client.get_default_database()
        print("[ok] primary MongoDB reachable")
        symbol = symbols_config.find_by_symbol(primary_db, "NIFTY")
        if not symbol or symbol.get("provider") != "zerodha":
            errors.append("NIFTY Zerodha symbol configuration is missing")
        else:
            print("[ok] NIFTY symbol configured as paid Zerodha data")
    except Exception as exc:
        errors.append(f"primary MongoDB: {exc}")

    try:
        zerodha_client_db = MongoClient(
            zerodha_uri, serverSelectionTimeoutMS=3000, **mongo_client_kwargs()
        )
        zerodha_client_db.admin.command("ping")
        market_db = zerodha_client_db.get_default_database()
        missing = [name for name in MARKET_COLLECTIONS if not market_db[name].index_information()]
        if missing:
            errors.append(f"missing Zerodha indexes: {', '.join(missing)}")
        else:
            print("[ok] Zerodha MongoDB and market indexes ready")
    except Exception as exc:
        errors.append(f"Zerodha MongoDB: {exc}")

    cache = zerodha_client.cache_status()
    print(f"[{'ok' if cache['exists'] else 'info'}] instrument cache: {cache['path'] or 'not created yet'}")

    if os.environ.get("ZERODHA_API_KEY") and os.environ.get("ZERODHA_ACCESS_TOKEN"):
        try:
            profile = zerodha_client.check_credentials()
            print(f"[ok] Zerodha token valid for user {profile.get('user_id') or 'unknown'}")
        except Exception as exc:
            errors.append(f"Zerodha credentials: {exc}")
    else:
        errors.append("ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are not both configured")

    for error in errors:
        print(f"[failed] {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
