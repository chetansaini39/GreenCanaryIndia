"""
Market calendar and trading-window helpers.

All times are in US/Chicago (CT) — the exchange's local zone — so the
scheduler's APScheduler instance should also be set to America/Chicago.

Trading window assumed:
  Market session: 8:30 AM CT open (9:30 AM ET)
  0DTE intraday:  8:45 AM–2:55 PM CT  (per spec scheduling table)
  General daily:  8:30 AM–3:00 PM CT
"""
import logging
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))

# ── 0DTE intraday window (spec: 8:45 AM – 2:55 PM CT) ────────────────────
_0DTE_START = time(8, 45)
_0DTE_END = time(14, 55)

# ── General daily window ──────────────────────────────────────────────────
_DAILY_START = time(8, 30)
_DAILY_END = time(15, 0)


# ── Trading-day check ──────────────────────────────────────────────────────

def is_trading_day(check_date: date | None = None) -> bool:
    """Return True if check_date (defaults to today CT) is a NYSE trading day.

    Uses pandas_market_calendars; falls back to a weekday-only check if
    the library raises an unexpected error.
    """
    if check_date is None:
        check_date = datetime.now(CT).date()

    try:
        import pandas_market_calendars as mcal

        nyse = mcal.get_calendar("NYSE")
        valid_days = nyse.valid_days(
            start_date=check_date.isoformat(),
            end_date=check_date.isoformat(),
        )
        return len(valid_days) > 0
    except Exception as exc:
        log.warning(
            "pandas_market_calendars unavailable (%s). Falling back to weekday check.",
            exc,
        )
        return check_date.weekday() < 5  # Mon–Fri only (ignores holidays)


def is_holiday(check_date: date | None = None) -> bool:
    return not is_trading_day(check_date)


# ── Time-window checks ────────────────────────────────────────────────────

def _now_ct() -> datetime:
    return datetime.now(CT)


def is_in_0dte_window() -> bool:
    """True if current CT time is within the 0DTE intraday job window
    AND today is a trading day."""
    now = _now_ct()
    return is_trading_day(now.date()) and _0DTE_START <= now.time() <= _0DTE_END


def is_in_daily_window() -> bool:
    """True if current CT time is within general trading hours."""
    now = _now_ct()
    return is_trading_day(now.date()) and _DAILY_START <= now.time() <= _DAILY_END


# ── Calendar helpers ───────────────────────────────────────────────────────

def is_friday(check_date: date | None = None) -> bool:
    if check_date is None:
        check_date = _now_ct().date()
    return check_date.weekday() == 4


def is_third_friday(check_date: date | None = None) -> bool:
    """True if check_date is the 3rd Friday of its month (monthly OPEX day)."""
    if check_date is None:
        check_date = _now_ct().date()
    if check_date.weekday() != 4:
        return False
    # 3rd Friday: day number falls in [15, 21]
    return 15 <= check_date.day <= 21


def week_of_monday(check_date: date | None = None) -> date:
    """Return the Monday of the week containing check_date."""
    if check_date is None:
        check_date = _now_ct().date()
    return check_date - __import__("datetime").timedelta(days=check_date.weekday())


def third_friday_of_month(year: int, month: int) -> date:
    """Return the 3rd Friday of the given year/month."""
    d = date(year, month, 1)
    days_until_friday = (4 - d.weekday()) % 7  # weekday 4 = Friday
    return d + timedelta(days=days_until_friday + 14)


def upcoming_opex_expiry(today: date | None = None) -> date:
    """3rd Friday this OPEX cycle is targeting.

    When today IS the 3rd Friday (the expiry day itself), returns next month's
    3rd Friday — the rollover case where the run seeds the new cycle instead of
    adding another point to the expiring one.
    """
    if today is None:
        today = _now_ct().date()

    this_month_opex = third_friday_of_month(today.year, today.month)
    if today < this_month_opex:
        return this_month_opex
    # today >= this_month_opex → next month
    if today.month == 12:
        return third_friday_of_month(today.year + 1, 1)
    return third_friday_of_month(today.year, today.month + 1)


def next_n_upcoming_fridays(n: int, from_date: date | None = None) -> list[date]:
    """Return the next N weekly Friday expiry dates, starting from (and including)
    the nearest Friday on or after from_date.

    If a computed Friday is a holiday the previous Thursday is used as fallback,
    matching the existing single-week EOD behavior.
    """
    if from_date is None:
        from_date = _now_ct().date()
    days_to_friday = (4 - from_date.weekday()) % 7  # 0 when from_date is already Friday
    first_friday = from_date + timedelta(days=days_to_friday)
    result = []
    for i in range(n):
        friday = first_friday + timedelta(weeks=i)
        if not is_trading_day(friday):
            friday -= timedelta(days=1)
        result.append(friday)
    return result


def next_m_upcoming_opex_cycles(m: int, from_date: date | None = None) -> list[date]:
    """Return the next M upcoming monthly OPEX (3rd-Friday) expiry dates.

    'Upcoming' means strictly after from_date — a 3rd Friday that IS from_date
    is no longer upcoming (it's expiring today) and falls out of the window,
    matching the sliding-window spec for gex_monthly_opex.
    """
    if from_date is None:
        from_date = _now_ct().date()
    result = []
    year, month = from_date.year, from_date.month
    while len(result) < m:
        candidate = third_friday_of_month(year, month)
        if candidate > from_date:
            result.append(candidate)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return result


def next_n_trading_days(n: int, from_date: date | None = None) -> list[date]:
    """Return the next *n* trading days after from_date, skipping weekends/holidays."""
    if from_date is None:
        from_date = _now_ct().date()
    result = []
    candidate = from_date + timedelta(days=1)
    while len(result) < n:
        if is_trading_day(candidate):
            result.append(candidate)
        candidate += timedelta(days=1)
    return result


def is_monday(check_date: date | None = None) -> bool:
    if check_date is None:
        check_date = _now_ct().date()
    return check_date.weekday() == 0


def trade_date_ct(check_date: date | None = None) -> datetime:
    """Return a Central Time midnight datetime for the given trade date.

    All trade_date fields in MongoDB are stored as CT midnight per the
    timezone standard in the spec — this must match what the API routes
    query against, or no records will be found.
    """
    if check_date is None:
        check_date = _now_ct().date()
    return datetime(check_date.year, check_date.month, check_date.day, tzinfo=CT)
