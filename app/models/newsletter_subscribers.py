from app.utils.time import now_ct


COLLECTION = "newsletter_subscribers"


def ensure_indexes(db):
    db[COLLECTION].create_index("email", unique=True)


# ── Write ──────────────────────────────────────────────────────────────────

def upsert(db, email: str, source: str = "other") -> bool:
    """Subscribe an email address. Returns True if newly inserted, False if already existed."""
    result = db[COLLECTION].update_one(
        {"email": email},
        {
            "$setOnInsert": {
                "email": email,
                "source": source,
                "subscribed_at": now_ct(),
                "is_active": True,
            }
        },
        upsert=True,
    )
    return result.upserted_id is not None


def set_active(db, email: str, active: bool):
    db[COLLECTION].update_one({"email": email}, {"$set": {"is_active": active}})


# ── Read ───────────────────────────────────────────────────────────────────

def find_by_email(db, email: str) -> dict | None:
    return db[COLLECTION].find_one({"email": email})


def find_all_active(db) -> list[dict]:
    return list(db[COLLECTION].find({"is_active": True}).sort("subscribed_at", 1))
