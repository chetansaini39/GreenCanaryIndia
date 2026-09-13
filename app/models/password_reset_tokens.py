"""Time-limited, single-use password-reset tokens (Module 01 / Module 03).

Only the SHA-256 hash of the token is ever stored — the raw token appears
only in the emailed reset URL. Lookups on an incoming reset request hash the
raw token and match against `token_hash`.
"""
import hashlib

from bson import ObjectId
from pymongo import ASCENDING

from app.utils.time import now_ct


COLLECTION = "password_reset_tokens"

# TTL: purge documents 24h *after* they expire, so used/expired tokens don't
# accumulate. MongoDB deletes a doc once (expires_at + 86400s) is in the past.
_PURGE_AFTER_EXPIRY_SECONDS = 86_400


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index("token_hash", unique=True)
    col.create_index("user_id")
    col.create_index("expires_at", expireAfterSeconds=_PURGE_AFTER_EXPIRY_SECONDS)


def hash_token(raw_token: str) -> str:
    """SHA-256 hex digest of the raw token — what we store and look up on."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# ── Write ──────────────────────────────────────────────────────────────────

def create(db, user_id, token_hash: str, expires_at) -> ObjectId:
    result = db[COLLECTION].insert_one(
        {
            "user_id": ObjectId(user_id),
            "token_hash": token_hash,
            "expires_at": expires_at,
            "used": False,
            "created_at": now_ct(),
        }
    )
    return result.inserted_id


def mark_used(db, token_id) -> None:
    db[COLLECTION].update_one({"_id": ObjectId(token_id)}, {"$set": {"used": True}})


def invalidate_unused_for_user(db, user_id) -> None:
    """Burn every still-usable token for a user — called when a new one is
    issued or a reset completes, so old links stop working."""
    db[COLLECTION].update_many(
        {"user_id": ObjectId(user_id), "used": False},
        {"$set": {"used": True}},
    )


# ── Read ───────────────────────────────────────────────────────────────────

def find_active_by_hash(db, token_hash: str) -> dict | None:
    """Return the token doc only if it exists, is unused, and hasn't expired.
    A used-or-expired token returns None — indistinguishable to the caller,
    per the spec's 'invalid or expired' requirement."""
    doc = db[COLLECTION].find_one({"token_hash": token_hash, "used": False})
    if not doc:
        return None
    expires_at = doc.get("expires_at")
    if expires_at is not None and expires_at.tzinfo is None:
        # Defensive: compare tz-aware even if a naive value slipped in.
        from zoneinfo import ZoneInfo
        import os
        expires_at = expires_at.replace(tzinfo=ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago")))
    if expires_at is None or expires_at < now_ct():
        return None
    return doc
