"""Homepage, ticker/pricing API, and contact form tests (Module 01).

No live MongoDB — the model modules and the email service are patched, so
these exercise route logic (rendering, validation, JSON responses, auth)
without a database or outbound email.
"""
from unittest.mock import patch

from bson import ObjectId

from tests.conftest import login_as


USER_ID = "000000000000000000000001"


# ── Homepage ────────────────────────────────────────────────────────────────

def test_homepage_renders_marketing_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # Marketing sections present
    assert b"Get started free" in resp.data
    assert b"id=\"features\"" in resp.data
    assert b"id=\"pricing\"" in resp.data


def test_homepage_shows_dashboard_link_when_logged_in(client):
    login_as(client, "paid")
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Go to dashboard" in resp.data
    # The nav's "Log in" button collapses when a session is active.
    assert b">Log in</a>" not in resp.data


def test_homepage_pro_price_from_config(client):
    resp = client.get("/")
    # Default PRO_PRICE ("$29") is injected from config, not hardcoded copy.
    assert b"$29" in resp.data


# ── /api/prices & /api/pricing ──────────────────────────────────────────────

def test_api_prices_returns_symbols_from_latest_snapshot(client):
    def _fake_latest(db, symbol, snapshot_type=None):
        return {"spot_price": 500.0, "created_at": None} if symbol == "SPY" else None

    with patch("app.models.gex_intraday.find_latest", side_effect=_fake_latest):
        resp = client.get("/api/prices")
    assert resp.status_code == 200
    data = resp.get_json()
    syms = {p["symbol"]: p["spot_price"] for p in data["prices"]}
    assert syms["SPY"] == 500.0
    assert syms["QQQ"] is None
    assert "market_open" in data


def test_api_pricing_returns_pro_price(client):
    resp = client.get("/api/pricing")
    assert resp.status_code == 200
    assert resp.get_json()["pro_price"] == "$29"


# ── GET /contact ────────────────────────────────────────────────────────────

def test_contact_get_ok(client):
    resp = client.get("/contact")
    assert resp.status_code == 200
    assert b"Send message" in resp.data


def test_contact_get_prefills_email_when_logged_in(client):
    login_as(client, "free")
    user = {"_id": ObjectId(USER_ID), "email": "prefill@example.com"}
    with patch("app.models.users.find_by_id", return_value=user):
        resp = client.get("/contact")
    assert resp.status_code == 200
    assert b"prefill@example.com" in resp.data


# ── POST /contact — validation ──────────────────────────────────────────────

def test_contact_post_rejects_invalid_type(client):
    resp = client.post("/contact", json={
        "type": "spam", "name": "A", "email": "a@b.com",
        "message": "This is a long enough message.",
    })
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_contact_post_rejects_missing_name(client):
    resp = client.post("/contact", json={
        "type": "bug", "name": "", "email": "a@b.com",
        "message": "This is a long enough message.",
    })
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_contact_post_rejects_bad_email(client):
    resp = client.post("/contact", json={
        "type": "bug", "name": "A", "email": "not-an-email",
        "message": "This is a long enough message.",
    })
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_contact_post_rejects_short_message(client):
    resp = client.post("/contact", json={
        "type": "bug", "name": "A", "email": "a@b.com", "message": "too short",
    })
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ── POST /contact — success ─────────────────────────────────────────────────

@patch("app.routes.public.email_service")
@patch("app.models.contact_submissions.insert_one", return_value=ObjectId(USER_ID))
def test_contact_post_valid_stores_and_emails(insert_one, email_service, client):
    resp = client.post("/contact", json={
        "type": "feature", "name": "Alice", "email": "alice@example.com",
        "message": "I would love a dark mode toggle please.",
    })
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    insert_one.assert_called_once()
    # Anonymous submission → no user_id attached
    assert insert_one.call_args.kwargs.get("user_id") is None
    email_service.send_contact_notification_email.assert_called_once()


@patch("app.routes.public.email_service")
@patch("app.models.contact_submissions.insert_one", return_value=ObjectId(USER_ID))
def test_contact_post_attaches_user_id_when_logged_in(insert_one, email_service, client):
    login_as(client, "free")
    resp = client.post("/contact", json={
        "type": "bug", "name": "Bob", "email": "bob@example.com",
        "message": "Something looks broken on the SPY chart today.",
    })
    assert resp.status_code == 200
    assert insert_one.call_args.kwargs.get("user_id") == USER_ID


# ── Admin contact view ──────────────────────────────────────────────────────

def test_admin_contact_requires_staff_or_admin(client):
    login_as(client, "free")
    assert client.get("/admin/contact").status_code == 403


def test_admin_contact_lists_for_staff(client):
    login_as(client, "staff")
    with patch("app.models.contact_submissions.search", return_value=[]), \
         patch("app.models.contact_submissions.count_unread", return_value=0):
        resp = client.get("/admin/contact")
    assert resp.status_code == 200
    assert b"Contact Submissions" in resp.data


def test_admin_contact_set_status_ok(client):
    login_as(client, "admin")
    with patch("app.models.contact_submissions.set_status", return_value=True) as set_status, \
         patch("app.models.contact_submissions.count_unread", return_value=0):
        resp = client.post(
            f"/admin/contact/{USER_ID}/status",
            data={"status": "resolved"},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    set_status.assert_called_once()


def test_admin_contact_set_status_rejects_bad_status(client):
    login_as(client, "admin")
    with patch("app.models.contact_submissions.count_unread", return_value=0):
        resp = client.post(
            f"/admin/contact/{USER_ID}/status",
            data={"status": "bogus"},
            follow_redirects=False,
        )
    assert resp.status_code == 302  # redirects back with a flash, no update
