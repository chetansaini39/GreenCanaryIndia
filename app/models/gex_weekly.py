from datetime import datetime
from pymongo import ASCENDING, DESCENDING
from app.utils.time import now_ct


COLLECTION = "gex_weekly"


def ensure_indexes(db):
    col = db[COLLECTION]
    # Drop legacy indexes that used week_of as the unique key component
    for old_idx in [
        [("symbol", ASCENDING), ("week_of", ASCENDING)],
        [("symbol", ASCENDING), ("week_of", ASCENDING), ("trade_date", ASCENDING)],
    ]:
        try:
            col.drop_index(old_idx)
        except Exception:
            pass
    # One doc per trading day per expiry date (the identity for which parallel track this is)
    col.create_index(
        [("symbol", ASCENDING), ("expiry_date", ASCENDING), ("trade_date", ASCENDING)],
        unique=True,
    )
    # Fast lookup: all trade_date points for one specific Friday's evolution
    col.create_index([("symbol", ASCENDING), ("expiry_date", ASCENDING)])
    # Fast lookup: today's snapshot across all N tracked weeks (forward-trend summary)
    col.create_index([("symbol", ASCENDING), ("trade_date", ASCENDING)])


# ── Write ──────────────────────────────────────────────────────────────────

def upsert(db, doc: dict):
    """Insert or replace the snapshot for (symbol, expiry_date, trade_date)."""
    doc.setdefault("created_at", now_ct())
    doc.setdefault("snapshot_type", "weekly")
    key = {
        "symbol": doc["symbol"],
        "expiry_date": doc["expiry_date"],
        "trade_date": doc["trade_date"],
    }
    db[COLLECTION].replace_one(key, doc, upsert=True)


# ── Read ───────────────────────────────────────────────────────────────────

def find_latest_for_expiry(db, symbol: str, expiry_date: datetime) -> dict | None:
    """Most recent trade_date snapshot for a specific Friday expiry."""
    return db[COLLECTION].find_one(
        {"symbol": symbol, "expiry_date": expiry_date},
        sort=[("trade_date", DESCENDING)],
    )


def find_latest_for_symbol(db, symbol: str) -> dict | None:
    """Latest trade date, choosing its nearest tracked actual expiry."""
    latest = db[COLLECTION].find_one(
        {"symbol": symbol}, sort=[("trade_date", DESCENDING)]
    )
    if not latest:
        return None
    return db[COLLECTION].find_one(
        {"symbol": symbol, "trade_date": latest["trade_date"]},
        sort=[("expiry_date", ASCENDING)],
    )


def find_by_symbol_expiry_trade_date(
    db, symbol: str, expiry_date: datetime, trade_date: datetime
) -> dict | None:
    """Exact lookup by (symbol, expiry_date, trade_date)."""
    return db[COLLECTION].find_one(
        {"symbol": symbol, "expiry_date": expiry_date, "trade_date": trade_date}
    )


def find_by_expiry(db, symbol: str, expiry_date: datetime) -> list[dict]:
    """All trade_date points for one Friday's evolution, oldest first."""
    return list(
        db[COLLECTION]
        .find({"symbol": symbol, "expiry_date": expiry_date})
        .sort("trade_date", ASCENDING)
    )


def find_by_symbol_trade_date(db, symbol: str, trade_date: datetime) -> dict | None:
    """Return the nearest-expiry snapshot for a given trade_date.

    With N weeks tracked in parallel there are multiple docs per trade_date.
    This returns the one with the smallest (nearest) expiry_date — matching
    the default 'current week' behaviour expected by the /api/gex?type=weekly
    endpoint when no expiry is specified.
    """
    return db[COLLECTION].find_one(
        {"symbol": symbol, "trade_date": trade_date},
        sort=[("expiry_date", ASCENDING)],
    )


def find_latest_for_week(db, symbol: str, week_of: datetime) -> dict | None:
    """Legacy alias — returns the latest trade_date snapshot for a given week_of Monday."""
    return db[COLLECTION].find_one(
        {"symbol": symbol, "week_of": week_of},
        sort=[("trade_date", DESCENDING)],
    )


def find_trend_for_trade_date(db, symbol: str, trade_date: datetime) -> list[dict]:
    """All expiry-week snapshots for a given trade_date, ordered by expiry_date ascending.

    Used by the /api/gex/weekly-trend endpoint — returns only summary fields
    (net_gex, call_wall, put_wall, expiry_date) not gex_by_strike.
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


def find_recent(db, symbol: str, n: int = 5) -> list[dict]:
    """Last n weekly snapshots for a symbol (one per expiry, most recent trade_date), newest first."""
    pipeline = [
        {"$match": {"symbol": symbol}},
        {"$sort": {"trade_date": DESCENDING}},
        {"$group": {"_id": "$expiry_date", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"expiry_date": DESCENDING}},
        {"$limit": n},
    ]
    return list(db[COLLECTION].aggregate(pipeline))


def find_latest_upcoming(
    db, symbol: str, from_expiry: datetime, n: int = 3
) -> list[dict]:
    """Latest stored snapshot for each upcoming actual expiry, nearest first."""
    pipeline = [
        {"$match": {"symbol": symbol, "expiry_date": {"$gte": from_expiry}}},
        {"$sort": {"trade_date": DESCENDING}},
        {"$group": {"_id": "$expiry_date", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"expiry_date": ASCENDING}},
        {"$limit": max(1, int(n))},
    ]
    return list(db[COLLECTION].aggregate(pipeline))


def find_by_symbol_date_range(db, symbol: str, start: datetime, end: datetime) -> list[dict]:
    """Latest snapshot per expiry_date for expiries falling in [start, end]."""
    pipeline = [
        {"$match": {"symbol": symbol, "expiry_date": {"$gte": start, "$lte": end}}},
        {"$sort": {"trade_date": DESCENDING}},
        {"$group": {"_id": "$expiry_date", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"expiry_date": ASCENDING}},
    ]
    return list(db[COLLECTION].aggregate(pipeline))
