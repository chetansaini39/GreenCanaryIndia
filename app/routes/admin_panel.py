import os
from datetime import datetime, timezone

from bson import ObjectId
from flask import (
    Blueprint, render_template, redirect, url_for,
    request, session, flash, abort, current_app,
)
from werkzeug.security import generate_password_hash

from app.extensions import mongo
from app.models import (
    users as users_model,
    symbols_config,
    pipeline_health,
    platform_settings,
    newsletter_subscribers,
    contact_submissions,
    api_keys as api_keys_model,
)
from app.models.contact_submissions import VALID_TYPES, VALID_STATUSES
from app.services import audit
from app.services.pipeline_rerun import available_jobs, rerun_job, backfill_forward_window
from scheduler.job_types import job_label
from app.utils.decorators import role_required

admin_bp = Blueprint("admin_panel", __name__)

VALID_ROLES = {"free", "paid", "staff", "admin"}


def _serialize_user(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "role": user.get("role", "free"),
        "subscription_status": user.get("subscription_status", "none"),
        "is_active": user.get("is_active", True),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
        "stripe_customer_id": user.get("stripe_customer_id"),
        "stripe_subscription_id": user.get("stripe_subscription_id"),
    }


def _valid_object_id(value: str) -> ObjectId | None:
    try:
        return ObjectId(value)
    except Exception:
        return None


# ── Landing pages (implemented here; main.py routes redirect for consistency) ──

@admin_bp.route("/admin")
@role_required("admin")
def admin_home():
    settings = platform_settings.get_settings(mongo.db)
    return render_template(
        "admin.html",
        registration_enabled=settings.get("registration_enabled", True),
        gex_0dte_interval_minutes=settings.get("gex_0dte_interval_minutes", 15),
        gex_weekly_forward_weeks=settings.get("gex_weekly_forward_weeks", 12),
        gex_monthly_forward_cycles=settings.get("gex_monthly_forward_cycles", 3),
        gex_term_structure_strike_range_pct=settings.get("gex_term_structure_strike_range_pct", 7),
    )


@admin_bp.route("/staff")
@role_required("staff", "admin")
def staff_home():
    return render_template("staff.html")


# ── User management (admin) ────────────────────────────────────────────────

@admin_bp.route("/admin/users")
@role_required("admin")
def admin_users():
    q = request.args.get("q", "").strip()
    role_filter = request.args.get("role", "").strip() or None
    users = users_model.search(mongo.db, query=q, role=role_filter)
    return render_template(
        "admin/users.html",
        users=[_serialize_user(u) for u in users],
        q=q,
        role_filter=role_filter or "",
        valid_roles=sorted(VALID_ROLES),
        current_user_id=session["user_id"],
    )


@admin_bp.route("/admin/users/<user_id>/role", methods=["POST"])
@role_required("admin")
def admin_change_role(user_id):
    if not _valid_object_id(user_id):
        abort(404)

    new_role = request.form.get("role", "").strip()
    if new_role not in VALID_ROLES:
        flash("Invalid role.", "danger")
        return redirect(url_for("admin_panel.admin_users", q=request.form.get("q", "")))

    # Self-demotion lockout protection
    if user_id == session["user_id"] and new_role != "admin":
        flash(
            "You cannot change your own role away from admin. "
            "Ask another admin to make this change.",
            "danger",
        )
        return redirect(url_for("admin_panel.admin_users"))

    user = users_model.find_by_id(mongo.db, user_id)
    if not user:
        abort(404)

    old_role = user.get("role")
    if old_role == new_role:
        flash("Role is already set to that value.", "info")
        return redirect(url_for("admin_panel.admin_users"))

    users_model.update_by_id(mongo.db, user_id, {"role": new_role})
    audit.log_action(
        session["user_id"],
        "role_change",
        target_user_id=user_id,
        old_value=old_role,
        new_value=new_role,
    )
    flash(f"Role updated to {new_role}.", "success")
    return redirect(url_for("admin_panel.admin_users", q=request.form.get("q", "")))


@admin_bp.route("/admin/users/<user_id>/deactivate", methods=["POST"])
@role_required("admin")
def admin_deactivate_user(user_id):
    if not _valid_object_id(user_id):
        abort(404)

    if user_id == session["user_id"]:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("admin_panel.admin_users"))

    user = users_model.find_by_id(mongo.db, user_id)
    if not user:
        abort(404)

    if not user.get("is_active", True):
        flash("Account is already deactivated.", "info")
        return redirect(url_for("admin_panel.admin_users"))

    users_model.set_active(mongo.db, user_id, False)
    audit.log_action(
        session["user_id"],
        "deactivate_user",
        target_user_id=user_id,
        old_value=True,
        new_value=False,
    )
    flash("Account deactivated.", "success")
    return redirect(url_for("admin_panel.admin_users"))


@admin_bp.route("/admin/users/<user_id>/reactivate", methods=["POST"])
@role_required("admin")
def admin_reactivate_user(user_id):
    if not _valid_object_id(user_id):
        abort(404)

    user = users_model.find_by_id(mongo.db, user_id)
    if not user:
        abort(404)

    if user.get("is_active", True):
        flash("Account is already active.", "info")
        return redirect(url_for("admin_panel.admin_users"))

    users_model.set_active(mongo.db, user_id, True)
    audit.log_action(
        session["user_id"],
        "reactivate_user",
        target_user_id=user_id,
        old_value=False,
        new_value=True,
    )
    flash("Account reactivated.", "success")
    return redirect(url_for("admin_panel.admin_users"))


@admin_bp.route("/admin/users/<user_id>/subscription", methods=["POST"])
@role_required("admin")
def admin_subscription_override(user_id):
    """Manual subscription comp/override without Stripe."""
    if not _valid_object_id(user_id):
        abort(404)

    new_role = request.form.get("role", "").strip()
    new_status = request.form.get("subscription_status", "").strip()
    valid_statuses = {"none", "active", "past_due", "canceled"}

    if new_role not in {"free", "paid"}:
        flash("Subscription override only applies to free or paid roles.", "danger")
        return redirect(url_for("admin_panel.admin_users"))
    if new_status not in valid_statuses:
        flash("Invalid subscription status.", "danger")
        return redirect(url_for("admin_panel.admin_users"))

    user = users_model.find_by_id(mongo.db, user_id)
    if not user:
        abort(404)

    if user.get("role") in ("staff", "admin"):
        flash("Cannot override subscription for staff or admin accounts.", "danger")
        return redirect(url_for("admin_panel.admin_users"))

    old = {
        "role": user.get("role"),
        "subscription_status": user.get("subscription_status"),
    }
    users_model.update_by_id(mongo.db, user_id, {
        "role": new_role,
        "subscription_status": new_status,
    })
    audit.log_action(
        session["user_id"],
        "subscription_override",
        target_user_id=user_id,
        old_value=old,
        new_value={"role": new_role, "subscription_status": new_status},
    )
    flash("Subscription override applied.", "success")
    return redirect(url_for("admin_panel.admin_users"))


def _serialize_api_key_row(db, key: dict) -> dict:
    owner = users_model.find_by_id(db, key["user_id"])
    return {
        "id": str(key["_id"]),
        "label": key.get("label", ""),
        "is_active": key.get("is_active", True),
        "created_at": key.get("created_at"),
        "last_used_at": key.get("last_used_at"),
        "owner_email": owner.get("email", "(deleted user)") if owner else "(deleted user)",
        "owner_role": owner.get("role") if owner else None,
    }


@admin_bp.route("/admin/api-keys")
@role_required("admin")
def admin_api_keys():
    q = request.args.get("q", "").strip().lower()
    db = mongo.db
    rows = [_serialize_api_key_row(db, k) for k in api_keys_model.find_all(db)]
    if q:
        rows = [r for r in rows if q in r["owner_email"].lower() or q in r["label"].lower()]
    return render_template("admin/api_keys.html", keys=rows, q=request.args.get("q", ""))


@admin_bp.route("/admin/api-keys/<key_id>/revoke", methods=["POST"])
@role_required("admin")
def admin_revoke_api_key(key_id):
    if not _valid_object_id(key_id):
        abort(404)
    revoked = api_keys_model.admin_revoke(mongo.db, key_id)
    flash("API key revoked." if revoked else "Key not found or already revoked.",
          "success" if revoked else "warning")
    return redirect(url_for("admin_panel.admin_api_keys", q=request.form.get("q", "")))


@admin_bp.route("/staff/api-keys")
@role_required("staff", "admin")
def staff_api_keys():
    q = request.args.get("q", "").strip().lower()
    db = mongo.db
    rows = [_serialize_api_key_row(db, k) for k in api_keys_model.find_all(db)]
    if q:
        rows = [r for r in rows if q in r["owner_email"].lower() or q in r["label"].lower()]
    return render_template("staff/api_keys.html", keys=rows, q=request.args.get("q", ""))


@admin_bp.route("/admin/staff/create", methods=["POST"])
@role_required("admin")
def admin_create_staff():
    email = request.form.get("email", "").strip().lower()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")

    if not email or not name or not password:
        flash("Email, name, and password are required.", "danger")
        return redirect(url_for("admin_panel.admin_users"))

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(url_for("admin_panel.admin_users"))

    if users_model.find_by_email(mongo.db, email):
        flash("An account with that email already exists.", "danger")
        return redirect(url_for("admin_panel.admin_users"))

    user_id = users_model.insert_one(mongo.db, {
        "email": email,
        "name": name,
        "password_hash": generate_password_hash(password),
        "google_id": None,
        "role": "staff",
        "subscription_status": "none",
        "is_active": True,
    })
    audit.log_action(
        session["user_id"],
        "create_staff",
        target_user_id=str(user_id),
        new_value={"email": email, "role": "staff"},
    )
    flash(f"Staff account created for {email}.", "success")
    return redirect(url_for("admin_panel.admin_users"))


# ── Symbol management (admin) ──────────────────────────────────────────────

@admin_bp.route("/admin/symbols", methods=["GET", "POST"])
@role_required("admin")
def admin_symbols():
    if request.method == "POST":
        action = request.form.get("action", "upsert")

        if action == "delete":
            symbol = request.form.get("symbol", "").upper().strip()
            if symbol:
                symbols_config.delete_by_symbol(mongo.db, symbol)
                audit.log_action(
                    session["user_id"],
                    "symbol_delete",
                    old_value=symbol,
                )
                flash(f"Symbol {symbol} removed.", "success")
            return redirect(url_for("admin_panel.admin_symbols"))

        symbol = request.form.get("symbol", "").upper().strip()
        asset_type = request.form.get("asset_type", "stock")
        tier = request.form.get("tier", "paid")
        weekly_expiry = request.form.get("weekly_expiry") == "on"
        active = request.form.get("active") == "on"

        if not symbol or asset_type not in ("index", "stock") or tier not in ("free", "paid"):
            flash("Invalid symbol configuration.", "danger")
            return redirect(url_for("admin_panel.admin_symbols"))

        existing = symbols_config.find_by_symbol(mongo.db, symbol)
        symbols_config.upsert(mongo.db, {
            **(existing or {}),
            "symbol": symbol,
            "asset_type": asset_type,
            "tier": tier,
            "weekly_expiry": weekly_expiry,
            "active": active,
        })
        audit.log_action(
            session["user_id"],
            "symbol_upsert",
            old_value=_serialize_symbol(existing) if existing else None,
            new_value={
                "symbol": symbol,
                "asset_type": asset_type,
                "tier": tier,
                "weekly_expiry": weekly_expiry,
                "active": active,
            },
        )
        flash(f"Symbol {symbol} saved.", "success")
        return redirect(url_for("admin_panel.admin_symbols"))

    symbols = symbols_config.find_all(mongo.db)
    return render_template("admin/symbols.html", symbols=symbols)


def _serialize_symbol(doc: dict | None) -> dict | None:
    if not doc:
        return None
    return {
        "symbol": doc.get("symbol"),
        "asset_type": doc.get("asset_type"),
        "tier": doc.get("tier"),
        "weekly_expiry": doc.get("weekly_expiry"),
        "active": doc.get("active"),
    }


# ── Pipeline monitoring ────────────────────────────────────────────────────

@admin_bp.route("/admin/pipeline")
@role_required("admin", "staff")
def admin_pipeline():
    from app.routes.main import scheduler_is_healthy
    logs = pipeline_health.find_recent(mongo.db, limit=100)
    is_admin = session.get("role") == "admin"
    scheduler_healthy, scheduler_detail = scheduler_is_healthy(mongo.db)
    zerodha_settings = platform_settings.get_zerodha_settings(mongo.db)
    from app.models.db import database_name, zerodha_market_db
    from data_sources import zerodha_client, zerodha_stream

    zerodha_status = {
        "primary_database": database_name(current_app.config["MONGO_URI"]),
        "market_database": database_name(current_app.config["ZERODHA_MONGO_URI"]),
        "market_database_ok": False,
        "credentials_configured": bool(
            os.environ.get("ZERODHA_API_KEY")
            and os.environ.get("ZERODHA_ACCESS_TOKEN")
        ),
        "cache": zerodha_client.cache_status(),
        "websocket": zerodha_stream.status(),
    }
    try:
        zerodha_market_db(current_app.config["ZERODHA_MONGO_URI"]).client.admin.command("ping")
        zerodha_status["market_database_ok"] = True
    except Exception as exc:
        zerodha_status["market_database_error"] = str(exc)
    return render_template(
        "admin/pipeline.html",
        logs=logs,
        jobs=available_jobs(),
        job_label=job_label,
        is_admin=is_admin,
        scheduler_healthy=scheduler_healthy,
        scheduler_detail=scheduler_detail,
        zerodha_settings=zerodha_settings,
        zerodha_status=zerodha_status,
    )


@admin_bp.route("/admin/pipeline/<job>/rerun", methods=["POST"])
@role_required("admin")
def admin_pipeline_rerun(job):
    symbol = request.form.get("symbol", "").strip().upper() or None
    date_str = request.form.get("date", "").strip()
    trade_date = None
    if date_str:
        try:
            trade_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date — use YYYY-MM-DD.", "danger")
            return redirect(url_for("admin_panel.admin_pipeline"))

    result = rerun_job(job, symbol=symbol, trade_date=trade_date)
    if result.get("ok"):
        detail = result.get("detail", job)
        count = result.get("symbols_processed")
        if count is not None:
            flash(f"Job triggered: {detail} ({count} symbol(s) processed)", "success")
        else:
            flash(f"Job triggered: {detail}", "success")
    else:
        flash(f"Rerun failed: {result.get('error', 'unknown error')}", "danger")
    return redirect(url_for("admin_panel.admin_pipeline"))


@admin_bp.route("/admin/pipeline/backfill-forward-window", methods=["POST"])
@role_required("admin")
def admin_backfill_forward_window():
    symbol = request.form.get("symbol", "").strip().upper() or None
    result = backfill_forward_window(symbol=symbol)
    if result.get("ok"):
        count = result.get("symbols_processed")
        flash(f"Backfill complete: {result.get('detail', '')} ({count} symbol(s))", "success")
    else:
        flash(f"Backfill failed: {result.get('error', 'unknown error')}", "danger")
    return redirect(url_for("admin_panel.admin_pipeline"))


@admin_bp.route("/admin/pipeline/zerodha/settings", methods=["POST"])
@role_required("admin")
def admin_zerodha_settings():
    values = {
        "zerodha_automation_enabled": request.form.get("zerodha_automation_enabled") == "on",
        "zerodha_ws_enabled": request.form.get("zerodha_ws_enabled") == "on",
        "zerodha_intraday_enabled": request.form.get("zerodha_intraday_enabled") == "on",
        "zerodha_eod_enabled": request.form.get("zerodha_eod_enabled") == "on",
        "zerodha_monthly_enabled": request.form.get("zerodha_monthly_enabled") == "on",
        "zerodha_intraday_start": request.form.get("zerodha_intraday_start", "09:20"),
        "zerodha_intraday_end": request.form.get("zerodha_intraday_end", "15:10"),
        "zerodha_intraday_interval_minutes": request.form.get(
            "zerodha_intraday_interval_minutes", "15"
        ),
        "zerodha_eod_times": [
            value.strip() for value in request.form.get("zerodha_eod_times", "").split(",")
            if value.strip()
        ],
        "zerodha_monthly_weekdays": request.form.getlist("zerodha_monthly_weekdays"),
        "zerodha_monthly_time": request.form.get("zerodha_monthly_time", "15:10"),
        "zerodha_weekly_forward_expiries": request.form.get(
            "zerodha_weekly_forward_expiries", "12"
        ),
        "zerodha_monthly_forward_cycles": request.form.get(
            "zerodha_monthly_forward_cycles", "3"
        ),
    }
    old = platform_settings.get_zerodha_settings(mongo.db)
    try:
        updated = platform_settings.set_zerodha_settings(
            mongo.db, values, session["user_id"]
        )
    except (TypeError, ValueError) as exc:
        flash(f"Zerodha settings were not saved: {exc}", "danger")
        return redirect(url_for("admin_panel.admin_pipeline"))
    audit.log_action(
        session["user_id"], "zerodha_settings_update", old_value=old, new_value=updated
    )
    flash("Zerodha schedule settings saved. The scheduler will apply them within two minutes.", "success")
    return redirect(url_for("admin_panel.admin_pipeline"))


@admin_bp.route("/admin/pipeline/zerodha/check", methods=["POST"])
@role_required("admin")
def admin_zerodha_check():
    from app.models.db import zerodha_market_db
    from data_sources import zerodha_client

    try:
        market_db = zerodha_market_db(current_app.config["ZERODHA_MONGO_URI"])
        market_db.client.admin.command("ping")
        profile = zerodha_client.check_credentials()
        detail = f"MongoDB ready; authenticated as {profile.get('user_id') or 'unknown'}"
        pipeline_health.insert_one(mongo.db, {
            "job_name": "zerodha_credentials_check",
            "status": "success",
            "detail": detail,
            "run_by": session["user_id"],
        })
        flash(f"Zerodha check passed: {detail}", "success")
    except Exception as exc:
        pipeline_health.insert_one(mongo.db, {
            "job_name": "zerodha_credentials_check",
            "status": "failed",
            "detail": str(exc),
            "run_by": session["user_id"],
        })
        flash(f"Zerodha check failed: {exc}", "danger")
    return redirect(url_for("admin_panel.admin_pipeline"))


@admin_bp.route("/admin/pipeline/zerodha/margins", methods=["POST"])
@role_required("admin")
def admin_zerodha_margins():
    import json
    from data_sources import zerodha_client

    try:
        orders = json.loads(request.form.get("orders_json", "[]"))
        if not isinstance(orders, list) or not 1 <= len(orders) <= 10:
            raise ValueError("Provide a JSON list containing 1–10 hypothetical orders")
        allowed = {
            "exchange", "tradingsymbol", "transaction_type", "variety",
            "product", "order_type", "quantity", "price", "trigger_price",
        }
        clean_orders = []
        for order in orders:
            if not isinstance(order, dict) or set(order) - allowed:
                raise ValueError("Each order must contain only supported margin fields")
            clean = dict(order)
            if clean.get("exchange") != "NFO":
                raise ValueError("Margin diagnostics accept NFO instruments only")
            if clean.get("transaction_type") not in {"BUY", "SELL"}:
                raise ValueError("transaction_type must be BUY or SELL")
            clean["quantity"] = int(clean.get("quantity", 0))
            if clean["quantity"] <= 0:
                raise ValueError("Order quantity must be positive")
            clean.setdefault("variety", "regular")
            clean.setdefault("product", "NRML")
            clean.setdefault("order_type", "MARKET")
            clean_orders.append(clean)
        result = zerodha_client.estimate_margins(clean_orders)
        rendered = json.dumps(result, default=str, separators=(",", ":"))
        flash(f"Margin estimate: {rendered[:1500]}", "success")
    except Exception as exc:
        flash(f"Margin estimate failed: {exc}", "danger")
    return redirect(url_for("admin_panel.admin_pipeline"))


# ── Platform settings ──────────────────────────────────────────────────────

@admin_bp.route("/admin/settings/forward-windows", methods=["POST"])
@role_required("admin")
def admin_set_forward_windows():
    try:
        weekly = int(request.form.get("gex_weekly_forward_weeks", 12))
        monthly = int(request.form.get("gex_monthly_forward_cycles", 3))
    except (TypeError, ValueError):
        flash("Invalid values — must be positive integers.", "danger")
        return redirect(url_for("admin_panel.admin_home"))

    if weekly < 1 or monthly < 1:
        flash("Forward window values must be at least 1.", "danger")
        return redirect(url_for("admin_panel.admin_home"))

    settings = platform_settings.get_settings(mongo.db)
    old_weekly = settings.get("gex_weekly_forward_weeks", 12)
    old_monthly = settings.get("gex_monthly_forward_cycles", 3)

    platform_settings.set_forward_windows(mongo.db, weekly, monthly, session["user_id"])
    audit.log_action(
        session["user_id"],
        "forward_windows_update",
        old_value={"weekly": old_weekly, "monthly": old_monthly},
        new_value={"weekly": weekly, "monthly": monthly},
    )
    flash(
        f"Forward windows updated: {weekly} weekly weeks, {monthly} monthly cycles. "
        "Takes effect on the scheduler's next run.",
        "success",
    )
    return redirect(url_for("admin_panel.admin_home"))


@admin_bp.route("/admin/settings/term-structure-range", methods=["POST"])
@role_required("admin")
def admin_set_term_structure_range():
    try:
        pct = float(request.form.get("gex_term_structure_strike_range_pct", 7))
    except (TypeError, ValueError):
        flash("Invalid strike range — must be a positive number.", "danger")
        return redirect(url_for("admin_panel.admin_home"))

    if pct <= 0:
        flash("Strike range percent must be greater than 0.", "danger")
        return redirect(url_for("admin_panel.admin_home"))

    settings = platform_settings.get_settings(mongo.db)
    old_value = settings.get("gex_term_structure_strike_range_pct", 7)

    updated = platform_settings.set_term_structure_strike_range_pct(
        mongo.db, pct, session["user_id"]
    )
    new_value = updated.get("gex_term_structure_strike_range_pct", pct)
    audit.log_action(
        session["user_id"],
        "term_structure_range_update",
        old_value=old_value,
        new_value=new_value,
    )
    flash(
        f"'GEX Next 3 Trading Days' strike range updated to ±{new_value}% of spot.",
        "success",
    )
    return redirect(url_for("admin_panel.admin_home"))


@admin_bp.route("/admin/settings/capture-intervals", methods=["POST"])
@role_required("admin")
def admin_set_capture_interval():
    try:
        minutes = int(request.form.get("gex_0dte_interval_minutes", 15))
    except (TypeError, ValueError):
        flash("Invalid capture interval — must be an integer.", "danger")
        return redirect(url_for("admin_panel.admin_home"))

    if minutes not in platform_settings.VALID_0DTE_INTERVALS:
        flash(
            f"Invalid capture interval: {minutes}. Must be one of "
            f"{', '.join(str(v) for v in platform_settings.VALID_0DTE_INTERVALS)} minutes "
            "(exact divisors of 60) so intraday snapshots stay clock-aligned.",
            "danger",
        )
        return redirect(url_for("admin_panel.admin_home"))

    settings = platform_settings.get_settings(mongo.db)
    old_value = settings.get("gex_0dte_interval_minutes", 15)

    platform_settings.set_0dte_interval_minutes(mongo.db, minutes, session["user_id"])
    audit.log_action(
        session["user_id"],
        "capture_interval_update",
        old_value=old_value,
        new_value=minutes,
    )
    flash(
        f"Intraday 0DTE capture interval updated to every {minutes} min. "
        "Takes effect on the scheduler's next run.",
        "success",
    )
    return redirect(url_for("admin_panel.admin_home"))


@admin_bp.route("/admin/settings/registration", methods=["POST"])
@role_required("admin")
def admin_toggle_registration():
    enabled = request.form.get("registration_enabled") == "on"
    settings = platform_settings.get_settings(mongo.db)
    old_value = settings.get("registration_enabled", True)

    if old_value == enabled:
        state = "OPEN" if enabled else "CLOSED"
        flash(f"Registration is already {state}.", "info")
        return redirect(url_for("admin_panel.admin_home"))

    platform_settings.set_registration_enabled(mongo.db, enabled, session["user_id"])
    audit.log_action(
        session["user_id"],
        "registration_toggle",
        old_value=old_value,
        new_value=enabled,
    )
    state = "OPEN" if enabled else "CLOSED"
    flash(f"Registration is now {state}.", "success")
    return redirect(url_for("admin_panel.admin_home"))


# ── Newsletter subscribers ─────────────────────────────────────────────────

@admin_bp.route("/admin/newsletter")
@role_required("admin")
def newsletter():
    subscribers = newsletter_subscribers.find_all_active(mongo.db)
    fmt = request.args.get("format", "html")
    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["email", "source", "subscribed_at"])
        writer.writeheader()
        for s in subscribers:
            writer.writerow({
                "email": s.get("email", ""),
                "source": s.get("source", ""),
                "subscribed_at": s.get("subscribed_at", ""),
            })
        from flask import Response
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=newsletter_subscribers.csv"},
        )
    return render_template("admin/newsletter.html", subscribers=subscribers)


# ── Contact submissions (admin + staff) ────────────────────────────────────

@admin_bp.app_context_processor
def _inject_contact_unread():
    """Expose the unread contact-submission count to templates for the sidenav
    badge — only computed for staff/admin sessions, so other pages pay nothing."""
    from flask import current_app
    if current_app.config.get("TESTING") or session.get("role") not in ("staff", "admin"):
        return {"contact_unread_count": 0}
    try:
        return {"contact_unread_count": contact_submissions.count_unread(mongo.db)}
    except Exception:
        return {"contact_unread_count": 0}


@admin_bp.route("/admin/contact")
@role_required("admin", "staff")
def admin_contact():
    type_filter = request.args.get("type", "").strip() or None
    status_filter = request.args.get("status", "").strip() or None
    if type_filter and type_filter not in VALID_TYPES:
        type_filter = None
    if status_filter and status_filter not in VALID_STATUSES:
        status_filter = None

    submissions = contact_submissions.search(
        mongo.db, type_=type_filter, status=status_filter
    )
    return render_template(
        "admin/contact.html",
        submissions=submissions,
        type_filter=type_filter or "",
        status_filter=status_filter or "",
        valid_types=VALID_TYPES,
        valid_statuses=VALID_STATUSES,
    )


@admin_bp.route("/admin/contact/<submission_id>/status", methods=["POST"])
@role_required("admin", "staff")
def admin_contact_set_status(submission_id):
    if not _valid_object_id(submission_id):
        abort(404)

    new_status = request.form.get("status", "").strip()
    if new_status not in VALID_STATUSES:
        flash("Invalid status.", "danger")
        return redirect(url_for("admin_panel.admin_contact"))

    if not contact_submissions.set_status(mongo.db, submission_id, new_status):
        abort(404)

    flash(f"Submission marked {new_status}.", "success")
    return redirect(url_for(
        "admin_panel.admin_contact",
        type=request.form.get("type", ""),
        status=request.form.get("status_filter", ""),
    ))


# ── Twitter integration test (admin only) ─────────────────────────────────

@admin_bp.route("/admin/integrations/twitter")
@role_required("admin")
def admin_twitter_integration():
    cred_logs = pipeline_health.find_by_job(mongo.db, "twitter_credentials_check", limit=10)
    post_logs = pipeline_health.find_by_job(mongo.db, "twitter_test_post", limit=10)
    stuck_tweet_id = request.args.get("stuck_tweet_id")
    return render_template(
        "admin/twitter_integration.html",
        cred_logs=cred_logs,
        post_logs=post_logs,
        stuck_tweet_id=stuck_tweet_id,
    )


@admin_bp.route("/admin/integrations/twitter/test-credentials", methods=["POST"])
@role_required("admin")
def admin_twitter_test_credentials():
    import os
    try:
        import tweepy
    except ImportError:
        flash("tweepy is not installed.", "danger")
        return redirect(url_for("admin_panel.admin_twitter_integration"))

    consumer_key    = os.environ.get("TWITTER_API_KEY") or os.environ.get("TWITTER_CONSUMER_KEY")
    consumer_secret = os.environ.get("TWITTER_API_SECRET") or os.environ.get("TWITTER_CONSUMER_SECRET")
    access_token    = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_secret   = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")
    bearer_token    = os.environ.get("TWITTER_BEARER_TOKEN")

    if not all([consumer_key, consumer_secret, access_token, access_secret]):
        pipeline_health.insert_one(mongo.db, {
            "job_name": "twitter_credentials_check",
            "status": "failed",
            "detail": "One or more Twitter credentials are not configured in environment.",
            "run_by": session.get("user_id"),
        })
        flash("Twitter credentials are not configured.", "danger")
        return redirect(url_for("admin_panel.admin_twitter_integration"))

    try:
        client = tweepy.Client(
            bearer_token=bearer_token,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        me = client.get_me()
        handle = me.data.username if me and me.data else "unknown"
        pipeline_health.insert_one(mongo.db, {
            "job_name": "twitter_credentials_check",
            "status": "success",
            "detail": f"Authenticated as @{handle}",
            "run_by": session.get("user_id"),
        })
        flash(f"Credentials valid — authenticated as @{handle}.", "success")
    except Exception as exc:
        pipeline_health.insert_one(mongo.db, {
            "job_name": "twitter_credentials_check",
            "status": "failed",
            "detail": str(exc),
            "run_by": session.get("user_id"),
        })
        flash(f"Credentials check failed: {exc}", "danger")

    return redirect(url_for("admin_panel.admin_twitter_integration"))


@admin_bp.route("/admin/integrations/twitter/test-post", methods=["POST"])
@role_required("admin")
def admin_twitter_test_post():
    import os
    from app.utils.time import now_ct
    from flask import current_app
    try:
        import tweepy
    except ImportError:
        flash("tweepy is not installed.", "danger")
        return redirect(url_for("admin_panel.admin_twitter_integration"))

    consumer_key    = os.environ.get("TWITTER_API_KEY") or os.environ.get("TWITTER_CONSUMER_KEY")
    consumer_secret = os.environ.get("TWITTER_API_SECRET") or os.environ.get("TWITTER_CONSUMER_SECRET")
    access_token    = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_secret   = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")
    bearer_token    = os.environ.get("TWITTER_BEARER_TOKEN")

    if not all([consumer_key, consumer_secret, access_token, access_secret]):
        pipeline_health.insert_one(mongo.db, {
            "job_name": "twitter_test_post",
            "status": "failed",
            "detail": "One or more Twitter credentials are not configured in environment.",
            "run_by": session.get("user_id"),
        })
        flash("Twitter credentials are not configured.", "danger")
        return redirect(url_for("admin_panel.admin_twitter_integration"))

    timestamp = now_ct().strftime("%Y-%m-%d %H:%M CT")
    tweet_text = (
        f"🧪 Connectivity test — automated check, please ignore. [{timestamp}]"
    )

    placeholder_path = os.path.join(
        current_app.static_folder, "img", "twitter_test_chart.png"
    )

    # Upload media via v1.1 API (v2 has no own media endpoint)
    auth = tweepy.OAuth1UserHandler(consumer_key, consumer_secret, access_token, access_secret)
    api_v1 = tweepy.API(auth)
    client = tweepy.Client(
        bearer_token=bearer_token,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )

    # --- Post step ---
    tweet_id = None
    try:
        media = api_v1.media_upload(filename=placeholder_path)
        response = client.create_tweet(text=tweet_text, media_ids=[media.media_id_string])
        tweet_id = response.data["id"]
    except Exception as exc:
        pipeline_health.insert_one(mongo.db, {
            "job_name": "twitter_test_post",
            "status": "failed",
            "detail": f"Post failed: {exc}",
            "run_by": session.get("user_id"),
        })
        flash(f"Test post failed: {exc}", "danger")
        return redirect(url_for("admin_panel.admin_twitter_integration"))

    # --- Delete step ---
    try:
        client.delete_tweet(tweet_id)
        pipeline_health.insert_one(mongo.db, {
            "job_name": "twitter_test_post",
            "status": "success",
            "detail": f"Test tweet posted and deleted successfully (id={tweet_id}).",
            "run_by": session.get("user_id"),
        })
        flash("Test tweet posted and immediately deleted — full pipeline verified.", "success")
    except Exception as exc:
        pipeline_health.insert_one(mongo.db, {
            "job_name": "twitter_test_post",
            "status": "post_ok_delete_failed",
            "detail": f"Tweet posted (id={tweet_id}) but delete failed: {exc}",
            "tweet_id": tweet_id,
            "run_by": session.get("user_id"),
        })
        return redirect(url_for(
            "admin_panel.admin_twitter_integration",
            stuck_tweet_id=tweet_id,
        ))

    return redirect(url_for("admin_panel.admin_twitter_integration"))



# ── Staff read-only user list ──────────────────────────────────────────────

@admin_bp.route("/staff/users")
@role_required("staff", "admin")
def staff_users():
    q = request.args.get("q", "").strip()
    users = users_model.search(mongo.db, query=q)
    return render_template(
        "staff/users.html",
        users=[_serialize_user(u) for u in users],
        q=q,
    )
