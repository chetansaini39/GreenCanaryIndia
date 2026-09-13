from bson import ObjectId
from app.utils.time import now_ct
from pymongo import ASCENDING, DESCENDING


COLLECTION = "admin_audit_log"


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index([("performed_at", DESCENDING)])
    col.create_index([("admin_user_id", ASCENDING), ("performed_at", DESCENDING)])
    col.create_index("action_type")


# ── Write ──────────────────────────────────────────────────────────────────

def insert_one(db, doc: dict) -> ObjectId:
    doc.setdefault("performed_at", now_ct())
    return db[COLLECTION].insert_one(doc).inserted_id


# ── Read ───────────────────────────────────────────────────────────────────

def find_recent(db, limit: int = 100) -> list[dict]:
    return list(
        db[COLLECTION].find().sort("performed_at", DESCENDING).limit(limit)
    )
