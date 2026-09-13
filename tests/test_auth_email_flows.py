"""Password-reset and email-verification flow tests (Module 01).

No live MongoDB — the model modules and the email service are patched on the
auth blueprint, so these exercise the route logic (branching, token handling,
messages) without a database or outbound email.
"""
from unittest.mock import MagicMock, patch

from bson import ObjectId

from tests.conftest import login_as


USER_ID = "000000000000000000000001"


def _user(**over):
    doc = {
        "_id": ObjectId(USER_ID),
        "name": "Test User",
        "email": "user@example.com",
        "password_hash": "hash",
        "google_id": None,
        "role": "free",
        "subscription_status": "none",
        "email_verified": True,
        "email_verification_token": None,
        "is_active": True,
    }
    doc.update(over)
    return doc


# ── /forgot-password ────────────────────────────────────────────────────────

def test_forgot_password_get_ok(client):
    resp = client.get("/forgot-password")
    assert resp.status_code == 200
    assert b"Reset your password" in resp.data


@patch("app.routes.auth.email_service")
@patch("app.routes.auth.reset_tokens_model")
@patch("app.routes.auth.users_model")
def test_forgot_unknown_email_is_generic_and_sends_nothing(users, tokens, email, client):
    users.find_by_email.return_value = None
    resp = client.post("/forgot-password", data={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert b"If that email is registered" in resp.data
    tokens.create.assert_not_called()
    email.send_password_reset_email.assert_not_called()


@patch("app.routes.auth.email_service")
@patch("app.routes.auth.reset_tokens_model")
@patch("app.routes.auth.users_model")
def test_forgot_google_only_tells_user_to_use_google(users, tokens, email, client):
    users.find_by_email.return_value = _user(password_hash=None, google_id="g-123")
    resp = client.post("/forgot-password", data={"email": "user@example.com"})
    assert resp.status_code == 200
    assert b"Google Sign-In" in resp.data
    tokens.create.assert_not_called()
    email.send_password_reset_email.assert_not_called()


@patch("app.routes.auth.email_service")
@patch("app.routes.auth.reset_tokens_model")
@patch("app.routes.auth.users_model")
def test_forgot_unverified_prompts_verification(users, tokens, email, client):
    users.find_by_email.return_value = _user(email_verified=False)
    resp = client.post("/forgot-password", data={"email": "user@example.com"})
    assert resp.status_code == 200
    assert b"verify your email address first" in resp.data.lower()
    tokens.create.assert_not_called()
    email.send_password_reset_email.assert_not_called()


@patch("app.routes.auth.email_service")
@patch("app.routes.auth.reset_tokens_model")
@patch("app.routes.auth.users_model")
def test_forgot_eligible_issues_token_and_emails(users, tokens, email, client):
    users.find_by_email.return_value = _user()
    tokens.hash_token.return_value = "hashed"
    resp = client.post("/forgot-password", data={"email": "user@example.com"})
    assert resp.status_code == 200
    assert b"If that email is registered" in resp.data
    tokens.invalidate_unused_for_user.assert_called_once()
    tokens.create.assert_called_once()
    email.send_password_reset_email.assert_called_once()


# ── /reset-password/<token> ─────────────────────────────────────────────────

@patch("app.routes.auth.reset_tokens_model")
def test_reset_invalid_token_get_shows_error(tokens, client):
    tokens.find_active_by_hash.return_value = None
    resp = client.get("/reset-password/whatever")
    assert resp.status_code == 200
    assert b"invalid" in resp.data.lower()


@patch("app.routes.auth.reset_tokens_model")
def test_reset_valid_token_get_shows_form(tokens, client):
    tokens.find_active_by_hash.return_value = {"_id": ObjectId(), "user_id": ObjectId(USER_ID)}
    resp = client.get("/reset-password/goodtoken")
    assert resp.status_code == 200
    assert b"Choose a new password" in resp.data


@patch("app.routes.auth.email_service")
@patch("app.routes.auth.users_model")
@patch("app.routes.auth.reset_tokens_model")
def test_reset_post_success_updates_and_logs_in(tokens, users, email, client):
    token_id = ObjectId()
    tokens.find_active_by_hash.return_value = {"_id": token_id, "user_id": ObjectId(USER_ID)}
    users.find_by_id.return_value = _user()
    resp = client.post(
        "/reset-password/goodtoken",
        data={"password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/home")
    users.set_password_hash.assert_called_once()
    tokens.mark_used.assert_called_once()
    tokens.invalidate_unused_for_user.assert_called_once()


@patch("app.routes.auth.users_model")
@patch("app.routes.auth.reset_tokens_model")
def test_reset_post_mismatch_rerenders_form(tokens, users, client):
    tokens.find_active_by_hash.return_value = {"_id": ObjectId(), "user_id": ObjectId(USER_ID)}
    resp = client.post(
        "/reset-password/goodtoken",
        data={"password": "newpass123", "confirm_password": "different1"},
    )
    assert resp.status_code == 200
    assert b"do not match" in resp.data
    users.set_password_hash.assert_not_called()


# ── /verify-email/<token> ───────────────────────────────────────────────────

@patch("app.routes.auth.users_model")
def test_verify_invalid_token_redirects(users, client):
    users.find_by_verification_token.return_value = None
    resp = client.get("/verify-email/badtoken")
    assert resp.status_code == 302
    users.set_email_verified.assert_not_called()


@patch("app.routes.auth.users_model")
def test_verify_valid_token_marks_verified(users, client):
    users.find_by_verification_token.return_value = _user(email_verified=False)
    resp = client.get("/verify-email/goodtoken")
    assert resp.status_code == 302
    users.set_email_verified.assert_called_once()


# ── /resend-verification ────────────────────────────────────────────────────

def test_resend_requires_login(client):
    resp = client.post("/resend-verification")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


@patch("app.utils.decorators.users_model")
@patch("app.routes.auth.email_service")
@patch("app.routes.auth.users_model")
def test_resend_sends_when_unverified(users, email, dec_users, client):
    # login_required (the decorator) does its own active-account lookup.
    dec_users.find_by_id.return_value = _user(email_verified=False)
    users.find_by_id.return_value = _user(email_verified=False)
    login_as(client, "free")
    resp = client.post("/resend-verification")
    assert resp.status_code == 302
    users.set_verification_token.assert_called_once()
    email.send_verification_email.assert_called_once()


@patch("app.utils.decorators.users_model")
@patch("app.routes.auth.email_service")
@patch("app.routes.auth.users_model")
def test_resend_noop_when_already_verified(users, email, dec_users, client):
    dec_users.find_by_id.return_value = _user(email_verified=True)
    users.find_by_id.return_value = _user(email_verified=True)
    login_as(client, "free")
    resp = client.post("/resend-verification")
    assert resp.status_code == 302
    email.send_verification_email.assert_not_called()


# ── /register sends verification + sets email_verified false ────────────────

@patch("app.routes.auth.email_service")
@patch("app.routes.auth.platform_settings_model")
@patch("app.routes.auth.users_model")
def test_register_sends_verification_email(users, settings, email, client):
    settings.is_registration_enabled.return_value = True
    users.find_by_email.return_value = None
    users.insert_one.return_value = ObjectId(USER_ID)
    users.find_by_id.return_value = _user(email_verified=False)

    resp = client.post(
        "/register",
        data={
            "name": "New User",
            "email": "new@example.com",
            "password": "password123",
            "confirm_password": "password123",
        },
    )
    assert resp.status_code == 302
    # Inserted with email_verified False and a verification token.
    inserted = users.insert_one.call_args[0][1]
    assert inserted["email_verified"] is False
    assert inserted["email_verification_token"]
    email.send_verification_email.assert_called_once()
