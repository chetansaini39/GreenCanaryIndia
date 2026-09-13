"""
Admin & staff panel tests — authorization, self-demotion lockout, audit logging.
"""
from unittest.mock import patch, MagicMock

from bson import ObjectId

from tests.conftest import login_as

ADMIN_ID = "000000000000000000000001"
OTHER_ID = "000000000000000000000002"


def _mock_user(user_id=OTHER_ID, role="free", is_active=True):
    return {
        "_id": ObjectId(user_id),
        "email": "user@example.com",
        "name": "Test User",
        "role": role,
        "subscription_status": "none",
        "is_active": is_active,
        "created_at": None,
        "last_login_at": None,
    }


# ── Authorization ──────────────────────────────────────────────────────────

def test_admin_home_requires_admin(client):
    login_as(client, "free")
    assert client.get("/admin").status_code == 403


def test_admin_home_200_for_admin(client):
    login_as(client, "admin")
    with patch("app.models.platform_settings.get_settings", return_value={"registration_enabled": True}):
        resp = client.get("/admin")
    assert resp.status_code == 200
    assert b"Registration: OPEN" in resp.data


def test_staff_home_200_for_staff(client):
    login_as(client, "staff")
    assert client.get("/staff").status_code == 200


def test_staff_users_read_only_page(client):
    login_as(client, "staff")
    with patch("app.models.users.search", return_value=[]):
        resp = client.get("/staff/users")
    assert resp.status_code == 200
    assert b"Read-only" in resp.data


def test_staff_cannot_access_admin_users(client):
    login_as(client, "staff")
    with patch("app.models.users.search", return_value=[]):
        assert client.get("/admin/users").status_code == 403


def test_free_user_cannot_access_pipeline(client):
    login_as(client, "free")
    assert client.get("/admin/pipeline").status_code == 403


# ── Self-demotion lockout ──────────────────────────────────────────────────

def test_admin_cannot_demote_self(client):
    login_as(client, "admin")
    resp = client.post(
        f"/admin/users/{ADMIN_ID}/role",
        data={"role": "staff"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"cannot change your own role" in resp.data


def test_admin_can_change_other_user_role(client):
    login_as(client, "admin")
    user = _mock_user()
    with patch("app.models.users.find_by_id", return_value=user), \
         patch("app.models.users.update_by_id") as mock_update, \
         patch("app.services.audit.log_action") as mock_audit:
        resp = client.post(
            f"/admin/users/{OTHER_ID}/role",
            data={"role": "paid"},
            follow_redirects=True,
        )
    assert resp.status_code == 200
    mock_update.assert_called_once()
    mock_audit.assert_called_once()
    assert mock_audit.call_args[0][1] == "role_change"


def test_admin_cannot_deactivate_self(client):
    login_as(client, "admin")
    resp = client.post(
        f"/admin/users/{ADMIN_ID}/deactivate",
        follow_redirects=True,
    )
    assert b"cannot deactivate your own account" in resp.data


# ── Audit logging ──────────────────────────────────────────────────────────

def test_registration_toggle_writes_audit_log(client):
    login_as(client, "admin")
    with patch("app.models.platform_settings.get_settings", return_value={"registration_enabled": True}), \
         patch("app.models.platform_settings.set_registration_enabled", return_value={"registration_enabled": False}) as mock_set, \
         patch("app.services.audit.log_action") as mock_audit:
        resp = client.post(
            "/admin/settings/registration",
            data={},
            follow_redirects=True,
        )
    assert resp.status_code == 200
    mock_set.assert_called_once()
    mock_audit.assert_called_once()
    assert mock_audit.call_args[0][1] == "registration_toggle"


def test_subscription_override_writes_audit_log(client):
    login_as(client, "admin")
    user = _mock_user(role="free")
    with patch("app.models.users.find_by_id", return_value=user), \
         patch("app.models.users.update_by_id"), \
         patch("app.services.audit.log_action") as mock_audit:
        client.post(
            f"/admin/users/{OTHER_ID}/subscription",
            data={"role": "paid", "subscription_status": "active"},
            follow_redirects=True,
        )
    mock_audit.assert_called_once()
    assert mock_audit.call_args[0][1] == "subscription_override"


# ── Staff full GEX API access ──────────────────────────────────────────────

def test_staff_can_access_paid_symbol(client):
    login_as(client, "staff")
    with patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get("/api/gex?symbol=AAPL&type=weekly&date=2025-06-16")
    assert resp.status_code == 200


def test_staff_can_access_rolling_endpoint(client):
    login_as(client, "staff")
    with patch("app.models.gex_rolling_5d.find_last_n", return_value=[]):
        resp = client.get("/api/gex/rolling?symbol=SPY")
    assert resp.status_code == 200
