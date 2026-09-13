"""
Public routes — no authentication required.

  GET  /snapshot/<symbol>/<date>   — single GEX snapshot landing page
  GET  /today                      — today's free-tier symbol set
  POST /newsletter/signup          — email capture
"""
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import (
    Blueprint, abort, current_app, jsonify, redirect, render_template,
    request, session, url_for,
)

from app.extensions import limiter, mongo
from app.models import (
    gex_intraday, gex_weekly, newsletter_subscribers,
    contact_submissions, users as users_model,
)
from app.models.contact_submissions import VALID_TYPES as CONTACT_TYPES
from app.services import email as email_service

CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))

public_bp = Blueprint("public", __name__)

FREE_SYMBOLS = ["SPY", "QQQ", "TSLA", "NVDA"]
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _serialize(obj):
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items() if k != "_id"}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# ── /snapshot/<symbol>/<date> ─────────────────────────────────────────────

@public_bp.route("/snapshot/<symbol>/<date_str>")
@limiter.limit("30 per hour")
def snapshot(symbol: str, date_str: str):
    symbol = symbol.upper().strip()

    try:
        req_date = date.fromisoformat(date_str)
    except ValueError:
        abort(404)

    # Fetch latest weekly snapshot for this symbol/date
    midnight_ct = datetime(req_date.year, req_date.month, req_date.day, tzinfo=CT)
    from datetime import timedelta
    mon = req_date - timedelta(days=req_date.weekday())
    week_dt = datetime(mon.year, mon.month, mon.day, tzinfo=CT)
    doc = gex_weekly.find_latest_for_week(mongo.db, symbol, week_dt)
    if doc is None:
        # Fall back to most recent intraday snapshot for that date
        docs = gex_intraday.find_by_symbol_date(mongo.db, symbol, midnight_ct)
        doc = docs[-1] if docs else None

    return render_template(
        "public/snapshot.html",
        symbol=symbol,
        date_str=date_str,
        snapshot=_serialize(doc) if doc else None,
    )


# ── /today ────────────────────────────────────────────────────────────────

@public_bp.route("/today")
@limiter.limit("30 per hour")
def today():
    today_date = date.today()
    midnight_ct = datetime(today_date.year, today_date.month, today_date.day, tzinfo=CT)

    snapshots = {}
    for sym in FREE_SYMBOLS:
        docs = gex_intraday.find_by_symbol_date(mongo.db, sym, midnight_ct)
        snapshots[sym] = _serialize(docs[-1]) if docs else None

    return render_template(
        "public/today.html",
        symbols=FREE_SYMBOLS,
        snapshots=snapshots,
        today_str=today_date.isoformat(),
    )


MIN_MESSAGE_LEN = 20


# ── GET /contact ──────────────────────────────────────────────────────────

@public_bp.route("/contact")
def contact():
    # Pre-fill the email field for logged-in users (saves a step).
    prefill_email = ""
    if session.get("user_id"):
        user = users_model.find_by_id(mongo.db, session["user_id"])
        if user:
            prefill_email = user.get("email", "")
    return render_template(
        "public/contact.html",
        prefill_email=prefill_email,
        now_year=datetime.now(CT).year,
    )


# ── POST /contact ─────────────────────────────────────────────────────────

@public_bp.route("/contact", methods=["POST"])
@limiter.limit("5 per hour")
def contact_submit():
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    type_ = (data.get("type") or "").strip().lower()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    message = (data.get("message") or "").strip()

    # Server-side validation — never rely on client-side alone (a direct API
    # call skips it).
    if type_ not in CONTACT_TYPES:
        return jsonify({"ok": False, "error": "Please choose a valid message type."}), 400
    if not name:
        return jsonify({"ok": False, "error": "Please enter your name."}), 400
    if not email or not _EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "Please enter a valid email address."}), 400
    if len(message) < MIN_MESSAGE_LEN:
        return jsonify({
            "ok": False,
            "error": f"Please enter a message of at least {MIN_MESSAGE_LEN} characters.",
        }), 400

    user_id = session.get("user_id")  # attach to the submission if logged in
    submission_id = contact_submissions.insert_one(
        mongo.db, type_, name, email, message, user_id=user_id
    )

    # Notify the team using the same email infrastructure as password reset.
    email_service.send_contact_notification_email(
        current_app.config.get("ADMIN_CONTACT_EMAIL"),
        {
            "_id": submission_id,
            "type": type_,
            "name": name,
            "email": email,
            "message": message,
            "user_id": user_id,
        },
    )

    return jsonify({"ok": True})


# ── POST /newsletter/signup ───────────────────────────────────────────────

@public_bp.route("/newsletter/signup", methods=["POST"])
@limiter.limit("5 per hour")
def newsletter_signup():
    if request.is_json:
        email = (request.json.get("email") or "").strip().lower()
        source = request.json.get("source") or "other"
    else:
        email = (request.form.get("email") or "").strip().lower()
        source = request.form.get("source") or "other"

    if not email or not _EMAIL_RE.match(email):
        if request.is_json:
            return jsonify({"error": "valid email address required"}), 400
        return render_template("public/newsletter_result.html", error="Please enter a valid email address."), 400

    # upsert — re-subscribing an existing email succeeds silently
    newsletter_subscribers.upsert(mongo.db, email, source=source)

    if request.is_json:
        return jsonify({"ok": True}), 200
    return render_template("public/newsletter_result.html", error=None)
