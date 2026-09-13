"""
Tier Authorization Test Matrix — Module 04 spec §"Tier authorization test matrix"

One test per spec table row (cases 1–20; case 21 is withheld pending a product
decision — see the comment at the bottom of this file). Test IDs match the spec
row numbers so "case_07 fails" maps unambiguously to a single spec row.

Run this suite any time tier logic changes:
    pytest tests/test_tier_auth_matrix.py -v
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from tests.conftest import login_as

# ── Fixed reference dates ──────────────────────────────────────────────────
# Using a known past week so tests are stable regardless of when they run.
# "Current week" is pinned to the week of 2025-06-09 (Mon) .. 2025-06-13 (Fri).

_PINNED_MONDAY   = date(2025, 6, 9)   # current week for free-tier purposes
_PINNED_TUESDAY  = date(2025, 6, 10)  # earlier day *within* the pinned week
_NEXT_MONDAY     = date(2025, 6, 16)  # one week after pinned — clearly "next week"
_NON_FRIDAY      = date(2025, 6, 9)   # Monday — not a Friday (for intraday stock tests)
_A_FRIDAY        = date(2025, 6, 13)  # Friday within the pinned week


def _pin_week():
    """Patch _free_stock_allowed_week_mondays to return the pinned monday."""
    return patch("app.routes.api._free_stock_allowed_week_mondays",
                 return_value={_PINNED_MONDAY})


def _mock_weekly_empty():
    """Patch the two weekly DB calls to return None (no data yet — still 200)."""
    return (
        patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None),
        patch("app.models.gex_weekly.find_latest_for_week", return_value=None),
        patch("app.models.gex_intraday.find_latest", return_value=None),
    )


def _mock_intraday_empty():
    return (
        patch("app.models.gex_intraday.find_by_symbol_date_range", return_value=[]),
        patch("app.models.gex_intraday.find_latest", return_value=None),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Case 1 — Free user, SPY intraday → 200
# ═══════════════════════════════════════════════════════════════════════════
def test_case_01_free_spy_intraday_is_200(client):
    """Free users have index-view access; SPY intraday (0DTE) is included."""
    login_as(client, "free")
    with patch("app.models.gex_intraday.find_by_symbol_date_range", return_value=[]), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get(f"/api/gex?symbol=SPY&type=intraday&date={_A_FRIDAY.isoformat()}")
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 2 — Free user, SPY weekly (current week, latest trade_date) → 200
# ═══════════════════════════════════════════════════════════════════════════
def test_case_02_free_spy_weekly_current_week_is_200(client):
    """Free users may access the current week's latest snapshot for SPY."""
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    login_as(client, "free")
    with patch("app.routes.api._free_stock_allowed_week_mondays",
               return_value={current_monday}), \
         patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get(f"/api/gex?symbol=SPY&type=weekly&date={today.isoformat()}")
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 3 — Free user, SPY weekly next week → 403
# ═══════════════════════════════════════════════════════════════════════════
def test_case_03_free_spy_weekly_next_week_is_403(client):
    """Next-week gate applies to index symbols — SPY next week is paid-only."""
    login_as(client, "free")
    with _pin_week():
        resp = client.get(f"/api/gex?symbol=SPY&type=weekly&date={_NEXT_MONDAY.isoformat()}")
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 4 — Free user, SPY weekly, earlier trade_date *within* current week → 403
# ═══════════════════════════════════════════════════════════════════════════
def test_case_04_free_spy_weekly_earlier_day_in_week_is_403(client):
    """Free users see the latest available snapshot only — stepping back is paid.

    The pinned tuesday (2025-06-10) is within the allowed week but is in the past,
    so req_date < today() triggers the 403.
    """
    login_as(client, "free")
    with _pin_week():
        resp = client.get(
            f"/api/gex?symbol=SPY&type=weekly&date={_PINNED_TUESDAY.isoformat()}"
        )
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 4b — Free user, SPY weekly with NO date, after Friday/weekend rollover → 200
# ═══════════════════════════════════════════════════════════════════════════
def test_case_04b_free_spy_weekly_no_date_after_rollover_is_200(client):
    """After the Friday/weekend rollover, the free index UI omits the date so the
    server resolves the current (rolled-forward) week itself. This must NOT 403
    just because 'today' now maps to the just-passed calendar week — the whole
    reason the client stopped sending a client-side 'today'.
    """
    login_as(client, "free")
    # Allowed window rolled forward to *next* Monday — i.e. today's calendar week
    # is no longer the allowed one. A request with no date must still be 200.
    rolled_monday = date.today() + timedelta(days=7)
    with patch("app.routes.api._free_stock_allowed_week_mondays",
               return_value={rolled_monday}), \
         patch("app.models.gex_weekly.find_by_symbol_expiry_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_expiry",
               return_value={"symbol": "SPY", "gex_by_strike": [], "created_at": None}), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get("/api/gex?symbol=SPY&type=weekly")
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 5 — Free user, SPY monthly → 403
# ═══════════════════════════════════════════════════════════════════════════
def test_case_05_free_spy_monthly_is_403(client):
    """Monthly OPEX is paid-only entirely — even for a free-tier index symbol like SPY."""
    login_as(client, "free")
    resp = client.get("/api/gex?symbol=SPY&type=monthly&date=2025-06-16")
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 6 — Free user, TSLA weekly (current week) → 200
# ═══════════════════════════════════════════════════════════════════════════
def test_case_06_free_tsla_weekly_current_week_is_200(client):
    """Free users have access to TSLA weekly for the current week."""
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    login_as(client, "free")
    with patch("app.routes.api._free_stock_allowed_week_mondays",
               return_value={current_monday}), \
         patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get(f"/api/gex?symbol=TSLA&type=weekly&date={today.isoformat()}")
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 7 — Free user, TSLA weekly, Next Week tab (expiry_date) → 200
# Case 7a — Free user, TSLA weekly, Week+2 tab → 200
# Case 7b — Free user, TSLA weekly, Past Week tab → 200
# Case 7c — Free user, TSLA weekly, week+3 (beyond the 4 tabs) → 403
# Case 7d — Free user, TSLA weekly, 2+ weeks back (beyond the 4 tabs) → 403
#
# Confirmed spec override: stock symbols (TSLA/NVDA) now expose a 4-tab
# window (Past Week | This Week | Next Week | Week+2) to free users — this
# replaces the old "next week is 403" rule for stocks specifically. Index
# symbols (case 3) are unaffected and remain current-week-only.
# ═══════════════════════════════════════════════════════════════════════════

_PIN_STOCK_PAST  = date(2025, 6, 6)
_PIN_STOCK_THIS  = date(2025, 6, 13)
_PIN_STOCK_NEXT  = date(2025, 6, 20)
_PIN_STOCK_WEEK2 = date(2025, 6, 27)
_PIN_STOCK_WEEK3 = date(2025, 7, 4)    # one window beyond Week+2 — still 403
_PIN_STOCK_2BACK = date(2025, 5, 30)   # one window before Past Week — still 403


def _pin_stock_tabs():
    """Patch _free_stock_tab_expiries to a fixed 4-window set, independent of
    whatever 'today' actually is when the suite runs."""
    return patch(
        "app.routes.api._free_stock_tab_expiries",
        return_value={_PIN_STOCK_PAST, _PIN_STOCK_THIS, _PIN_STOCK_NEXT, _PIN_STOCK_WEEK2},
    )


def _mock_weekly_by_expiry_empty():
    return (
        patch("app.models.gex_weekly.find_latest_for_expiry", return_value=None),
        patch("app.models.gex_intraday.find_latest", return_value=None),
    )


def test_case_07_free_tsla_weekly_next_week_tab_is_200(client):
    """Next Week tab (expiry_date one week out) is now free-accessible for TSLA —
    changed from 403: free users on stock symbols get the 4-tab window."""
    login_as(client, "free")
    with _pin_stock_tabs(), \
         patch("app.models.gex_weekly.find_latest_for_expiry", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get(
            f"/api/gex?symbol=TSLA&type=weekly&expiry_date={_PIN_STOCK_NEXT.isoformat()}"
        )
    assert resp.status_code == 200, resp.get_json()


def test_case_07a_free_tsla_weekly_week_plus_2_tab_is_200(client):
    """Week+2 tab is free-accessible for TSLA."""
    login_as(client, "free")
    with _pin_stock_tabs(), \
         patch("app.models.gex_weekly.find_latest_for_expiry", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get(
            f"/api/gex?symbol=TSLA&type=weekly&expiry_date={_PIN_STOCK_WEEK2.isoformat()}"
        )
    assert resp.status_code == 200, resp.get_json()


def test_case_07b_free_tsla_weekly_past_week_tab_is_200(client):
    """Past Week tab (one week back) is free-accessible for TSLA — the only
    historical access free users get on stock symbols."""
    login_as(client, "free")
    with _pin_stock_tabs(), \
         patch("app.models.gex_weekly.find_latest_for_expiry", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get(
            f"/api/gex?symbol=TSLA&type=weekly&expiry_date={_PIN_STOCK_PAST.isoformat()}"
        )
    assert resp.status_code == 200, resp.get_json()


def test_case_07c_free_tsla_weekly_week_plus_3_is_403(client):
    """Beyond the 4-tab window (week+3) is still paid-only for free users —
    confirms the widened window has a hard boundary, not unlimited forward access."""
    login_as(client, "free")
    with _pin_stock_tabs():
        resp = client.get(
            f"/api/gex?symbol=TSLA&type=weekly&expiry_date={_PIN_STOCK_WEEK3.isoformat()}"
        )
    assert resp.status_code == 403, resp.get_json()


def test_case_07d_free_tsla_weekly_two_weeks_back_is_403(client):
    """More than 1 week back is still paid-only for free users — only the single
    Past Week tab is free, not open-ended history."""
    login_as(client, "free")
    with _pin_stock_tabs():
        resp = client.get(
            f"/api/gex?symbol=TSLA&type=weekly&expiry_date={_PIN_STOCK_2BACK.isoformat()}"
        )
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 8 — Free user, TSLA intraday on a non-Friday → 200 with availability message
# ═══════════════════════════════════════════════════════════════════════════
def test_case_08_free_tsla_intraday_non_friday_is_200_with_message(client):
    """Free users requesting TSLA intraday on a non-Friday get a 200 with a
    data-availability message, NOT a 403 — this is a 'not applicable today'
    response, not a tier denial."""
    login_as(client, "free")
    # _NON_FRIDAY is a Monday (weekday 0)
    resp = client.get(f"/api/gex?symbol=TSLA&type=intraday&date={_NON_FRIDAY.isoformat()}")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["data"] is None
    # Message must convey "Fridays only" — not a generic "not available yet"
    assert "Friday" in (body.get("message") or ""), (
        f"Expected 'Friday' in message, got: {body.get('message')!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Case 9 — Free user, AAPL intraday (paid symbol) → 403
# ═══════════════════════════════════════════════════════════════════════════
def test_case_09_free_aapl_intraday_is_403(client):
    """AAPL is not in the free-tier symbol set — any request for it gets 403."""
    login_as(client, "free")
    resp = client.get(f"/api/gex?symbol=AAPL&type=intraday&date={_A_FRIDAY.isoformat()}")
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 10 — Free user, SPX intraday (paid symbol) → 403
# ═══════════════════════════════════════════════════════════════════════════
def test_case_10_free_spx_intraday_is_403(client):
    """SPX is not in the free-tier symbol set."""
    login_as(client, "free")
    resp = client.get(f"/api/gex?symbol=SPX&type=intraday&date={_A_FRIDAY.isoformat()}")
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 11 — Free user, /api/gex/greeks?symbol=SPY → 403
# ═══════════════════════════════════════════════════════════════════════════
def test_case_11_free_greeks_spy_is_403(client):
    """Parallel coordinates endpoint is paid-only regardless of symbol — even SPY."""
    login_as(client, "free")
    resp = client.get("/api/gex/greeks?symbol=SPY&type=weekly&date=2025-06-09")
    assert resp.status_code == 403, resp.get_json()
    assert "paid" in resp.get_json()["error"]


# ═══════════════════════════════════════════════════════════════════════════
# Case 12 — Free user, /api/gex/term-structure?symbol=SPY → 403
# ═══════════════════════════════════════════════════════════════════════════
def test_case_12_free_term_structure_spy_is_403(client):
    """EOD GEX Analysis card is paid-only — even for SPY which is a free-tier symbol."""
    login_as(client, "free")
    resp = client.get("/api/gex/term-structure?symbol=SPY")
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 13 — term-structure?symbol=TSLA → 404/400 (index-only, not a tier issue)
# Tested as a PAID user so the tier check doesn't shadow the symbol-type check.
# ═══════════════════════════════════════════════════════════════════════════
def test_case_13_paid_term_structure_tsla_is_400(client):
    """Term structure is index-only — TSLA (a stock) should return 400/404 even for
    a paid user.  This is a symbol-type restriction, not a tier restriction."""
    login_as(client, "paid", subscription_status="active")
    resp = client.get("/api/gex/term-structure?symbol=TSLA")
    assert resp.status_code in (400, 404), resp.get_json()


def test_case_13b_free_term_structure_tsla_is_403_not_400(client):
    """A free user hitting the same endpoint hits the tier check *first* (403), not
    the symbol-type check (400).  Both are acceptable rejection modes — document both."""
    login_as(client, "free")
    resp = client.get("/api/gex/term-structure?symbol=TSLA")
    # Tier check fires before symbol-type check → 403, not 400
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 14 — Free user, /api/gex/rolling?symbol=SPY → 403
# ═══════════════════════════════════════════════════════════════════════════
def test_case_14_free_rolling_spy_is_403(client):
    """Rolling 5-day series is paid-only — even for free-tier index symbols."""
    login_as(client, "free")
    resp = client.get("/api/gex/rolling?symbol=SPY")
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 15 — Paid user, AAPL monthly → 200
# ═══════════════════════════════════════════════════════════════════════════
def test_case_15_paid_aapl_monthly_is_200(client):
    """Monthly OPEX now covers Mag7 stocks, not just index symbols."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_monthly_opex.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_monthly_opex.find_latest_for_symbol", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get("/api/gex?symbol=AAPL&type=monthly&date=2025-06-01")
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 16 — Paid user, SPY weekly any earlier trade_date in current week → 200
# ═══════════════════════════════════════════════════════════════════════════
def test_case_16_paid_spy_weekly_earlier_day_in_week_is_200(client):
    """Paid users can step back to any trade_date within the current expiry cycle."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        # _PINNED_TUESDAY is a past date — paid users are not subject to the
        # "latest snapshot only" restriction.
        resp = client.get(
            f"/api/gex?symbol=SPY&type=weekly&date={_PINNED_TUESDAY.isoformat()}"
        )
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 17 — Paid user, TSLA weekly next week → 200
# ═══════════════════════════════════════════════════════════════════════════
def test_case_17_paid_tsla_weekly_next_week_is_200(client):
    """Paid users can access next-week expiry data for TSLA."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
         patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get(
            f"/api/gex?symbol=TSLA&type=weekly&date={_NEXT_MONDAY.isoformat()}"
        )
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 18 — Paid user, /api/gex/term-structure?symbol=SPX → 200
# ═══════════════════════════════════════════════════════════════════════════
def test_case_18_paid_term_structure_spx_is_200(client):
    """Paid users can access the EOD GEX Analysis card for index symbols."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_intraday.find_latest", return_value=None):
        resp = client.get("/api/gex/term-structure?symbol=SPX")
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 19 — Paid user, /api/gex/term-structure?symbol=AAPL → 404/400
# ═══════════════════════════════════════════════════════════════════════════
def test_case_19_paid_term_structure_aapl_is_400(client):
    """Term structure is index-only — even paid users cannot request it for stocks."""
    login_as(client, "paid", subscription_status="active")
    resp = client.get("/api/gex/term-structure?symbol=AAPL")
    assert resp.status_code in (400, 404), resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 20 — Anonymous, /api/gex/public?symbol=SPY → 200, rate-limited
# ═══════════════════════════════════════════════════════════════════════════
def test_case_20_anonymous_public_spy_is_200(client):
    """Public endpoint requires no auth — used for social-share landing pages."""
    with patch("app.models.gex_weekly.find_latest_for_week", return_value=None):
        resp = client.get("/api/gex/public?symbol=SPY&date=2025-06-09")
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Case 21 — Anonymous, /api/gex/public?symbol=AAPL → 403
# Decision: Option B — non-free symbols are blocked on the public endpoint
# to prevent unauthenticated data access regardless of link generation.
# ═══════════════════════════════════════════════════════════════════════════
def test_case_21_anonymous_public_paid_symbol_is_403(client):
    """Public endpoint blocks non-free symbols — even without auth, AAPL is 403."""
    resp = client.get("/api/gex/public?symbol=AAPL&date=2025-06-09")
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Cases 22–26 — Weekly/Monthly forward-trend endpoints (new, N-week/M-cycle)
# ═══════════════════════════════════════════════════════════════════════════

def test_case_22_free_weekly_trend_spy_is_200_no_strike_data(client):
    """Free users can access /api/gex/weekly-trend for their allowed symbols.
    Response must NOT contain gex_by_strike or any per-strike field."""
    login_as(client, "free")
    with patch("app.models.gex_weekly.find_trend_for_trade_date", return_value=[
        {"expiry_date": None, "net_gex": 1.0e9, "call_wall": 5000.0, "put_wall": 4800.0, "created_at": None},
        {"expiry_date": None, "net_gex": 0.8e9, "call_wall": 5010.0, "put_wall": 4780.0, "created_at": None},
    ]):
        resp = client.get("/api/gex/weekly-trend?symbol=SPY")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert "weeks" in body
    for week in body["weeks"]:
        assert "gex_by_strike" not in week, "gex_by_strike must not appear in weekly-trend response"
        assert "net_gex" in week
        assert "call_wall" in week
        assert "put_wall" in week


def test_case_23_free_weekly_trend_paid_symbol_is_403(client):
    """Symbol restriction applies to /api/gex/weekly-trend even though the endpoint is tier-open."""
    login_as(client, "free")
    resp = client.get("/api/gex/weekly-trend?symbol=AAPL")
    assert resp.status_code == 403, resp.get_json()


def test_case_24_free_monthly_trend_spy_is_200_no_strike_data(client):
    """Free users can access /api/gex/monthly-trend for their allowed symbols.
    Response must NOT contain gex_by_strike."""
    login_as(client, "free")
    with patch("app.models.gex_monthly_opex.find_trend_for_trade_date", return_value=[
        {"expiry_date": None, "net_gex": 2.0e9, "call_wall": 5000.0, "put_wall": 4800.0, "created_at": None},
    ]):
        resp = client.get("/api/gex/monthly-trend?symbol=SPY")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert "cycles" in body
    for cycle in body["cycles"]:
        assert "gex_by_strike" not in cycle, "gex_by_strike must not appear in monthly-trend response"


def test_case_25_paid_weekly_trend_spx_is_200(client):
    """Paid users can access /api/gex/weekly-trend for paid symbols too."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_weekly.find_trend_for_trade_date", return_value=[
        {"expiry_date": None, "net_gex": 3.0e9, "call_wall": 5000.0, "put_wall": 4800.0, "created_at": None},
    ]):
        resp = client.get("/api/gex/weekly-trend?symbol=SPX")
    assert resp.status_code == 200, resp.get_json()


def test_case_26_free_full_detail_5_weeks_out_is_403(client):
    """N-week expansion must NOT accidentally open up full per-strike detail for free users.

    A free user requesting /api/gex?type=weekly with a date 5 weeks out must still
    get a 403 — only the summary trend endpoint is reachable that far out.
    This is the regression test for 'did the N-tracked-weeks change accidentally give
    free users full strike-level access beyond next-week.'
    """
    five_weeks_out = _PINNED_MONDAY + timedelta(weeks=5)
    login_as(client, "free")
    with _pin_week():
        resp = client.get(f"/api/gex?symbol=SPY&type=weekly&date={five_weeks_out.isoformat()}")
    assert resp.status_code == 403, (
        f"Expected 403 for free user requesting full weekly detail 5 weeks out, got {resp.status_code}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Friday 3 PM CT rollover — clock-mocked boundary test
# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# Cases 30–34 — 12-Week / 3-Month Comparison endpoints (new carve-out)
# ═══════════════════════════════════════════════════════════════════════════

_MOCK_WEEKLY_SNAPSHOT = {
    "symbol": "SPY",
    "expiry_date": None,
    "trade_date": None,
    "net_gex": 1.0e9,
    "call_wall": 5000.0,
    "put_wall": 4800.0,
    "gex_by_strike": [{"strike": 500, "call_gex": 1.0e8, "put_gex": -5.0e7}],
    "created_at": None,
}

_MOCK_MONTHLY_SNAPSHOT = {
    "symbol": "SPY",
    "expiry_date": None,
    "trade_date": None,
    "net_gex": 2.0e9,
    "call_wall": 5100.0,
    "put_wall": 4900.0,
    "gex_by_strike": [{"strike": 510, "call_gex": 2.0e8, "put_gex": -1.0e8}],
    "created_at": None,
}


def test_case_30_free_weekly_comparison_spy_is_200_with_strike_data(client):
    """Free user, weekly-comparison for SPY → 200 with full gex_by_strike present.

    This is the deliberate carve-out: per-strike detail across all 12 weeks is
    allowed for free users on their free-tier symbols via this endpoint only.
    """
    login_as(client, "free")
    with patch("app.models.gex_weekly.find_recent", return_value=[_MOCK_WEEKLY_SNAPSHOT]):
        resp = client.get("/api/gex/weekly-comparison?symbol=SPY")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert "weeks" in body
    assert len(body["weeks"]) == 1
    # gex_by_strike must be present — that's the point of this endpoint
    assert body["weeks"][0].get("gex_by_strike") is not None, (
        "gex_by_strike must be present in weekly-comparison response"
    )


def test_case_31_free_weekly_comparison_paid_symbol_is_403(client):
    """Free user, weekly-comparison for AAPL → 403 (symbol restriction unchanged)."""
    login_as(client, "free")
    resp = client.get("/api/gex/weekly-comparison?symbol=AAPL")
    assert resp.status_code == 403, resp.get_json()


def test_case_32_free_original_weekly_5_weeks_out_still_403(client):
    """Critical regression check: the new comparison carve-out must NOT loosen the
    original /api/gex?type=weekly endpoint's week-distance gate.

    This is an exact re-run of case #26 — same endpoint, same user, same date,
    expected to still be 403.  If this flips to 200, the two authorization paths
    have been accidentally merged.
    """
    five_weeks_out = _PINNED_MONDAY + timedelta(weeks=5)
    login_as(client, "free")
    with _pin_week():
        resp = client.get(f"/api/gex?symbol=SPY&type=weekly&date={five_weeks_out.isoformat()}")
    assert resp.status_code == 403, (
        f"REGRESSION: original weekly endpoint returned {resp.status_code} instead of 403 "
        f"after adding weekly-comparison carve-out.  The two auth paths must stay separate."
    )


def test_case_33_free_monthly_comparison_spy_is_200_with_strike_data(client):
    """Free user, monthly-comparison for SPY → 200 with full gex_by_strike present."""
    login_as(client, "free")
    with patch("app.models.gex_monthly_opex.find_recent_cycles",
               return_value=[_MOCK_MONTHLY_SNAPSHOT]):
        resp = client.get("/api/gex/monthly-comparison?symbol=SPY")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert "cycles" in body
    assert body["cycles"][0].get("gex_by_strike") is not None


def test_case_34_paid_weekly_comparison_spx_is_200(client):
    """Paid user, weekly-comparison for SPX → 200 (paid users get all accessible symbols)."""
    login_as(client, "paid", subscription_status="active")
    with patch("app.models.gex_weekly.find_recent", return_value=[_MOCK_WEEKLY_SNAPSHOT]):
        resp = client.get("/api/gex/weekly-comparison?symbol=SPX")
    assert resp.status_code == 200, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# /api/gex/weekly-tabs — tab metadata for the stock weekly 4-tab toggle
# ═══════════════════════════════════════════════════════════════════════════

def test_weekly_tabs_free_tsla_returns_four_ordered_tabs(client):
    """Free user on a free stock symbol gets 4 ordered tabs, each with a
    resolved expiry_date and has_data flag — and NO per-strike data."""
    login_as(client, "free")
    with patch("app.models.gex_weekly.find_latest_for_expiry", return_value=None):
        resp = client.get("/api/gex/weekly-tabs?symbol=TSLA")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert [t["key"] for t in body["tabs"]] == ["past", "this", "next", "week2"]
    assert [t["label"] for t in body["tabs"]] == ["Past Week", "This Week", "Next Week", "Week+2"]
    for t in body["tabs"]:
        assert t["expiry_date"]                    # resolved ISO date present
        assert t["has_data"] is False              # mocked to None → no data
        assert "gex_by_strike" not in t            # metadata only, no strike data


def test_weekly_tabs_has_data_reflects_db_presence(client):
    """has_data is True when a gex_weekly snapshot exists for that expiry; the
    per-strike payload of that snapshot must not leak into the tab metadata."""
    login_as(client, "free")
    snap = {"created_at": None, "gex_by_strike": [{"strike": 100, "call_gex": 1.0}]}
    with patch("app.models.gex_weekly.find_latest_for_expiry", return_value=snap):
        resp = client.get("/api/gex/weekly-tabs?symbol=TSLA")
    body = resp.get_json()
    assert all(t["has_data"] is True for t in body["tabs"])
    for t in body["tabs"]:
        assert "gex_by_strike" not in t


def test_weekly_tabs_free_paid_symbol_is_403(client):
    """Same free-tier symbol restriction as /api/gex — AAPL is 403 for free users."""
    login_as(client, "free")
    resp = client.get("/api/gex/weekly-tabs?symbol=AAPL")
    assert resp.status_code == 403, resp.get_json()


def test_weekly_tabs_expiry_dates_match_free_window():
    """The tab expiry dates returned to the client must be exactly the set the
    /api/gex authorization check allows — no drift between the two code paths."""
    from app.routes import api as api_module
    tab_expiries = {t["expiry_date"] for t in api_module._stock_weekly_tabs()}
    assert tab_expiries == api_module._free_stock_tab_expiries()


def test_weekly_tabs_past_week_holiday_adjacent_is_a_real_friday():
    """Regression: when This Week is holiday-adjusted back to a Thursday (the
    July-4-on-Friday week), Past Week must still resolve to the *real* prior
    Friday (2025-06-27), not a Thursday one day earlier. Anchored to a Tuesday
    (2025-07-01) so This Week's nominal Friday is the 4th, which is a holiday."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from app.routes import api as api_module

    CT = ZoneInfo("America/Chicago")
    frozen_ct = _dt.datetime(2025, 7, 1, 10, 0, 0, tzinfo=CT)  # Tue before July 4
    with patch("app.routes.api.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_ct
        mock_dt.side_effect = lambda *a, **kw: _dt.datetime(*a, **kw)
        tabs = {t["key"]: t["expiry_date"] for t in api_module._stock_weekly_tabs()}

    # This Week's nominal Friday (07-04) is a holiday → adjusted to Thursday 07-03.
    assert tabs["this"] == date(2025, 7, 3)
    # Past Week must be the real prior Friday 06-27, NOT 06-26 (this_week - 7).
    assert tabs["past"] == date(2025, 6, 27)


# ═══════════════════════════════════════════════════════════════════════════
# /api/gex expiry_date + trade_date composition
# ═══════════════════════════════════════════════════════════════════════════

def test_weekly_expiry_only_returns_latest_day_for_that_week(client):
    """expiry_date without an explicit date → server returns that week's latest
    available trade_date (find_latest_for_expiry), not an exact-day lookup."""
    login_as(client, "paid", subscription_status="active")
    snap = {"gex_by_strike": [], "created_at": None}
    with patch("app.models.gex_weekly.find_latest_for_expiry", return_value=snap) as latest, \
         patch("app.models.gex_weekly.find_by_symbol_expiry_trade_date", return_value=None) as exact:
        resp = client.get("/api/gex?symbol=TSLA&type=weekly&expiry_date=2025-06-20")
    assert resp.status_code == 200, resp.get_json()
    assert latest.called
    assert not exact.called


def test_weekly_expiry_plus_explicit_date_fetches_exact_trade_date(client):
    """expiry_date + an explicit date → exact (expiry, trade_date) lookup so a
    paid user can step through individual days within a tab's week."""
    login_as(client, "paid", subscription_status="active")
    snap = {"gex_by_strike": [], "created_at": None}
    with patch("app.models.gex_weekly.find_by_symbol_expiry_trade_date", return_value=snap) as exact, \
         patch("app.models.gex_weekly.find_latest_for_expiry", return_value=None) as latest:
        resp = client.get("/api/gex?symbol=TSLA&type=weekly&expiry_date=2025-06-20&date=2025-06-18")
    assert resp.status_code == 200, resp.get_json()
    assert exact.called  # exact-day lookup used, not the latest-for-expiry path


# ═══════════════════════════════════════════════════════════════════════════
# Friday 3 PM CT rollover — clock-mocked boundary test
# ═══════════════════════════════════════════════════════════════════════════
def test_friday_rollover_before_cutoff_is_200(client):
    """At 2:59 PM CT on a Friday, the current expiry week is still valid."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    CT = ZoneInfo("America/Chicago")
    # Simulate Friday 2025-06-13 at 14:59 CT
    frozen_ct = _dt.datetime(2025, 6, 13, 14, 59, 0, tzinfo=CT)

    login_as(client, "free")
    with patch("app.routes.api.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_ct
        mock_dt.side_effect = lambda *a, **kw: _dt.datetime(*a, **kw)
        with patch("app.models.gex_weekly.find_by_symbol_trade_date", return_value=None), \
             patch("app.models.gex_weekly.find_latest_for_week", return_value=None), \
             patch("app.models.gex_intraday.find_latest", return_value=None):
            # Requesting the current expiry Friday itself — still current week at 14:59
            resp = client.get("/api/gex?symbol=TSLA&type=weekly&date=2025-06-13")
    assert resp.status_code == 200, resp.get_json()


def test_friday_rollover_after_cutoff_next_week_is_403(client):
    """After 3:00 PM CT on Friday, the just-expired week becomes 'last week';
    next week's Monday is now the current week — so the old Friday date is denied."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    CT = ZoneInfo("America/Chicago")
    # Simulate Friday 2025-06-13 at 15:01 CT — expiry has rolled
    frozen_ct = _dt.datetime(2025, 6, 13, 15, 1, 0, tzinfo=CT)

    login_as(client, "free")
    with patch("app.routes.api.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_ct
        mock_dt.side_effect = lambda *a, **kw: _dt.datetime(*a, **kw)
        # Requesting the *just-rolled* week's Monday — now in the past week
        resp = client.get("/api/gex?symbol=TSLA&type=weekly&date=2025-06-09")
    assert resp.status_code == 403, resp.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Friday 3 PM CT rollover — the stock 4-tab window (Past/This/Next/Week+2)
# must shift forward by exactly one week at the same boundary, not just the
# single-week index rule above. This is the case explicitly flagged as most
# likely to get wrong: after rollover, "Next Week" becomes "This Week", and
# "Past Week" falls out of range entirely (becomes 2-weeks-back → 403).
# ═══════════════════════════════════════════════════════════════════════════

def test_friday_rollover_stock_tab_window_shifts_forward_one_week():
    """Unit-tests _free_stock_tab_expiries() directly across the rollover
    boundary — isolates the window-computation logic from HTTP/DB concerns."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    from app.routes import api as api_module

    CT = ZoneInfo("America/Chicago")
    before_cutoff = _dt.datetime(2025, 6, 13, 14, 59, 0, tzinfo=CT)  # Friday, pre-rollover
    after_cutoff  = _dt.datetime(2025, 6, 13, 15, 1, 0, tzinfo=CT)   # Friday, post-rollover

    with patch("app.routes.api.datetime") as mock_dt:
        mock_dt.now.return_value = before_cutoff
        mock_dt.side_effect = lambda *a, **kw: _dt.datetime(*a, **kw)
        window_before = api_module._free_stock_tab_expiries()

    with patch("app.routes.api.datetime") as mock_dt:
        mock_dt.now.return_value = after_cutoff
        mock_dt.side_effect = lambda *a, **kw: _dt.datetime(*a, **kw)
        window_after = api_module._free_stock_tab_expiries()

    # Note: 2025-07-04 (Independence Day) is a market holiday and falls on a
    # Friday this year, so the holiday-adjusted expiry steps back to 07-03.
    assert window_before == {date(2025, 6, 6), date(2025, 6, 13), date(2025, 6, 20), date(2025, 6, 27)}
    assert window_after  == {date(2025, 6, 13), date(2025, 6, 20), date(2025, 6, 27), date(2025, 7, 3)}

    # Old "Next Week" (06-20) is still in range post-rollover — it's now "This Week".
    assert date(2025, 6, 20) in window_before and date(2025, 6, 20) in window_after
    # Old "Past Week" (06-06) falls out of range entirely post-rollover.
    assert date(2025, 6, 6) in window_before and date(2025, 6, 6) not in window_after
    # Old "This Week" (06-13) becomes the new "Past Week" — still in range.
    assert date(2025, 6, 13) in window_before and date(2025, 6, 13) in window_after
    # A new "Week+2" (07-03, holiday-adjusted from 07-04) enters the window
    # post-rollover, was out-of-range before.
    assert date(2025, 7, 3) not in window_before and date(2025, 7, 3) in window_after


def test_friday_rollover_stock_past_week_tab_becomes_403_after_cutoff(client):
    """End-to-end: the Past Week tab (2025-06-06) is a 200 pre-rollover and a
    403 post-rollover once the window has shifted forward by one week."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    CT = ZoneInfo("America/Chicago")
    after_cutoff = _dt.datetime(2025, 6, 13, 15, 1, 0, tzinfo=CT)

    login_as(client, "free")
    with patch("app.routes.api.datetime") as mock_dt:
        mock_dt.now.return_value = after_cutoff
        mock_dt.side_effect = lambda *a, **kw: _dt.datetime(*a, **kw)
        resp = client.get("/api/gex?symbol=TSLA&type=weekly&expiry_date=2025-06-06")
    assert resp.status_code == 403, resp.get_json()


def test_friday_rollover_stock_next_week_tab_stays_200_after_cutoff(client):
    """End-to-end: the old Next Week tab (2025-06-20) remains a 200 after
    rollover — it has simply become the new This Week tab."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    CT = ZoneInfo("America/Chicago")
    after_cutoff = _dt.datetime(2025, 6, 13, 15, 1, 0, tzinfo=CT)

    login_as(client, "free")
    with patch("app.routes.api.datetime") as mock_dt, \
         patch("app.models.gex_weekly.find_latest_for_expiry", return_value=None), \
         patch("app.models.gex_intraday.find_latest", return_value=None):
        mock_dt.now.return_value = after_cutoff
        mock_dt.side_effect = lambda *a, **kw: _dt.datetime(*a, **kw)
        resp = client.get("/api/gex?symbol=TSLA&type=weekly&expiry_date=2025-06-20")
    assert resp.status_code == 200, resp.get_json()
