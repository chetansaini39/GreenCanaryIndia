"""NSE calendar and timezone helpers used only by Zerodha jobs."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def is_nse_trading_day(value: date) -> bool:
    calendar = mcal.get_calendar("NSE")
    return len(calendar.valid_days(start_date=value, end_date=value)) == 1


def in_time_window(now: datetime, start: str, end: str) -> bool:
    local = now.astimezone(IST).time().replace(second=0, microsecond=0)
    start_time = time.fromisoformat(start)
    end_time = time.fromisoformat(end)
    return start_time <= local <= end_time
