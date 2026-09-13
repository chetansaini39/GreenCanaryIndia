"""API keys for MCP server access (Module 03 / Module 11).

Keys are stored as SHA-256 hashes — the raw key is only ever returned once,
at creation time, and never persisted.
"""
import hashlib
import secrets

from bson import ObjectId

from app.utils.time import now_ct


COLLECTION = "api_keys"

# Prefix makes a leaked key recognizable in logs/support tickets without
# revealing anything about the key itself.
_KEY_PREFIX = "rgex_"


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index("key_hash", unique=True)
    col.create_index("user_id")


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_raw_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


# ── Write ──────────────────────────────────────────────────────────────────

def create(db, user_id, label: str) -> tuple[ObjectId, str]:
    """Create a new key. Returns (doc_id, raw_key) — raw_key is shown once."""
    raw_key = generate_raw_key()
    result = db[COLLECTION].insert_one(
        {
            "user_id": ObjectId(user_id),
            "key_hash": hash_key(raw_key),
            "label": label,
            "is_active": True,
            "created_at": now_ct(),
            "last_used_at": None,
        }
    )
    return result.inserted_id, raw_key


def revoke(db, key_id, user_id) -> bool:
    """Deactivate a key. Scoped to user_id so a user can't revoke someone else's key."""
    result = db[COLLECTION].update_one(
        {"_id": ObjectId(key_id), "user_id": ObjectId(user_id)},
        {"$set": {"is_active": False}},
    )
    return result.modified_count > 0


def admin_revoke(db, key_id) -> bool:
    """Deactivate any key regardless of owner — admin oversight only."""
    result = db[COLLECTION].update_one(
        {"_id": ObjectId(key_id)},
        {"$set": {"is_active": False}},
    )
    return result.modified_count > 0


def touch_last_used(db, key_id) -> None:
    db[COLLECTION].update_one(
        {"_id": ObjectId(key_id)},
        {"$set": {"last_used_at": now_ct()}},
    )


# ── Read ───────────────────────────────────────────────────────────────────

def find_by_user(db, user_id) -> list[dict]:
    return list(
        db[COLLECTION].find({"user_id": ObjectId(user_id)}).sort("created_at", -1)
    )


def find_active_by_hash(db, key_hash: str) -> dict | None:
    return db[COLLECTION].find_one({"key_hash": key_hash, "is_active": True})


def find_all(db, limit: int = 500) -> list[dict]:
    """All keys across all users, newest first — admin/staff oversight view."""
    return list(db[COLLECTION].find().sort("created_at", -1).limit(limit))
