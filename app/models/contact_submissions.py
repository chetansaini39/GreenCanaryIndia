"""Contact / feedback form submissions (Module 01 / Module 03).

Persistent record of every `/contact` submission. An email notification is
sent to ADMIN_CONTACT_EMAIL on arrival (see app/services/email.py); this
collection is the durable store the admin panel reads back (Module 05).
"""
from bson import ObjectId
from pymongo import DESCENDING

from app.utils.time import now_ct


COLLECTION = "contact_submissions"

VALID_TYPES = ("bug", "feature", "data", "general")
VALID_STATUSES = ("new", "read", "resolved")


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index("submitted_at", name="submitted_at_desc")
    col.create_index("status")
    col.create_index("user_id")


# ── Write ──────────────────────────────────────────────────────────────────

def insert_one(db, type_: str, name: str, email: str, message: str,
               user_id=None) -> ObjectId:
    doc = {
        "type": type_,
        "name": name,
        "email": email,
        "message": message,
        "user_id": ObjectId(user_id) if user_id else None,
        "submitted_at": now_ct(),
        "status": "new",
        "notes": None,
    }
    return db[COLLECTION].insert_one(doc).inserted_id


def set_status(db, submission_id, status: str) -> bool:
    """Mark a submission read/resolved. Returns True if a document was updated."""
    result = db[COLLECTION].update_one(
        {"_id": ObjectId(submission_id)},
        {"$set": {"status": status}},
    )
    return result.matched_count > 0


# ── Read ───────────────────────────────────────────────────────────────────

def search(db, type_: str | None = None, status: str | None = None,
           limit: int = 200) -> list[dict]:
    """Contact submissions, newest first, optionally filtered by type/status."""
    filt: dict = {}
    if type_:
        filt["type"] = type_
    if status:
        filt["status"] = status
    return list(
        db[COLLECTION].find(filt).sort("submitted_at", DESCENDING).limit(limit)
    )


def count_unread(db) -> int:
    return db[COLLECTION].count_documents({"status": "new"})
