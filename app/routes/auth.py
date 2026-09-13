import secrets
from datetime import timedelta

from flask import (
    Blueprint, render_template, redirect, url_for,
    request, session, flash, jsonify, current_app,
)
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import mongo, oauth, limiter
from app.models import users as users_model
from app.models import platform_settings as platform_settings_model
from app.models import password_reset_tokens as reset_tokens_model
from app.models import api_keys as api_keys_model
from app.services import email as email_service
from app.utils.time import now_ct
from app.utils.decorators import login_required

auth_bp = Blueprint("auth", __name__)


# ── Verification helpers ────────────────────────────────────────────────────

def _new_token() -> str:
    """256 bits of URL-safe entropy — used for both verification and reset tokens."""
    return secrets.token_urlsafe(32)


def _send_verification(user: dict, token: str) -> None:
    """Build the absolute verify link and dispatch the email."""
    verify_url = url_for("auth.verify_email", token=token, _external=True)
    email_service.send_verification_email(user["email"], user.get("name", ""), verify_url)


# ── Session helpers ────────────────────────────────────────────────────────

def _establish_session(user: dict) -> None:
    """Populate the server-side session from a user document."""
    session.clear()
    session["user_id"] = str(user["_id"])
    session["role"] = user["role"]
    session["subscription_status"] = user["subscription_status"]


def _role_redirect(next_url: str | None = None):
    """Redirect to the correct landing page for the current session role,
    honouring a `next` param if it is a safe relative URL."""
    if next_url and next_url.startswith("/") and next_url not in ("/login", "/register"):
        return redirect(next_url)
    role = session.get("role")
    if role == "admin":
        return redirect(url_for("admin_panel.admin_home"))
    if role == "staff":
        return redirect(url_for("admin_panel.staff_home"))
    return redirect(url_for("main.home"))


# ── /login ─────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute", methods=["POST"])
def login():
    if "user_id" in session:
        return _role_redirect()

    next_url = request.args.get("next") or request.form.get("next", "")

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = users_model.find_by_email(mongo.db, email)

        if not user:
            flash("No account found with that email address.", "danger")
            return render_template("auth/login.html", next=next_url)

        # Account was created via Google — no password set
        if not user.get("password_hash"):
            flash(
                "This account uses Google sign-in. Please click 'Continue with Google'.",
                "warning",
            )
            return render_template("auth/login.html", next=next_url)

        if not check_password_hash(user["password_hash"], password):
            flash("Incorrect password. Please try again.", "danger")
            return render_template("auth/login.html", next=next_url)

        if not user.get("is_active"):
            flash("Your account has been deactivated. Please contact support.", "danger")
            return render_template("auth/login.html", next=next_url)

        _establish_session(user)
        users_model.update_by_id(
            mongo.db, user["_id"], {"last_login_at": now_ct()}
        )
        return _role_redirect(next_url)

    return render_template("auth/login.html", next=next_url)


# ── /register ──────────────────────────────────────────────────────────────

@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def register():
    if "user_id" in session:
        return _role_redirect()

    registration_open = platform_settings_model.is_registration_enabled(mongo.db)

    if request.method == "POST":
        if not registration_open:
            flash("Registration is currently closed.", "warning")
            return render_template("auth/register.html", registration_closed=True)

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        # Basic field validation
        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return render_template("auth/register.html")

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("auth/register.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("auth/register.html")

        existing = users_model.find_by_email(mongo.db, email)
        if existing:
            # Edge case: email belongs to a Google-only account
            if existing.get("google_id") and not existing.get("password_hash"):
                flash(
                    "This email is already registered via Google. "
                    "Please sign in with Google instead.",
                    "warning",
                )
            else:
                flash("An account with this email already exists. Please log in.", "warning")
            return render_template("auth/register.html")

        now = now_ct()
        verification_token = _new_token()
        user_id = users_model.insert_one(
            mongo.db,
            {
                "name": name,
                "email": email,
                "password_hash": generate_password_hash(password),
                "google_id": None,
                "role": "free",
                "subscription_status": "none",
                "email_verified": False,
                "email_verification_token": verification_token,
                "created_at": now,
                "last_login_at": now,
                "is_active": True,
            },
        )

        user = users_model.find_by_id(mongo.db, user_id)
        # Auto-login regardless — verification is a prompt, not a gate (Module 01).
        _establish_session(user)
        _send_verification(user, verification_token)
        flash("Welcome! Your account has been created.", "success")
        flash(
            "We've sent a verification link to your email — please confirm your address.",
            "info",
        )
        return redirect(url_for("main.home"))

    return render_template(
        "auth/register.html",
        registration_closed=not registration_open,
    )


# ── Google OAuth — initiate ────────────────────────────────────────────────

@auth_bp.route("/login/google")
def login_google():
    """Kick off the Google OAuth flow from the login screen."""
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri, prompt="select_account")


@auth_bp.route("/register/google")
def register_google():
    """Same OAuth flow as login/google — the callback handles both cases."""
    if not platform_settings_model.is_registration_enabled(mongo.db):
        flash("Registration is currently closed.", "warning")
        return redirect(url_for("auth.register"))
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri, prompt="select_account")


# ── Google OAuth — callback ────────────────────────────────────────────────

@auth_bp.route("/login/google/callback")
def google_callback():
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.userinfo()

    google_id = userinfo["sub"]
    email = userinfo["email"].lower()
    name = userinfo.get("name", "")

    # 1. Existing account matched by google_id → log in
    user = users_model.find_by_google_id(mongo.db, google_id)

    if not user:
        # 2. Email/password account with the same address → link the Google identity
        user = users_model.find_by_email(mongo.db, email)
        if user:
            users_model.update_by_id(
                mongo.db, user["_id"], {"google_id": google_id, "name": name}
            )
            user = users_model.find_by_id(mongo.db, user["_id"])

    if not user:
        # 3. Brand-new user — create a free account (unless registration is closed)
        if not platform_settings_model.is_registration_enabled(mongo.db):
            flash("Registration is currently closed.", "warning")
            return redirect(url_for("auth.login"))
        now = now_ct()
        user_id = users_model.insert_one(
            mongo.db,
            {
                "name": name,
                "email": email,
                "password_hash": None,
                "google_id": google_id,
                "role": "free",
                "subscription_status": "none",
                # Google has already verified the address — skip our own flow.
                "email_verified": True,
                "email_verification_token": None,
                "created_at": now,
                "last_login_at": now,
                "is_active": True,
            },
        )
        user = users_model.find_by_id(mongo.db, user_id)

    if not user.get("is_active"):
        flash("Your account has been deactivated. Please contact support.", "danger")
        return redirect(url_for("auth.login"))

    _establish_session(user)
    users_model.update_by_id(
        mongo.db, user["_id"], {"last_login_at": now_ct()}
    )
    return _role_redirect()


# ── /logout ────────────────────────────────────────────────────────────────

@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("main.index"))


# ── /forgot-password ────────────────────────────────────────────────────────

# Generic response shown whether or not the email exists — prevents enumeration.
_RESET_SENT_MSG = "If that email is registered, a password reset link has been sent."


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("auth/forgot_password.html")

    email = request.form.get("email", "").strip().lower()
    user = users_model.find_by_email(mongo.db, email)

    # Google-only account → tell them to use Google (distinct, per spec).
    if user and user.get("google_id") and not user.get("password_hash"):
        flash(
            "That account uses Google Sign-In — please log in with Google instead.",
            "warning",
        )
        return render_template("auth/forgot_password.html")

    # Unverified email/password account → don't send a reset to an unconfirmed
    # address; prompt them to verify first (spec: reset requires a verified email).
    if user and not user.get("email_verified"):
        flash(
            "Please verify your email address first — check your inbox for the "
            "verification email. You can log in and use the banner to resend it.",
            "warning",
        )
        return render_template("auth/forgot_password.html")

    # Eligible account (exists, email/password, verified) → issue a reset token.
    if user:
        raw_token = _new_token()
        reset_tokens_model.invalidate_unused_for_user(mongo.db, user["_id"])
        reset_tokens_model.create(
            mongo.db,
            user["_id"],
            reset_tokens_model.hash_token(raw_token),
            now_ct() + timedelta(hours=1),
        )
        reset_url = url_for("auth.reset_password", token=raw_token, _external=True)
        email_service.send_password_reset_email(
            user["email"], user.get("name", ""), reset_url
        )

    # Same generic confirmation regardless of whether the email existed.
    flash(_RESET_SENT_MSG, "info")
    return render_template("auth/forgot_password.html")


# ── /reset-password/<token> ─────────────────────────────────────────────────

@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_doc = reset_tokens_model.find_active_by_hash(
        mongo.db, reset_tokens_model.hash_token(token)
    )

    # Invalid / expired / already-used all look identical to the user.
    if token_doc is None:
        return render_template("auth/reset_password.html", valid=False)

    if request.method == "GET":
        return render_template("auth/reset_password.html", valid=True, token=token)

    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return render_template("auth/reset_password.html", valid=True, token=token)
    if password != confirm:
        flash("Passwords do not match.", "danger")
        return render_template("auth/reset_password.html", valid=True, token=token)

    user_id = token_doc["user_id"]
    users_model.set_password_hash(mongo.db, user_id, generate_password_hash(password))
    reset_tokens_model.mark_used(mongo.db, token_doc["_id"])
    # Burn any other outstanding tokens for this user too.
    reset_tokens_model.invalidate_unused_for_user(mongo.db, user_id)

    # Log the user in automatically and send them to /home.
    user = users_model.find_by_id(mongo.db, user_id)
    _establish_session(user)
    users_model.update_by_id(mongo.db, user_id, {"last_login_at": now_ct()})
    flash("Your password has been reset. You're now signed in.", "success")
    return redirect(url_for("main.home"))


# ── /verify-email/<token> ───────────────────────────────────────────────────

@auth_bp.route("/verify-email/<token>")
def verify_email(token):
    user = users_model.find_by_verification_token(mongo.db, token)
    dest = "main.home" if "user_id" in session else "auth.login"

    if user is None:
        # Invalid or already-used token (token is nulled once verified).
        flash("This verification link is invalid or has already been used.", "warning")
        return redirect(url_for(dest))

    users_model.set_email_verified(mongo.db, user["_id"])
    flash("Your email has been verified — thanks!", "success")
    return redirect(url_for(dest))


# ── /resend-verification ────────────────────────────────────────────────────

@auth_bp.route("/resend-verification", methods=["POST"])
@login_required
@limiter.limit("5 per hour")
def resend_verification():
    user = users_model.find_by_id(mongo.db, session["user_id"])

    if user is None:
        session.clear()
        return redirect(url_for("auth.login"))

    if user.get("email_verified"):
        flash("Your email is already verified.", "info")
        return redirect(url_for("main.home"))

    token = _new_token()
    users_model.set_verification_token(mongo.db, user["_id"], token)
    _send_verification(user, token)
    flash("Verification email sent — please check your inbox.", "info")

    # Return the user where they came from (the banner lives on /home & /dashboard).
    dest = request.referrer if (request.referrer or "").startswith(request.host_url) else None
    return redirect(dest or url_for("main.home"))


# ── /account/api-keys ────────────────────────────────────────────────────────
# MCP server access (Module 11). GET lists the user's keys (raw key never
# shown again after creation); POST generates a new one; revoke deactivates.

def _serialize_api_key(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "label": doc.get("label", ""),
        "is_active": doc.get("is_active", True),
        "created_at": doc.get("created_at"),
        "last_used_at": doc.get("last_used_at"),
    }


@auth_bp.route("/account/api-keys", methods=["GET"])
@login_required
def api_keys():
    keys = api_keys_model.find_by_user(mongo.db, session["user_id"])
    return render_template("auth/api_keys.html", keys=keys)


@auth_bp.route("/account/api-keys", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def create_api_key():
    if request.is_json:
        label = ((request.get_json(silent=True) or {}).get("label") or "").strip()
    else:
        label = (request.form.get("label") or "").strip()
    label = label or "API key"

    key_id, raw_key = api_keys_model.create(mongo.db, session["user_id"], label)

    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True, "id": str(key_id), "label": label, "key": raw_key})

    return render_template(
        "auth/api_key_created.html",
        raw_key=raw_key,
        label=label,
        mcp_url=current_app.config.get("MCP_PUBLIC_URL"),
    )


@auth_bp.route("/account/api-keys/<id>/revoke", methods=["POST"])
@login_required
def revoke_api_key(id):
    revoked = api_keys_model.revoke(mongo.db, id, session["user_id"])
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": revoked})
    flash("API key revoked." if revoked else "Key not found.", "success" if revoked else "warning")
    return redirect(url_for("auth.api_keys"))
