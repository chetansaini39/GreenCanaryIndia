from bson import ObjectId
from app.utils.time import now_ct
from pymongo import DESCENDING


COLLECTION = "subscriptions_log"


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index("stripe_event_id", unique=True)
    col.create_index("user_id")


# ── Write ──────────────────────────────────────────────────────────────────

def insert_one(db, doc: dict) -> ObjectId:
    """Append a Stripe event to the audit log.
    Returns None silently on duplicate stripe_event_id (idempotent webhook handling)."""
    doc.setdefault("processed_at", now_ct())
    try:
        return db[COLLECTION].insert_one(doc).inserted_id
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            return None
        raise


# ── Read ───────────────────────────────────────────────────────────────────

def find_by_stripe_event(db, stripe_event_id: str) -> dict | None:
    return db[COLLECTION].find_one({"stripe_event_id": stripe_event_id})


def find_by_user(db, user_id, limit: int = 50) -> list[dict]:
    return list(
        db[COLLECTION]
        .find({"user_id": ObjectId(user_id)})
        .sort("processed_at", DESCENDING)
        .limit(limit)
    )
