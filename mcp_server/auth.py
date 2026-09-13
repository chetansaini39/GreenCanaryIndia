"""API-key authentication, tier/role authorization, and rate limiting for the
MCP server (Module 11).

Every tool calls authorize() at the top — same pattern the spec asks for:
a single helper, not a parallel per-tool implementation. Tier enforcement
reuses app.routes.api's existing helpers rather than reimplementing the
free/paid rules.
"""
import time
from datetime import datetime, timezone

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from fastmcp.server.dependencies import get_http_headers

from app.models import api_keys as api_keys_model
from app.models import users as users_model
from mcp_server.db import get_db

# MCP-specific JSON-RPC error codes, per docs/spec/11-mcp-server.md.
ERR_UNAUTHORIZED = -32401  # bad/missing key
ERR_FORBIDDEN = -32403     # tier/role insufficient
ERR_NOT_FOUND = -32404     # no data for that symbol/date
ERR_RATE_LIMIT = -32429    # too many requests

RATE_LIMITS = {
    "free": 60,
    "paid": 300,
    "staff": 300,
    "admin": 1000,
}

_RATE_LIMIT_COLLECTION = "mcp_rate_limits"


def _raise(code: int, message: str):
    raise McpError(ErrorData(code=code, message=message))


def _extract_bearer_token() -> str | None:
    # get_http_headers() strips Authorization by default (it's excluded from
    # forwarding to downstream services) — opt back in explicitly.
    headers = get_http_headers(include={"authorization"})
    auth_header = headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    return auth_header[len("bearer "):].strip() or None


def check_named_rate_limit(db, key_hash: str, bucket_name: str, limit: int) -> None:
    """Fixed-window (1 hour) counter stored in Mongo so limits hold across
    workers, mirroring the app's Mongo-backed Flask-Limiter storage.

    `bucket_name` namespaces separate limits for the same key (e.g. the
    general per-role tool-call limit vs. trigger_data_refresh's own 5/hour cap).
    """
    hour_bucket = int(time.time() // 3600)
    doc_id = f"{key_hash}:{bucket_name}:{hour_bucket}"

    result = db[_RATE_LIMIT_COLLECTION].find_one_and_update(
        {"_id": doc_id},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
        },
        upsert=True,
        return_document=True,
    )
    if result["count"] > limit:
        _raise(ERR_RATE_LIMIT, f"rate limit exceeded — {limit} calls/hour")


def _check_rate_limit(db, key_hash: str, role: str) -> None:
    limit = RATE_LIMITS.get(role, RATE_LIMITS["free"])
    check_named_rate_limit(db, key_hash, "calls", limit)


def ensure_indexes(db):
    db[_RATE_LIMIT_COLLECTION].create_index("created_at", expireAfterSeconds=7200)


def authorize(required_tier: str | None = None, required_roles: tuple[str, ...] | None = None) -> dict:
    """Validate the request's Authorization: Bearer <key> header.

    required_tier="paid" requires the linked user to have full data access
    (paid/staff/admin) — same rule as the dashboard's paid-only endpoints.
    required_roles restricts to specific roles regardless of tier (e.g. admin-only actions).

    Returns the linked user document on success; raises McpError otherwise.
    """
    raw_key = _extract_bearer_token()
    if not raw_key:
        _raise(ERR_UNAUTHORIZED, "missing Authorization: Bearer <api_key> header")

    db = get_db()
    key_hash = api_keys_model.hash_key(raw_key)
    key_doc = api_keys_model.find_active_by_hash(db, key_hash)
    if key_doc is None:
        _raise(ERR_UNAUTHORIZED, "invalid or revoked API key")

    user = users_model.find_by_id(db, key_doc["user_id"])
    if user is None or not user.get("is_active"):
        _raise(ERR_UNAUTHORIZED, "the account linked to this key is inactive")

    role = user.get("role", "free")
    _check_rate_limit(db, key_hash, role)

    if required_roles and role not in required_roles:
        _raise(ERR_FORBIDDEN, f"this tool requires role in {required_roles}")

    if required_tier == "paid" and role not in ("paid", "staff", "admin"):
        _raise(ERR_FORBIDDEN, "this tool requires a paid plan")

    api_keys_model.touch_last_used(db, key_doc["_id"])
    return user
