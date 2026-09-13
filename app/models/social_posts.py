from pymongo import ASCENDING, DESCENDING
from bson import ObjectId
from app.utils.time import now_ct

COLLECTION = "social_posts"
# 60 days — Module 10 retention policy
_TTL_SECONDS = 5_184_000


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index([("post_type", ASCENDING), ("created_at", DESCENDING)])
    col.create_index("status")
    col.create_index("created_at", expireAfterSeconds=_TTL_SECONDS)
    col.create_index(
        "schedule_key",
        unique=True,
        partialFilterExpression={"schedule_key": {"$type": "string"}},
    )


# ── Write ──────────────────────────────────────────────────────────────────

def insert_draft(db, doc: dict) -> ObjectId:
    doc.setdefault("status", "draft")
    doc.setdefault("platform", "twitter")
    doc.setdefault("tweet_ids", [])
    doc.setdefault("partial_tweet_ids", [])
    doc.setdefault("published_at", None)
    doc.setdefault("error_detail", None)
    doc.setdefault("validation_result", None)
    doc.setdefault("created_at", now_ct())
    return db[COLLECTION].insert_one(doc).inserted_id


def mark_published(db, post_id, tweet_ids: list[str]) -> None:
    db[COLLECTION].update_one(
        {"_id": ObjectId(post_id)},
        {"$set": {
            "status": "published",
            "tweet_ids": tweet_ids,
            "partial_tweet_ids": [],
            "published_at": now_ct(),
            "error_detail": None,
        }},
    )


def update_tweets(db, post_id: str, tweets: list[str], validation_result=None) -> None:
    fields = {"tweets": tweets}
    if validation_result is not None:
        fields["validation_result"] = validation_result
    db[COLLECTION].update_one(
        {"_id": ObjectId(post_id)},
        {"$set": fields},
    )


def mark_failed(db, post_id, error: str, partial_tweet_ids: list[str] | None = None) -> None:
    partial_tweet_ids = partial_tweet_ids or []
    db[COLLECTION].update_one(
        {"_id": ObjectId(post_id)},
        {"$set": {
            "status": "failed_partial" if partial_tweet_ids else "failed",
            "error_detail": error,
            "partial_tweet_ids": partial_tweet_ids,
            "tweet_ids": partial_tweet_ids,
        }},
    )


# ── Read ───────────────────────────────────────────────────────────────────

def find_by_id(db, post_id: str) -> dict | None:
    try:
        return db[COLLECTION].find_one({"_id": ObjectId(post_id)})
    except Exception:
        return None


def find_by_schedule_key(db, schedule_key: str) -> dict | None:
    return db[COLLECTION].find_one({"schedule_key": schedule_key})


def find_recent(db, limit: int = 50) -> list[dict]:
    return list(db[COLLECTION].find().sort("created_at", DESCENDING).limit(limit))


def delete_by_id(db, post_id: str) -> bool:
    try:
        result = db[COLLECTION].delete_one({"_id": ObjectId(post_id)})
        return result.deleted_count == 1
    except Exception:
        return False


def find_drafts(db) -> list[dict]:
    return list(
        db[COLLECTION]
        .find({"status": "draft"})
        .sort("created_at", DESCENDING)
    )
