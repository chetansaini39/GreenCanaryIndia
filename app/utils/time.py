"""
Central timezone utilities.

All timestamps written to MongoDB should use now_ct() so that every
created_at / run_at / last_login_at etc. is expressed in Central Time,
per docs/spec/03-database-schema.md and docs/spec/07-deployment-infra.md.

The timezone is driven by the TIMEZONE env var (default America/Chicago)
so it is a single source of truth rather than being hardcoded in every file.
"""
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))


def now_ct() -> datetime:
    """Return the current moment as a tz-aware Central Time datetime."""
    return datetime.now(_tz())


def market_midnight(value: date, timezone_name: str) -> datetime:
    """Represent a logical market date at midnight in its own timezone."""
    return datetime.combine(value, time.min, tzinfo=ZoneInfo(timezone_name))


def mongo_client_kwargs() -> dict:
    """Kwargs for MongoClient / PyMongo.init_app so stored datetimes are
    returned as Central-Time-aware rather than naive UTC.

    Without this, PyMongo hands back naive UTC wall-clock, which makes
    server-side strftime and API isoformat emit UTC values under a CT label.
    """
    return {"tz_aware": True, "tzinfo": _tz()}
