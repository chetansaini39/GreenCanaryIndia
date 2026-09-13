from app.utils.time import now_ct

COLLECTION = "scheduler_heartbeat"
GLOBAL_DOC_ID = "global"


def ensure_indexes(db):
    # Single-document collection — no indexes required beyond _id.
    pass


def write_heartbeat(db, process_started_at=None) -> None:
    """Upsert the single heartbeat document with the current Central Time."""
    fields = {"last_heartbeat_at": now_ct()}
    if process_started_at is not None:
        fields["process_started_at"] = process_started_at
    db[COLLECTION].update_one(
        {"_id": GLOBAL_DOC_ID},
        {"$set": fields, "$setOnInsert": {"_id": GLOBAL_DOC_ID}},
        upsert=True,
    )


def get_heartbeat(db) -> dict | None:
    return db[COLLECTION].find_one({"_id": GLOBAL_DOC_ID})
