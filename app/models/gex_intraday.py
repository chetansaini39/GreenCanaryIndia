from datetime import datetime
from pymongo import ASCENDING, DESCENDING
from app.utils.time import now_ct


COLLECTION = "gex_intraday"


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index([("symbol", ASCENDING), ("trade_date", ASCENDING), ("timestamp", ASCENDING)])
    col.create_index([("symbol", ASCENDING), ("snapshot_type", ASCENDING)])


# ── Write ──────────────────────────────────────────────────────────────────

def insert_one(db, doc: dict):
    """Plain insert — intraday has no uniqueness constraint; multiple snapshots
    per symbol/date/time are valid (e.g. re-runs with a different source)."""
    doc.setdefault("created_at", now_ct())
    return db[COLLECTION].insert_one(doc).inserted_id


# ── Read ───────────────────────────────────────────────────────────────────

def find_by_symbol_date(db, symbol: str, trade_date: datetime) -> list[dict]:
    """All intraday snapshots for a symbol on a given trade date, oldest first."""
    return list(
        db[COLLECTION]
        .find({"symbol": symbol, "trade_date": trade_date})
        .sort("timestamp", ASCENDING)
    )


def find_by_symbol_date_range(
    db, symbol: str, start: datetime, end: datetime, snapshot_type: str | None = None
) -> list[dict]:
    query: dict = {"symbol": symbol, "trade_date": {"$gte": start, "$lte": end}}
    if snapshot_type:
        query["snapshot_type"] = snapshot_type
    return list(db[COLLECTION].find(query).sort("timestamp", ASCENDING))


def find_last_n_by_symbol_date(
    db, symbol: str, trade_date: datetime, snapshot_type: str, n: int = 10
) -> list[dict]:
    """Most recent N snapshots for a symbol/trade_date/type, timestamp descending
    (latest first) — used for the intraday time-box picker."""
    return list(
        db[COLLECTION]
        .find({"symbol": symbol, "trade_date": trade_date, "snapshot_type": snapshot_type})
        .sort("timestamp", DESCENDING)
        .limit(n)
    )


def find_latest(db, symbol: str, snapshot_type: str | None = None) -> dict | None:
    """Most recent snapshot for a symbol, optionally filtered by snapshot_type."""
    query: dict = {"symbol": symbol}
    if snapshot_type:
        query["snapshot_type"] = snapshot_type
    return db[COLLECTION].find_one(query, sort=[("timestamp", DESCENDING)])
