"""
Manual pipeline job triggers for the admin panel.

Wraps scheduler.jobs.gex_collection so the Flask app can re-run a job for
NIFTY (the only symbol/provider this fork supports) without starting the
full scheduler process.
"""
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from scheduler.job_types import (
    JOB_TYPES,
    JobType,
    ZERODHA_NIFTY_EOD,
    ZERODHA_NIFTY_INTRADAY,
    ZERODHA_NIFTY_MONTHLY,
)

log = logging.getLogger(__name__)
CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))

_JOB_REGISTRY: dict | None = None


def _registry():
    global _JOB_REGISTRY
    if _JOB_REGISTRY is None:
        from scheduler.jobs import gex_collection as gc

        _canonical = {
            ZERODHA_NIFTY_INTRADAY: lambda: gc.run_zerodha_intraday(manual=True),
            ZERODHA_NIFTY_EOD: lambda: gc.run_zerodha_eod(manual=True),
            ZERODHA_NIFTY_MONTHLY: lambda: gc.run_zerodha_monthly(manual=True),
        }
        _JOB_REGISTRY = {}
        for job_id, fn in _canonical.items():
            _JOB_REGISTRY[job_id] = (job_id, fn)
        for job in JOB_TYPES:
            _, fn = _JOB_REGISTRY[job.id]
            for alias in job.aliases:
                _JOB_REGISTRY[alias] = (job.id, fn)
    return _JOB_REGISTRY


def available_jobs() -> list[JobType]:
    return list(JOB_TYPES)


def rerun_job(job: str, symbol: str | None = None, trade_date: date | None = None) -> dict:
    """Trigger a pipeline job manually.

    All jobs in this fork are Zerodha/NIFTY jobs, which run against the live
    chain — *symbol* (if given) must be "NIFTY", and *trade_date* (if given)
    must be today in IST.
    """
    registry = _registry()
    if job not in registry:
        return {"ok": False, "error": f"unknown job '{job}'"}

    job_name, fn = registry[job]

    if symbol and symbol.upper().strip() != "NIFTY":
        return {"ok": False, "error": "Zerodha jobs currently support NIFTY only"}
    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if trade_date and trade_date != today_ist:
        return {
            "ok": False,
            "error": "Zerodha manual jobs use the live chain and only accept today's IST date",
        }
    try:
        fn()
        return {"ok": True, "job": job_name, "symbol": "NIFTY", "detail": "live manual run"}
    except Exception as exc:
        log.exception("Manual Zerodha rerun of %s failed", job_name)
        return {"ok": False, "error": str(exc)}


def backfill_forward_window(symbol: str | None = None) -> dict:
    """Populate NIFTY's tracked weekly expiries and monthly OPEX cycles using
    today's live chain data. NIFTY is the only symbol/provider this fork
    supports, so any other named symbol is rejected.

    Reuses the exact same write logic as the scheduled Zerodha EOD/monthly
    jobs — idempotent upserts, so safe to run even if data already exists.
    """
    if symbol and symbol.upper().strip() != "NIFTY":
        return {"ok": False, "error": "backfill currently supports NIFTY only"}

    from scheduler.jobs.gex_collection import run_zerodha_eod, run_zerodha_monthly

    try:
        run_zerodha_eod(manual=True)
        run_zerodha_monthly(manual=True)
        return {
            "ok": True,
            "detail": "backfill-forward-window for NIFTY actual expiries",
            "symbols_processed": 1,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "symbols_processed": 0}
