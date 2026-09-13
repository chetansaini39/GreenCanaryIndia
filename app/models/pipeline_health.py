from bson import ObjectId
from app.utils.time import now_ct
from pymongo import ASCENDING, DESCENDING


COLLECTION = "pipeline_health"


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index([("job_name", ASCENDING), ("run_at", DESCENDING)])
    col.create_index("status")


# ── Write ──────────────────────────────────────────────────────────────────

def insert_one(db, doc: dict) -> ObjectId:
    doc.setdefault("run_at", now_ct())
    doc.setdefault("symbols_processed", 0)
    return db[COLLECTION].insert_one(doc).inserted_id


# ── Read ───────────────────────────────────────────────────────────────────

def find_recent(db, limit: int = 50) -> list[dict]:
    """Most recent runs across all jobs."""
    return list(db[COLLECTION].find().sort("run_at", DESCENDING).limit(limit))


def find_by_job(db, job_name: str, limit: int = 20) -> list[dict]:
    return list(
        db[COLLECTION]
        .find({"job_name": job_name})
        .sort("run_at", DESCENDING)
        .limit(limit)
    )


def find_by_status(db, status: str, limit: int = 100) -> list[dict]:
    return list(
        db[COLLECTION]
        .find({"status": status})
        .sort("run_at", DESCENDING)
        .limit(limit)
    )
