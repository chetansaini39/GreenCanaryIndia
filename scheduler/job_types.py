"""
Canonical GEX pipeline job identifiers and metadata.

Use these IDs everywhere new code writes job_name to pipeline_health or
registers APScheduler jobs. Legacy aliases remain accepted for reroute URLs
and for displaying older log entries.
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
        id=INTRADAY_0DTE_5MIN,
        label="Intraday 0DTE (clock-aligned, default 15 min)",
        schedule="Clock-aligned every N min (admin-configurable, default 15), 8:45 AM – 2:55 PM CT",
        writes="Intraday 0DTE snapshots (<code>gex_intraday</code>, type <code>0dte</code>)",
        symbols="Index symbols daily; stocks on Fridays only",
        aliases=("0dte_intraday",),
    ),
    JobType(
        id=EOD_ROLLING_5D_WEEKLY,
        label="EOD rolling 21d + weekly",
        schedule="2:50 PM CT on trading days",
        writes=(
            "End-of-day rolling 21-day chart data (<code>gex_rolling_21d</code>). "
            "Also writes a weekly snapshot (<code>gex_weekly</code>) on the last trading day of the week."
        ),
        symbols="All active symbols",
        aliases=("eod", "eod_gex"),
    ),
    JobType(
        id=MONTHLY_OPEX_3RD_FRIDAY,
        label="Monthly OPEX (Mon + Fri EOD)",
        schedule="3:05 PM CT every Monday and Friday",
        writes="Monthly OPEX snapshot (<code>gex_monthly_opex</code>)",
        symbols="All active symbols (index group + Mag7 stocks)",
        aliases=("monthly_opex",),
    ),
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
