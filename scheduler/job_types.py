"""
Canonical GEX pipeline job identifiers and metadata.

Use these IDs everywhere new code writes job_name to pipeline_health or
registers APScheduler jobs.

INTRADAY_0DTE_5MIN / EOD_ROLLING_5D_WEEKLY / MONTHLY_OPEX_3RD_FRIDAY were the
US-market (Schwab/CBOE/yfinance) job ids, removed from JOB_TYPES in this
fork. The constants are kept only so job_label()/resolve_job_id() can still
render pre-existing pipeline_health log entries that used them; they are no
longer runnable via rerun_job().
"""
from dataclasses import dataclass

# ── Canonical job IDs ─────────────────────────────────────────────────────

INTRADAY_0DTE_5MIN = "intraday_0dte_5min"
EOD_ROLLING_5D_WEEKLY = "eod_rolling_5d_weekly"
MONTHLY_OPEX_3RD_FRIDAY = "monthly_opex_3rd_friday"
ZERODHA_NIFTY_INTRADAY = "zerodha_nifty_intraday"
ZERODHA_NIFTY_EOD = "zerodha_nifty_eod"
ZERODHA_NIFTY_MONTHLY = "zerodha_nifty_monthly"


@dataclass(frozen=True)
class JobType:
    id: str
    label: str
    schedule: str
    writes: str
    symbols: str
    aliases: tuple[str, ...] = ()


JOB_TYPES: tuple[JobType, ...] = (
    JobType(
        id=ZERODHA_NIFTY_INTRADAY,
        label="NIFTY intraday 0DTE",
        schedule="Admin-configurable IST window and interval (disabled by default)",
        writes="NIFTY intraday snapshots to isolated Zerodha MongoDB",
        symbols="NIFTY on actual expiry days",
    ),
    JobType(
        id=ZERODHA_NIFTY_EOD,
        label="NIFTY EOD + actual expiries",
        schedule="Admin-configurable IST capture times (disabled by default)",
        writes="NIFTY rolling and weekly-expiry snapshots to isolated Zerodha MongoDB",
        symbols="NIFTY",
    ),
    JobType(
        id=ZERODHA_NIFTY_MONTHLY,
        label="NIFTY monthly expiries",
        schedule="Admin-configurable IST weekdays/time (disabled by default)",
        writes="NIFTY monthly-expiry snapshots to isolated Zerodha MongoDB",
        symbols="NIFTY",
    ),
)

_ALIASES: dict[str, str] = {}
for _job in JOB_TYPES:
    _ALIASES[_job.id] = _job.id
    for _alias in _job.aliases:
        _ALIASES[_alias] = _job.id


def resolve_job_id(job: str) -> str | None:
    """Map a route param or legacy alias to the canonical job id."""
    return _ALIASES.get(job)


def job_label(job_id: str) -> str:
    """Human-readable label for a canonical id or legacy alias."""
    canonical = resolve_job_id(job_id) or job_id
    for job in JOB_TYPES:
        if job.id == canonical:
            return job.label
    return job_id
