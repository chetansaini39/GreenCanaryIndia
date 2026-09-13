"""Helpers for writing admin audit log entries."""

from app.extensions import mongo
from app.models import admin_audit_log as audit_model


def log_action(
    admin_user_id: str,
    action_type: str,
    target_user_id: str | None = None,
    old_value=None,
    new_value=None,
    detail: str = "",
) -> None:
    from bson import ObjectId

    doc = {
        "admin_user_id": ObjectId(admin_user_id),
        "action_type": action_type,
        "old_value": old_value,
        "new_value": new_value,
        "detail": detail,
    }
    if target_user_id:
        doc["target_user_id"] = ObjectId(target_user_id)
    audit_model.insert_one(mongo.db, doc)
