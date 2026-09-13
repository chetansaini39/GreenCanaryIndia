from datetime import datetime
from pymongo import ASCENDING, DESCENDING
from app.utils.time import now_ct


COLLECTION = "gex_monthly_opex"


def ensure_indexes(db):
    col = db[COLLECTION]
    # Drop the old (symbol, month) unique index if it exists.
    try:
        col.drop_index([("symbol", ASCENDING), ("month", ASCENDING)])
    except Exception:
        pass
    col.create_index(
        [("symbol", ASCENDING), ("expiry_date", ASCENDING), ("trade_date", ASCENDING)],
        unique=True,
    )
    col.create_index([("symbol", ASCENDING), ("expiry_date", ASCENDING)])


# ── Write ──────────────────────────────────────────────────────────────────

def upsert(db, doc: dict):
    """Insert or replace the snapshot for (symbol, expiry_date, trade_date)."""
    doc.setdefault("created_at", now_ct())
    doc.setdefault("snapshot_type", "monthly_opex")
    key = {
        "symbol": doc["symbol"],
        "expiry_date": doc["expiry_date"],
        "trade_date": doc["trade_date"],
    }
    db[COLLECTION].replace_one(key, doc, upsert=True)


# ── Read ───────────────────────────────────────────────────────────────────

def find_by_symbol_trade_date(db, symbol: str, trade_date: datetime) -> dict | None:
    return db[COLLECTION].find_one({"symbol": symbol, "trade_date": trade_date})


def find_latest_for_symbol(db, symbol: str) -> dict | None:
    """Most recent captured snapshot by trade_date."""
    return db[COLLECTION].find_one(
        {"symbol": symbol},
        sort=[("trade_date", DESCENDING)],
    )


def find_by_expiry(db, symbol: str, expiry_date: datetime) -> list[dict]:
    """All trade_date points for one OPEX cycle, oldest first."""
    return list(
        db[COLLECTION]
        .find({"symbol": symbol, "expiry_date": expiry_date})
        .sort("trade_date", ASCENDING)
    )


def find_trend_for_trade_date(db, symbol: str, trade_date: datetime) -> list[dict]:
    """All OPEX-cycle snapshots for a given trade_date, ordered by expiry_date ascending.

    Used by /api/gex/monthly-trend — returns only summary fields, not gex_by_strike.
    """
    projection = {
        "symbol": 1,
        "expiry_date": 1,
        "trade_date": 1,
        "net_gex": 1,
        "call_wall": 1,
        "put_wall": 1,
        "created_at": 1,
    }
    return list(
        db[COLLECTION]
        .find({"symbol": symbol, "trade_date": trade_date}, projection)
        .sort("expiry_date", ASCENDING)
    )


def find_recent_cycles(db, symbol: str, n: int = 6) -> list[dict]:
    """Last n distinct OPEX expiries (most recent trade_date per expiry), newest first."""
    pipeline = [
        {"$match": {"symbol": symbol}},
        {"$sort": {"trade_date": DESCENDING}},
        {"$group": {"_id": "$expiry_date", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"expiry_date": DESCENDING}},
        {"$limit": n},
    ]
    return list(db[COLLECTION].aggregate(pipeline))
