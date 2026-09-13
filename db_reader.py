"""
DB reader — query helpers consumed by Flask routes (Module 04) and the
public/Twitter snapshot views (Module 08).

All functions accept a PyMongo Database object so they work in any context
(Flask via mongo.db, tests via a test-DB client, etc.).

datetime arguments for date fields should be Central Time midnight datetimes, e.g.:
    datetime(2024, 4, 19, tzinfo=ZoneInfo("America/Chicago"))
"""
from datetime import datetime

from app.models import (
    gex_intraday,
    gex_weekly,
    gex_monthly_opex,
    gex_rolling_21d,
    symbols_config,
)


# ── Intraday / 0DTE ───────────────────────────────────────────────────────

def get_intraday(db, symbol: str, trade_date: datetime) -> list[dict]:
    """All intraday snapshots for a symbol on a given trade date, time-ordered."""
    return gex_intraday.find_by_symbol_date(db, symbol, trade_date)


def get_0dte(db, symbol: str, trade_date: datetime) -> list[dict]:
    """0DTE intraday snapshots only (filters snapshot_type='0dte')."""
    return gex_intraday.find_by_symbol_date_range(
        db, symbol, trade_date, trade_date, snapshot_type="0dte"
    )


def get_latest_snapshot(db, symbol: str, snapshot_type: str | None = None) -> dict | None:
    """The most recent intraday snapshot for a symbol."""
    return gex_intraday.find_latest(db, symbol, snapshot_type)


def get_intraday_range(
    db,
    symbol: str,
    start: datetime,
    end: datetime,
    snapshot_type: str | None = None,
) -> list[dict]:
    """Intraday snapshots for a symbol across a date range."""
    return gex_intraday.find_by_symbol_date_range(db, symbol, start, end, snapshot_type)


# ── Weekly ─────────────────────────────────────────────────────────────────

def get_weekly(db, symbol: str, week_of: datetime) -> dict | None:
    """Weekly snapshot for a specific week (week_of = Monday midnight UTC)."""
    return gex_weekly.find_by_symbol_week(db, symbol, week_of)


def get_weekly_trend(db, symbol: str, trade_date: datetime) -> list[dict]:
    """Summary (net_gex/call_wall/put_wall) across all N tracked weeks for one trade_date.

    Never includes gex_by_strike — safe to expose to free users.
    """
    return gex_weekly.find_trend_for_trade_date(db, symbol, trade_date)


def get_recent_weekly(db, symbol: str, n: int = 5) -> list[dict]:
    """Last n weekly snapshots, oldest-first (for trend charts)."""
    return list(reversed(gex_weekly.find_recent(db, symbol, n)))


def get_weekly_range(db, symbol: str, start: datetime, end: datetime) -> list[dict]:
    return gex_weekly.find_by_symbol_date_range(db, symbol, start, end)


def get_weekly_evolution(db, symbol: str, expiry_date: datetime) -> list[dict]:
    """Every trade_date snapshot for a specific upcoming Friday expiry, oldest
    first — the day-by-day GEX evolution for that week (Module 11)."""
    return gex_weekly.find_by_expiry(db, symbol, expiry_date)


# ── Monthly OPEX ───────────────────────────────────────────────────────────

def get_monthly_trend(db, symbol: str, trade_date: datetime) -> list[dict]:
    """Summary (net_gex/call_wall/put_wall) across all M tracked OPEX cycles for one trade_date.

    Never includes gex_by_strike — safe to expose to free users.
    """
    return gex_monthly_opex.find_trend_for_trade_date(db, symbol, trade_date)


def get_monthly_opex(db, symbol: str, trade_date) -> dict | None:
    """Monthly OPEX snapshot for a given trade_date (CT midnight datetime)."""
    doc = gex_monthly_opex.find_by_symbol_trade_date(db, symbol, trade_date)
    if doc is None:
        doc = gex_monthly_opex.find_latest_for_symbol(db, symbol)
    return doc


def get_recent_monthly_opex(db, symbol: str, n: int = 6) -> list[dict]:
    """Last n monthly OPEX snapshots, oldest-first."""
    return list(reversed(gex_monthly_opex.find_recent_cycles(db, symbol, n)))


# ── Rolling 21-day EOD ─────────────────────────────────────────────────────

def get_rolling_21d(db, symbol: str, n: int = 21) -> list[dict]:
    """Last n EOD snapshots for a symbol, oldest-first (for trend charts).

    Full history is stored; this slices the most recent n trade_dates.
    """
    return gex_rolling_21d.find_last_n(db, symbol, n)


def get_rolling_eod_range(db, symbol: str, start: datetime, end: datetime) -> list[dict]:
    return gex_rolling_21d.find_by_symbol_date_range(db, symbol, start, end)


# ── Symbol config ──────────────────────────────────────────────────────────

def get_free_symbols(db) -> list[dict]:
    """Active symbols available on the free tier."""
    return symbols_config.find_by_tier(db, "free")


def get_paid_symbols(db) -> list[dict]:
    """All active symbols (free + paid tier)."""
    return symbols_config.find_all_active(db)


def get_symbols_for_role(db, role: str) -> list[dict]:
    """Return the symbol list appropriate for a given user role."""
    if role in ("paid", "staff", "admin"):
        return get_paid_symbols(db)
    return get_free_symbols(db)
