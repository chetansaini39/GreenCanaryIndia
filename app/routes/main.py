import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, render_template, session
from app.utils.decorators import login_required, role_required

_CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))
_STALE_THRESHOLD = timedelta(minutes=5)


def _parse_hhmm(value: str, default: "time") -> "time":
    from datetime import time as _time
    try:
        h, m = value.strip().split(":")
        return _time(int(h), int(m))
    except Exception:
        return default


def _is_market_hours_now() -> bool:
    from datetime import time as _time
    now = datetime.now(_CT)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    market_open  = _parse_hhmm(os.environ.get("MARKET_OPEN",  "08:45"), _time(8, 45))
    market_close = _parse_hhmm(os.environ.get("MARKET_CLOSE", "15:00"), _time(15, 0))
    return market_open <= now.time() <= market_close


def scheduler_is_healthy(db) -> tuple[bool, str]:
    """Return (healthy, reason). Unhealthy only when stale AND inside market hours."""
    from app.models import scheduler_heartbeat
    doc = scheduler_heartbeat.get_heartbeat(db)
    if doc is None:
        if _is_market_hours_now():
            return False, "no heartbeat document found — scheduler may never have started"
        return True, "no heartbeat yet (outside market hours)"

    last = doc.get("last_heartbeat_at")
    if last is None:
        return True, "heartbeat document exists but timestamp missing"

    # Ensure tz-aware comparison
    if last.tzinfo is None:
        last = last.replace(tzinfo=_CT)

    age = datetime.now(_CT) - last
    if age > _STALE_THRESHOLD and _is_market_hours_now():
        return False, f"last heartbeat {int(age.total_seconds() // 60)} min ago — scheduler may be down"
    return True, f"last heartbeat {int(age.total_seconds())} s ago"

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    # Pro price comes from config (PRO_PRICE env var), never hardcoded in the
    # template; ticker prices are loaded client-side from /api/prices.
    return render_template(
        "index.html",
        pro_price=current_app.config.get("PRO_PRICE"),
        now_year=datetime.now(_CT).year,
    )


@main_bp.route("/health")
@main_bp.route("/healthz")
def health():
    return {"status": "ok"}, 200


@main_bp.route("/healthz/scheduler")
def healthz_scheduler():
    from app.extensions import mongo
    healthy, reason = scheduler_is_healthy(mongo.db)
    status = "ok" if healthy else "degraded"
    code = 200 if healthy else 503
    return {"status": status, "detail": reason}, code


@main_bp.route("/this-week")
def this_week():
    """Public read-only GEX snapshot — no login required."""
    return render_template("this_week.html")


def _email_unverified() -> bool:
    """True when the logged-in user still needs to verify their email — read
    fresh from the DB so verifying in another tab clears the banner on reload."""
    from app.extensions import mongo
    from app.models import users as users_model
    user = users_model.find_by_id(mongo.db, session["user_id"])
    return bool(user and not user.get("email_verified", False))


@main_bp.route("/home")
@role_required("free", "paid")
def home():
    return render_template(
        "home.html",
        user_role=session.get("role"),
        email_unverified=_email_unverified(),
    )


@main_bp.route("/dashboard")
@role_required("free", "paid", "staff", "admin")
def dashboard():
    """User dashboard — full implementation in Module 04."""
    from app.extensions import mongo
    from app.models import platform_settings
    return render_template(
        "dashboard.html",
        user_role=session.get("role"),
        email_unverified=_email_unverified(),
        gex_0dte_interval_minutes=platform_settings.get_0dte_interval_minutes(mongo.db),
    )

