from datetime import datetime
from pymongo import ASCENDING, DESCENDING
from app.utils.time import now_ct


COLLECTION = "gex_rolling_21d"


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index([("symbol", ASCENDING), ("trade_date", ASCENDING)], unique=True)


# ── Write ──────────────────────────────────────────────────────────────────

def upsert(db, doc: dict):
    """Insert or replace the EOD snapshot for (symbol, trade_date).
    Full history is kept permanently; the 'rolling 21d' view is computed at
    query time by slicing the last 21 trade_dates."""
    doc.setdefault("created_at", now_ct())
    doc.setdefault("snapshot_type", "rolling_21d_eod")
    key = {"symbol": doc["symbol"], "trade_date": doc["trade_date"]}
    db[COLLECTION].replace_one(key, doc, upsert=True)


# ── Read ───────────────────────────────────────────────────────────────────

def find_last_n(db, symbol: str, n: int = 21) -> list[dict]:
    """Last n EOD snapshots for a symbol, oldest-first (for charting)."""
    rows = list(
        db[COLLECTION]
        .find({"symbol": symbol})
        .sort("trade_date", DESCENDING)
        .limit(n)
    )
    return list(reversed(rows))


def find_by_symbol_date_range(db, symbol: str, start: datetime, end: datetime) -> list[dict]:
    return list(
        db[COLLECTION]
        .find({"symbol": symbol, "trade_date": {"$gte": start, "$lte": end}})
        .sort("trade_date", ASCENDING)
    )


def find_by_symbol_date(db, symbol: str, trade_date: datetime) -> dict | None:
    return db[COLLECTION].find_one({"symbol": symbol, "trade_date": trade_date})


def find_last_n_before(db, symbol: str, end_date: datetime, n: int = 21) -> list[dict]:
    """Last n EOD snapshots on or before end_date, oldest-first."""
    rows = list(
        db[COLLECTION]
        .find({"symbol": symbol, "trade_date": {"$lte": end_date}})
        .sort("trade_date", DESCENDING)
        .limit(n)
    )
    return list(reversed(rows))
