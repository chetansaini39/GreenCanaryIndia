"""Manual data-refresh requests queued by the MCP server's trigger_data_refresh
tool (Module 11). Writing here — rather than calling the Schwab client
directly from the MCP process — keeps Schwab/token access isolated to the
scheduler process (see docs/spec/11-mcp-server.md, Open items).

The scheduler polls this collection on a short interval and runs the same
EOD job the admin panel's manual "rerun" button uses.
"""
from bson import ObjectId

from app.utils.time import now_ct


COLLECTION = "data_refresh_requests"


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index("status")
    col.create_index("requested_at")


def create(db, symbol: str, trade_date, requested_by) -> ObjectId:
    result = db[COLLECTION].insert_one(
        {
            "symbol": symbol,
            "trade_date": trade_date,
            "requested_by": ObjectId(requested_by),
            "status": "pending",
            "requested_at": now_ct(),
            "processed_at": None,
            "detail": None,
        }
    )
    return result.inserted_id


def find_pending(db, limit: int = 20) -> list[dict]:
    return list(
        db[COLLECTION].find({"status": "pending"}).sort("requested_at", 1).limit(limit)
    )


def mark_done(db, request_id, detail: str) -> None:
    db[COLLECTION].update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {"status": "done", "processed_at": now_ct(), "detail": detail}},
    )


def mark_failed(db, request_id, detail: str) -> None:
    db[COLLECTION].update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {"status": "failed", "processed_at": now_ct(), "detail": detail}},
    )
