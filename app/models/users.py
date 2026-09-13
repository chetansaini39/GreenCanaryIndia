from bson import ObjectId
from app.utils.time import now_ct
from pymongo import ASCENDING, DESCENDING


COLLECTION = "users"


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index("email", unique=True)
    col.create_index("google_id", sparse=True)
    col.create_index("role")


# ── Write ──────────────────────────────────────────────────────────────────

def insert_one(db, doc: dict) -> ObjectId:
    doc.setdefault("created_at", now_ct())
    doc.setdefault("last_login_at", None)
    doc.setdefault("is_active", True)
    doc.setdefault("role", "free")
    doc.setdefault("subscription_status", "none")
    doc.setdefault("stripe_customer_id", None)
    doc.setdefault("stripe_subscription_id", None)
    doc.setdefault("password_hash", None)
    doc.setdefault("google_id", None)
    doc.setdefault("email_verified", False)
    doc.setdefault("email_verification_token", None)
    result = db[COLLECTION].insert_one(doc)
    return result.inserted_id


def update_by_id(db, user_id, update: dict):
    db[COLLECTION].update_one({"_id": ObjectId(user_id)}, {"$set": update})


# ── Read ───────────────────────────────────────────────────────────────────

def find_by_id(db, user_id) -> dict | None:
    return db[COLLECTION].find_one({"_id": ObjectId(user_id)})


def find_by_email(db, email: str) -> dict | None:
    return db[COLLECTION].find_one({"email": email})


def find_by_google_id(db, google_id: str) -> dict | None:
    return db[COLLECTION].find_one({"google_id": google_id})


def find_by_verification_token(db, token: str) -> dict | None:
    return db[COLLECTION].find_one({"email_verification_token": token})


def find_by_stripe_customer_id(db, customer_id: str) -> dict | None:
    return db[COLLECTION].find_one({"stripe_customer_id": customer_id})


def find_by_role(db, role: str) -> list[dict]:
    return list(db[COLLECTION].find({"role": role}).sort("created_at", ASCENDING))


def search(
    db,
    query: str = "",
    role: str | None = None,
    active_only: bool | None = None,
    limit: int = 200,
) -> list[dict]:
    """List/search users for admin and staff panels."""
    filt: dict = {}
    if query:
        filt["$or"] = [
            {"email": {"$regex": query, "$options": "i"}},
            {"name": {"$regex": query, "$options": "i"}},
        ]
    if role:
        filt["role"] = role
    if active_only is True:
        filt["is_active"] = True
    elif active_only is False:
        filt["is_active"] = False
    return list(
        db[COLLECTION].find(filt).sort("created_at", DESCENDING).limit(limit)
    )


def set_email_verified(db, user_id) -> None:
    """Mark the email verified and clear the (now-spent) verification token."""
    db[COLLECTION].update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"email_verified": True, "email_verification_token": None}},
    )


def set_verification_token(db, user_id, token: str) -> None:
    """Store a freshly issued verification token (e.g. on resend)."""
    db[COLLECTION].update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"email_verification_token": token}},
    )


def set_password_hash(db, user_id, password_hash: str) -> None:
    db[COLLECTION].update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"password_hash": password_hash}},
    )


def set_active(db, user_id, active: bool):
    db[COLLECTION].update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_active": active}},
    )
