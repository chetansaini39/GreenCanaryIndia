"""
Server-side tier/symbol authorization tests for the /api/* endpoints.

These tests verify that:
  - Unauthenticated requests are rejected (401).
  - Free users receive 403 for paid-only endpoints, regardless of how the
    request is crafted — the server enforces access, not the frontend.
  - Paid users can reach the same endpoints (200, even when there's no data).

No live MongoDB is needed: denied paths return before any DB call,
and allowed paths mock the relevant DB function.
"""
from unittest.mock import patch

import pytest
from tests.conftest import login_as


# ── Unauthenticated ────────────────────────────────────────────────────────

def test_symbols_unauthenticated(client):
    assert client.get("/api/symbols").status_code == 401


def test_gex_unauthenticated(client):
    assert client.get("/api/gex?symbol=SPY&type=weekly").status_code == 401


def test_gex_rolling_unauthenticated(client):
    assert client.get("/api/gex/rolling?symbol=SPY").status_code == 401


def test_ohlcv_unauthenticated(client):
    assert client.get("/api/ohlcv?symbol=SPY").status_code == 401


# ── Free user blocked on paid-only features ────────────────────────────────

def test_free_user_rolling_endpoint_is_403(client):
    """Rolling 5-day series is paid-only; crafting the request manually still blocks."""
    login_as(client, "free")
    resp = client.get("/api/gex/rolling?symbol=SPY")
    assert resp.status_code == 403
    assert "paid" in resp.get_json()["error"]


def test_free_user_paid_symbol_is_403(client):
    """Free user requesting a paid symbol (e.g. AAPL) directly gets 403."""
    login_as(client, "free")
    resp = client.get("/api/gex?symbol=AAPL&type=weekly&date=2025-06-16")
    assert resp.status_code == 403


def test_free_user_monthly_on_free_stock_is_403(client):
    """Monthly granularity is not in the free-stock tier even for a free symbol."""
    login_as(client, "free")
    resp = client.get("/api/gex?symbol=TSLA&type=monthly&date=2025-06-16")
    assert resp.status_code == 403


def test_free_user_intraday_on_free_index_is_200(client):
    """Free index users have intraday (0DTE) access for SPY/QQQ."""
    login_as(client, "free")
    with patch("app.models.gex_intraday.find_by_symbol_date_range", return_value=[]), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get("/api/gex?symbol=SPY&type=intraday&date=2025-06-13")  # a Friday
    assert resp.status_code == 200


def test_free_user_stock_out_of_window_is_403(client):
    """Free stock users cannot request dates outside the current week window.

    2000-01-03 is far in the past — guaranteed to be outside any current window.
    """
    login_as(client, "free")
    resp = client.get("/api/gex?symbol=TSLA&type=weekly&date=2000-01-03")
    assert resp.status_code == 403


def test_free_user_current_week_stock_weekly_is_200(client):
    """Free user on TSLA weekly gets 200 when requesting today's date (latest snapshot).

    Free users always see the latest available day — the UI sends today's date.
    Pins the allowed window so the test is independent of the real clock.
    """
    from datetime import date as _date
    today = _date.today()
    pinned_monday = today - __import__("datetime").timedelta(days=today.weekday())
    login_as(client, "free")
    with patch("app.routes.api._free_stock_allowed_week_mondays", return_value={pinned_monday}), \
         patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get(f"/api/gex?symbol=TSLA&type=weekly&date={today.isoformat()}")
    assert resp.status_code == 200


def test_free_user_next_week_stock_weekly_is_403(client):
    """Same symbol (TSLA), same granularity (weekly) — only the date differs.

    Next week's Monday must return 403 even though TSLA is a free-tier symbol.
    This is the critical next-week gate: symbol+granularity alone isn't enough.
    """
    from datetime import date as _date
    pinned_monday = _date(2025, 6, 9)
    login_as(client, "free")
    with patch("app.routes.api._free_stock_allowed_week_mondays", return_value={pinned_monday}):
        resp = client.get("/api/gex?symbol=TSLA&type=weekly&date=2025-06-16")  # next Monday
    assert resp.status_code == 403


def test_free_user_next_week_index_weekly_is_403(client):
    """Next-week gate applies to index symbols too (SPY, QQQ), not just stocks."""
    from datetime import date as _date
    pinned_monday = _date(2025, 6, 9)
    login_as(client, "free")
    with patch("app.routes.api._free_stock_allowed_week_mondays", return_value={pinned_monday}):
        resp = client.get("/api/gex?symbol=SPY&type=weekly&date=2025-06-16")
    assert resp.status_code == 403


def test_free_user_ohlcv_paid_symbol_is_403(client):
    login_as(client, "free")
    resp = client.get("/api/ohlcv?symbol=AAPL&days=10")
    assert resp.status_code == 403


def test_free_user_ohlcv_free_symbol_is_200(client):
    login_as(client, "free")
    sample = [{"date": "2025-06-16", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1000}]
    with patch("app.services.ohlcv_history.fetch_ohlcv_history", return_value=sample):
        resp = client.get("/api/ohlcv?symbol=NVDA&days=10")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["symbol"] == "NVDA"
    assert len(body["bars"]) == 1


# ── Paid user can reach paid-only endpoints ────────────────────────────────

def test_paid_user_rolling_is_200(client):
    """Paid user hitting /api/gex/rolling gets 200 (empty series when no data)."""
    login_as(client, "paid", subscription_status="active")
    # Mock the DB call so no live MongoDB is needed
    with patch("app.models.gex_rolling_5d.find_last_n", return_value=[]):
        resp = client.get("/api/gex/rolling?symbol=SPY")
    assert resp.status_code == 200
    assert resp.get_json()["series"] == []


def test_paid_user_monthly_on_any_symbol_is_200(client):
    """Paid user can request monthly granularity for any symbol; returns null data if pipeline hasn't run."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_monthly_opex.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_monthly_opex.find_latest_for_symbol", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get("/api/gex?symbol=TSLA&type=monthly&date=2025-06-01")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data"] is None
    assert "not available" in body["message"]


def test_paid_user_paid_symbol_is_200(client):
    """Paid user can reach a paid symbol like AAPL."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get("/api/gex?symbol=AAPL&type=weekly&date=2025-06-16")
    assert resp.status_code == 200


# ── /api/gex/greeks — paid-only ───────────────────────────────────────────

def test_greeks_unauthenticated(client):
    assert client.get("/api/gex/greeks?symbol=TSLA&type=weekly").status_code == 401


def test_free_user_greeks_is_403(client):
    """Greeks endpoint is paid-only; a free user on a free symbol still gets 403."""
    login_as(client, "free")
    resp = client.get("/api/gex/greeks?symbol=TSLA&type=weekly&date=2025-06-09")
    assert resp.status_code == 403
    assert "paid" in resp.get_json()["error"]


def test_paid_user_greeks_is_200(client):
    """Paid user gets 200 from /api/gex/greeks (empty strikes when no data)."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get("/api/gex/greeks?symbol=TSLA&type=weekly&date=2025-06-09")
    assert resp.status_code == 200
    assert resp.get_json()["strikes"] == []


# ── Public endpoint (no auth) ──────────────────────────────────────────────

def test_public_endpoint_no_auth_returns_200(client):
    """Public snapshot endpoint requires no auth — Twitter landing page."""
    with patch("app.models.gex_weekly.find_latest_for_week", return_value=None):
        resp = client.get("/api/gex/public?symbol=SPY&date=2025-06-16")
    assert resp.status_code == 200


# ── Symbols listing ────────────────────────────────────────────────────────

def test_free_user_symbols_locks_paid_symbols(client):
    """Free user's /api/symbols response marks paid symbols as locked=true."""
    login_as(client, "free")
    with patch("app.models.symbols_config.find_all_active", return_value=[]):
        resp = client.get("/api/symbols")
    assert resp.status_code == 200
    body = resp.get_json()
    symbols = {s["symbol"]: s for s in body["symbols"]}
    # Fallback catalog is used when DB returns empty; check a few entries
    assert symbols["SPY"]["locked"] is False
    assert symbols["QQQ"]["locked"] is False
    assert symbols["TSLA"]["locked"] is False
    assert symbols["NVDA"]["locked"] is False
    # Paid symbols must be locked
    for sym in ("SPX", "AAPL", "MSFT"):
        assert symbols[sym]["locked"] is True, f"{sym} should be locked for free user"


def test_paid_user_symbols_none_locked(client):
    """Paid user's /api/symbols response has no locked symbols."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.symbols_config.find_all_active", return_value=[]):
        resp = client.get("/api/symbols")
    assert resp.status_code == 200
    body = resp.get_json()
    locked = [s for s in body["symbols"] if s["locked"]]
    assert locked == [], f"Paid user should see no locked symbols; got {locked}"
