from bson import ObjectId
from app.utils.time import now_ct


COLLECTION = "platform_settings"
GLOBAL_DOC_ID = "global"

# Only exact divisors of 60 produce clock-aligned intraday firing times
# (see docs/spec/02-data-pipeline-gex-collection.md, Granularity definitions).
VALID_0DTE_INTERVALS = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60)
VALID_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")

ZERODHA_DEFAULTS = {
    "zerodha_automation_enabled": False,
    "zerodha_ws_enabled": False,
    "zerodha_intraday_enabled": False,
    "zerodha_eod_enabled": False,
    "zerodha_monthly_enabled": False,
    "zerodha_intraday_start": "09:20",
    "zerodha_intraday_end": "15:10",
    "zerodha_intraday_interval_minutes": 15,
    "zerodha_eod_times": ["09:30", "15:10"],
    "zerodha_monthly_weekdays": ["mon", "fri"],
    "zerodha_monthly_time": "15:10",
    "zerodha_weekly_forward_expiries": 12,
    "zerodha_monthly_forward_cycles": 3,
    "zerodha_schedule_revision": 0,
}


def ensure_indexes(db):
    # Single-document collection — no indexes required beyond _id. Migrate the
    # prior EOD auto-publish default to the permanent review-only invariant.
    db[COLLECTION].update_one(
        {"_id": GLOBAL_DOC_ID},
        {"$set": {"social_eod_autopublish": False}},
    )


# ── Read ───────────────────────────────────────────────────────────────────

def get_settings(db) -> dict:
    """Return platform settings, creating defaults if the doc doesn't exist yet."""
    doc = db[COLLECTION].find_one({"_id": GLOBAL_DOC_ID})
    if doc:
        doc["social_eod_autopublish"] = False
        for key, value in ZERODHA_DEFAULTS.items():
            doc.setdefault(key, list(value) if isinstance(value, list) else value)
        return doc
    return {
        "_id": GLOBAL_DOC_ID,
        "registration_enabled": True,
        "social_premarket_autopublish": True,
        "social_eod_autopublish": False,
        "social_eow_autopublish": True,
        "gex_0dte_interval_minutes": 15,
        "gex_weekly_forward_weeks": 12,
        "gex_monthly_forward_cycles": 3,
        "gex_term_structure_strike_range_pct": 7,
        "updated_at": None,
        "updated_by": None,
        **ZERODHA_DEFAULTS,
    }


def is_registration_enabled(db) -> bool:
    return bool(get_settings(db).get("registration_enabled", True))


def is_autopublish_enabled(db, post_type: str) -> bool:
    """post_type: 'premarket' | 'eod' | 'eow'"""
    if post_type == "eod":
        return False
    key = f"social_{post_type}_autopublish"
    return bool(get_settings(db).get(key, True))


# ── Write ──────────────────────────────────────────────────────────────────

def _set_flag(db, field: str, value: bool, actor_id) -> None:
    db[COLLECTION].update_one(
        {"_id": GLOBAL_DOC_ID},
        {
            "$set": {
                field: value,
                "updated_at": now_ct(),
                "updated_by": ObjectId(actor_id),
            },
            "$setOnInsert": {"_id": GLOBAL_DOC_ID},
        },
        upsert=True,
    )


def set_registration_enabled(db, enabled: bool, admin_user_id) -> dict:
    _set_flag(db, "registration_enabled", enabled, admin_user_id)
    return get_settings(db)


def get_0dte_interval_minutes(db) -> int:
    return int(get_settings(db).get("gex_0dte_interval_minutes", 15))


def set_0dte_interval_minutes(db, minutes: int, actor_id) -> dict:
    if minutes not in VALID_0DTE_INTERVALS:
        raise ValueError(f"{minutes} is not a valid 0DTE interval (must be an exact divisor of 60)")
    _set_flag(db, "gex_0dte_interval_minutes", int(minutes), actor_id)
    return get_settings(db)


def get_weekly_forward_weeks(db) -> int:
    return int(get_settings(db).get("gex_weekly_forward_weeks", 12))


def get_monthly_forward_cycles(db) -> int:
    return int(get_settings(db).get("gex_monthly_forward_cycles", 3))


def set_forward_windows(db, weekly_forward_weeks: int, monthly_forward_cycles: int, actor_id) -> dict:
    db[COLLECTION].update_one(
        {"_id": GLOBAL_DOC_ID},
        {
            "$set": {
                "gex_weekly_forward_weeks": max(1, int(weekly_forward_weeks)),
                "gex_monthly_forward_cycles": max(1, int(monthly_forward_cycles)),
                "updated_at": now_ct(),
                "updated_by": ObjectId(actor_id),
            },
            "$setOnInsert": {"_id": GLOBAL_DOC_ID},
        },
        upsert=True,
    )
    return get_settings(db)


def get_term_structure_strike_range_pct(db):
    """Percentage band (±%) around spot used to filter strikes on the
    'GEX Next 3 Trading Days' term-structure card. Default 7."""
    return get_settings(db).get("gex_term_structure_strike_range_pct", 7)


def set_term_structure_strike_range_pct(db, pct, actor_id) -> dict:
    """Accepts any positive number (no divisor-of-60 restriction, unlike the
    scheduler interval)."""
    pct = float(pct)
    if pct <= 0:
        raise ValueError("strike range percent must be a positive number")
    # Store as int when it's a whole number, else keep the decimal.
    value = int(pct) if pct == int(pct) else pct
    db[COLLECTION].update_one(
        {"_id": GLOBAL_DOC_ID},
        {
            "$set": {
                "gex_term_structure_strike_range_pct": value,
                "updated_at": now_ct(),
                "updated_by": ObjectId(actor_id),
            },
            "$setOnInsert": {"_id": GLOBAL_DOC_ID},
        },
        upsert=True,
    )
    return get_settings(db)


def set_autopublish(db, post_type: str, enabled: bool, actor_id) -> None:
    """Toggle auto-publish for a social post type. Callable by staff or admin."""
    if post_type == "eod" and enabled:
        raise ValueError("EOD drafts always require staff/admin review")
    _set_flag(db, f"social_{post_type}_autopublish", enabled, actor_id)


def get_zerodha_settings(db) -> dict:
    settings = get_settings(db)
    return {key: settings.get(key, default) for key, default in ZERODHA_DEFAULTS.items()}


def _valid_time(value: str) -> str:
    from datetime import time

    text = str(value or "").strip()
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid time '{text}'; expected HH:MM") from exc
    if parsed.second or parsed.microsecond:
        raise ValueError("Schedule times must use minute precision")
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def set_zerodha_settings(db, values: dict, actor_id) -> dict:
    """Validate and persist the complete admin-controlled Zerodha schedule."""
    start = _valid_time(values.get("zerodha_intraday_start"))
    end = _valid_time(values.get("zerodha_intraday_end"))
    if start >= end:
        raise ValueError("Zerodha intraday start must be before end")

    interval = int(values.get("zerodha_intraday_interval_minutes", 15))
    if interval not in VALID_0DTE_INTERVALS:
        raise ValueError("Invalid Zerodha intraday interval")

    eod_times = sorted({_valid_time(value) for value in values.get("zerodha_eod_times", [])})
    if not eod_times:
        raise ValueError("At least one Zerodha EOD capture time is required")

    weekdays = list(dict.fromkeys(
        str(value).lower() for value in values.get("zerodha_monthly_weekdays", [])
    ))
    if not weekdays or any(value not in VALID_WEEKDAYS for value in weekdays):
        raise ValueError("Invalid Zerodha monthly weekday selection")

    weekly = int(values.get("zerodha_weekly_forward_expiries", 12))
    monthly = int(values.get("zerodha_monthly_forward_cycles", 3))
    if not 1 <= weekly <= 24 or not 1 <= monthly <= 12:
        raise ValueError("Zerodha forward windows must be 1–24 weekly and 1–12 monthly")

    update = {
        "zerodha_automation_enabled": bool(values.get("zerodha_automation_enabled")),
        "zerodha_ws_enabled": bool(values.get("zerodha_ws_enabled")),
        "zerodha_intraday_enabled": bool(values.get("zerodha_intraday_enabled")),
        "zerodha_eod_enabled": bool(values.get("zerodha_eod_enabled")),
        "zerodha_monthly_enabled": bool(values.get("zerodha_monthly_enabled")),
        "zerodha_intraday_start": start,
        "zerodha_intraday_end": end,
        "zerodha_intraday_interval_minutes": interval,
        "zerodha_eod_times": eod_times,
        "zerodha_monthly_weekdays": weekdays,
        "zerodha_monthly_time": _valid_time(values.get("zerodha_monthly_time")),
        "zerodha_weekly_forward_expiries": weekly,
        "zerodha_monthly_forward_cycles": monthly,
        "updated_at": now_ct(),
        "updated_by": ObjectId(str(actor_id)),
    }
    db[COLLECTION].update_one(
        {"_id": GLOBAL_DOC_ID},
        {
            "$set": update,
            "$inc": {"zerodha_schedule_revision": 1},
            "$setOnInsert": {"_id": GLOBAL_DOC_ID},
        },
        upsert=True,
    )
    return get_zerodha_settings(db)


def get_eod_theme_overrides(db) -> dict:
    value = get_settings(db).get("social_eod_theme") or {}
    return dict(value) if isinstance(value, dict) else {}


def set_eod_theme_overrides(db, theme: dict, actor_id) -> None:
    db[COLLECTION].update_one(
        {"_id": GLOBAL_DOC_ID},
        {
            "$set": {
                "social_eod_theme": dict(theme),
                "updated_at": now_ct(),
                "updated_by": ObjectId(str(actor_id)),
            },
            "$setOnInsert": {"_id": GLOBAL_DOC_ID},
        },
        upsert=True,
    )


def reset_eod_theme_overrides(db, actor_id) -> None:
    db[COLLECTION].update_one(
        {"_id": GLOBAL_DOC_ID},
        {
            "$unset": {"social_eod_theme": ""},
            "$set": {
                "updated_at": now_ct(),
                "updated_by": ObjectId(str(actor_id)),
            },
            "$setOnInsert": {"_id": GLOBAL_DOC_ID},
        },
        upsert=True,
    )
