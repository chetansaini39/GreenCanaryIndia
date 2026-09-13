"""
GEX collection scheduler — entry point.

Run as a standalone process, completely separate from the Flask web app:
    python -m scheduler.runner

⚠  Single-instance constraint (from spec):
   Only ONE instance of this process may run at a time.  Multiple instances
   touching the same Schwab token file will corrupt it and cause duplicate
   writes.  A PID lock file (scheduler.pid) is used to enforce this.

Scheduling table (all times CT / America/Chicago):

  Job                              Trigger                      Notes
  ─────────────────────────────    ─────────────────────────    ──────────────────────────
  intraday_0dte_5min               Cron, clock-aligned          Fires at multiples of
                                    minute='*/N' (default N=15)  platform_settings.gex_0dte_
                                                                  interval_minutes; guard:
                                                                  8:45-14:55 CT + trading day;
                                                                  stocks Friday only
  eod_rolling_5d_weekly            Cron 14:50                   Rolling 21d + weekly
  monthly_opex_3rd_friday          Cron Friday 15:05            3rd Friday only
  social_eod                       Cron 15:15                   Review-only SPX EOD draft
"""
import atexit
import logging
import os
import signal
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler.runner")

_TIMEZONE = os.environ.get("TIMEZONE", "America/Chicago")
_IST_TIMEZONE = "Asia/Kolkata"
_PID_FILE = Path(__file__).parent.parent / "scheduler.pid"
_zerodha_schedule_signature = None
_settings_client = None


# ── PID lock — enforce single instance ────────────────────────────────────

def _acquire_pid_lock() -> None:
    if _PID_FILE.exists():
        existing_pid = _PID_FILE.read_text().strip()
        # Check if the process is actually still running
        try:
            os.kill(int(existing_pid), 0)
            log.error(
                "Another scheduler instance is already running (PID %s). Exiting.",
                existing_pid,
            )
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            log.warning("Stale PID file found (PID %s). Removing.", existing_pid)

    _PID_FILE.write_text(str(os.getpid()))
    atexit.register(_release_pid_lock)


def _release_pid_lock() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Graceful shutdown ──────────────────────────────────────────────────────

def _shutdown(signum, frame, scheduler: BlockingScheduler) -> None:
    log.info("Received signal %s — shutting down scheduler gracefully …", signum)
    scheduler.shutdown(wait=False)
    _release_pid_lock()
    sys.exit(0)


# ── Scheduler setup ────────────────────────────────────────────────────────

# ── Social post schedule times (CT) ───────────────────────────────────────
# Pre-Market remains 7:45 AM CT. EOD is 3:15 PM CT / 4:15 PM ET and review-only.
SOCIAL_PREMARKET_HOUR,   SOCIAL_PREMARKET_MINUTE   = 7,  45

SOCIAL_EOD_HOUR,         SOCIAL_EOD_MINUTE         = 15, 15
SOCIAL_EOW_HOUR,         SOCIAL_EOW_MINUTE         = 15, 15


def _get_0dte_interval_minutes() -> int:
    """Read the admin-configurable intraday interval from platform_settings.

    Falls back to the 15-min default if Mongo is unreachable at startup —
    the scheduler should still come up rather than crash on a transient
    DB hiccup.
    """
    try:
        from app.models import platform_settings
        return platform_settings.get_0dte_interval_minutes(_settings_db())
    except Exception as exc:
        log.warning("Could not read gex_0dte_interval_minutes, defaulting to 15: %s", exc)
        return 15


def _reschedule_intraday_job(sched: BlockingScheduler) -> None:
    """Check platform_settings for an updated 0DTE interval and, if it has
    changed since this job was registered, remove and re-add it with a
    new clock-aligned CronTrigger.
    """
    from scheduler.jobs.gex_collection import run_0dte_intraday
    from scheduler.job_types import INTRADAY_0DTE_5MIN, job_label

    job = sched.get_job(INTRADAY_0DTE_5MIN)
    if job is None:
        return

    current_minutes = _get_0dte_interval_minutes()
    current_expr = f"*/{current_minutes}"
    if getattr(job.trigger, "fields", None):
        minute_field = next((f for f in job.trigger.fields if f.name == "minute"), None)
        if minute_field is not None and str(minute_field.expressions[0]) == current_expr:
            return

    sched.remove_job(INTRADAY_0DTE_5MIN)
    sched.add_job(
        run_0dte_intraday,
        trigger=CronTrigger(minute=current_expr, timezone=_TIMEZONE),
        id=INTRADAY_0DTE_5MIN,
        name=job_label(INTRADAY_0DTE_5MIN),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    log.info(
        "Intraday 0DTE interval changed — rescheduled to minute='%s' (every %s min).",
        current_expr, current_minutes,
    )


def _settings_db():
    global _settings_client
    from pymongo import MongoClient
    from app.utils.time import mongo_client_kwargs

    if _settings_client is None:
        _settings_client = MongoClient(
            os.environ["MONGO_URI"], **mongo_client_kwargs()
        )
    return _settings_client.get_default_database()


def _read_zerodha_settings() -> dict:
    from app.models import platform_settings

    return platform_settings.get_zerodha_settings(_settings_db())


def _sync_zerodha_jobs(sched: BlockingScheduler) -> None:
    """Apply Pipeline Health schedule settings without restarting the process."""
    global _zerodha_schedule_signature
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from data_sources import zerodha_client, zerodha_stream
    from scheduler.job_types import (
        ZERODHA_NIFTY_EOD,
        ZERODHA_NIFTY_INTRADAY,
        ZERODHA_NIFTY_MONTHLY,
    )
    from scheduler.jobs.gex_collection import (
        run_zerodha_eod,
        run_zerodha_intraday,
        run_zerodha_monthly,
    )

    try:
        settings = _read_zerodha_settings()
    except Exception as exc:
        log.warning("Could not read Zerodha schedule settings: %s", exc)
        return

    signature = (
        settings["zerodha_schedule_revision"],
        datetime.now(ZoneInfo(_IST_TIMEZONE)).date().isoformat(),
    )
    if signature == _zerodha_schedule_signature:
        return

    for job in list(sched.get_jobs()):
        if job.id == ZERODHA_NIFTY_INTRADAY or job.id == ZERODHA_NIFTY_MONTHLY \
                or job.id.startswith(f"{ZERODHA_NIFTY_EOD}_"):
            sched.remove_job(job.id)

    automation = settings["zerodha_automation_enabled"]
    if automation and settings["zerodha_intraday_enabled"]:
        sched.add_job(
            run_zerodha_intraday,
            trigger=CronTrigger(
                minute=f"*/{settings['zerodha_intraday_interval_minutes']}",
                timezone=_IST_TIMEZONE,
            ),
            id=ZERODHA_NIFTY_INTRADAY,
            name="Zerodha: NIFTY intraday",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
    if automation and settings["zerodha_eod_enabled"]:
        for value in settings["zerodha_eod_times"]:
            hour, minute = (int(part) for part in value.split(":"))
            sched.add_job(
                run_zerodha_eod,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=_IST_TIMEZONE),
                id=f"{ZERODHA_NIFTY_EOD}_{hour:02d}{minute:02d}",
                name=f"Zerodha: NIFTY EOD ({value} IST)",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
    if automation and settings["zerodha_monthly_enabled"]:
        hour, minute = (int(part) for part in settings["zerodha_monthly_time"].split(":"))
        sched.add_job(
            run_zerodha_monthly,
            trigger=CronTrigger(
                day_of_week=",".join(settings["zerodha_monthly_weekdays"]),
                hour=hour,
                minute=minute,
                timezone=_IST_TIMEZONE,
            ),
            id=ZERODHA_NIFTY_MONTHLY,
            name="Zerodha: NIFTY monthly expiries",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    if automation and settings["zerodha_ws_enabled"]:
        try:
            tokens = zerodha_client.subscription_tokens(
                settings["zerodha_weekly_forward_expiries"]
            )
            zerodha_stream.start(
                os.environ["ZERODHA_API_KEY"],
                os.environ["ZERODHA_ACCESS_TOKEN"],
                tokens,
            )
        except Exception as exc:
            log.error("Zerodha WebSocket setup failed: %s", exc)
    else:
        zerodha_stream.stop()

    _zerodha_schedule_signature = signature
    log.info("Applied Zerodha schedule revision %s", settings["zerodha_schedule_revision"])


def build_scheduler() -> BlockingScheduler:
    from scheduler.jobs.gex_collection import (
        run_0dte_intraday,
        run_eod,
        run_monthly_opex_check,
    )
    from scheduler.jobs.twitter_post import run_twitter_post
    from scheduler.jobs.social_post import (
        run_premarket_post,
        run_eod_post,
        run_eow_post,
    )
    from scheduler.job_types import (
        EOD_ROLLING_5D_WEEKLY,
        INTRADAY_0DTE_5MIN,
        MONTHLY_OPEX_3RD_FRIDAY,
        job_label,
    )

    sched = BlockingScheduler(timezone=_TIMEZONE)

    interval_minutes = _get_0dte_interval_minutes()
    sched.add_job(
        run_0dte_intraday,
        trigger=CronTrigger(minute=f"*/{interval_minutes}", timezone=_TIMEZONE),
        id=INTRADAY_0DTE_5MIN,
        name=job_label(INTRADAY_0DTE_5MIN),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # EOD runs twice per trading day (Module 02): a 9:00 AM CT capture of the
    # morning state and the primary 2:50 PM CT capture. Both call run_eod —
    # identical scope and idempotent upsert write logic, same trade_date — so
    # the 2:50 PM run simply refreshes each tracked expiry's document written
    # in the morning. Distinct APScheduler ids (the second suffixed "_am");
    # both log to pipeline_health under the same job_name.
    sched.add_job(
        run_eod,
        trigger=CronTrigger(hour=9, minute=0, timezone=_TIMEZONE),
        id=f"{EOD_ROLLING_5D_WEEKLY}_am",
        name=f"{job_label(EOD_ROLLING_5D_WEEKLY)} (9:00 AM)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    sched.add_job(
        run_eod,
        trigger=CronTrigger(hour=14, minute=50, timezone=_TIMEZONE),
        id=EOD_ROLLING_5D_WEEKLY,
        name=job_label(EOD_ROLLING_5D_WEEKLY),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    sched.add_job(
        run_monthly_opex_check,
        trigger=CronTrigger(
            day_of_week="mon,fri",
            hour=15,
            minute=5,
            timezone=_TIMEZONE,
        ),
        id=MONTHLY_OPEX_3RD_FRIDAY,
        name=job_label(MONTHLY_OPEX_3RD_FRIDAY),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    for hour, minute in [(8, 0), (9, 30), (15, 30)]:
        sched.add_job(
            run_twitter_post,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=_TIMEZONE),
            id=f"twitter_post_{hour:02d}{minute:02d}",
            name=f"Twitter GEX Post {hour:02d}:{minute:02d}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    # ── Module 10: Social Post Studio scheduled jobs ───────────────────────
    sched.add_job(
        run_premarket_post,
        trigger=CronTrigger(
            hour=SOCIAL_PREMARKET_HOUR, minute=SOCIAL_PREMARKET_MINUTE,
            timezone=_TIMEZONE,
        ),
        id="social_premarket",
        name="Social: Pre-Market Brief",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    sched.add_job(
        run_eod_post,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=SOCIAL_EOD_HOUR, minute=SOCIAL_EOD_MINUTE,
            timezone=_TIMEZONE,
        ),
        id="social_eod",
        name="Social: EOD Report",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    sched.add_job(
        run_eow_post,
        trigger=CronTrigger(
            day_of_week="fri",
            hour=SOCIAL_EOW_HOUR, minute=SOCIAL_EOW_MINUTE,
            timezone=_TIMEZONE,
        ),
        id="social_eow",
        name="Social: EOW Wrap",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    return sched


# ── Main ──────────────────────────────────────────────────────────────────

def _write_heartbeat(started_at) -> None:
    """Write a liveness heartbeat to MongoDB. Called every 2 minutes by APScheduler."""
    try:
        from pymongo import MongoClient
        from app.models import scheduler_heartbeat
        from app.utils.time import mongo_client_kwargs
        client = MongoClient(os.environ["MONGO_URI"], **mongo_client_kwargs())
        db = client.get_default_database()
        scheduler_heartbeat.write_heartbeat(db, process_started_at=started_at)
    except Exception as exc:
        log.warning("Heartbeat write failed: %s", exc)


if __name__ == "__main__":
    _acquire_pid_lock()

    scheduler = build_scheduler()
    _sync_zerodha_jobs(scheduler)

    # Heartbeat job — runs every 2 minutes regardless of trading hours
    _process_started_at = __import__("datetime").datetime.now(
        __import__("zoneinfo").ZoneInfo(_TIMEZONE)
    )
    scheduler.add_job(
        lambda: _write_heartbeat(_process_started_at),
        trigger=IntervalTrigger(minutes=2, timezone=_TIMEZONE),
        id="scheduler_heartbeat",
        name="Scheduler Liveness Heartbeat",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Watches platform_settings.gex_0dte_interval_minutes and reschedules the
    # intraday job (remove + re-add with a new CronTrigger) if an admin
    # changes it — picked up without a scheduler restart.
    scheduler.add_job(
        lambda: _reschedule_intraday_job(scheduler),
        trigger=IntervalTrigger(minutes=2, timezone=_TIMEZONE),
        id="intraday_interval_watcher",
        name="Intraday 0DTE Interval Watcher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        lambda: _sync_zerodha_jobs(scheduler),
        trigger=IntervalTrigger(minutes=2, timezone=_TIMEZONE),
        id="zerodha_schedule_watcher",
        name="Zerodha Schedule and WebSocket Watcher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Module 11: picks up manual refresh requests queued by the MCP server's
    # trigger_data_refresh tool. Kept here (not in the MCP process) so Schwab
    # access stays isolated to this single scheduler process.
    from scheduler.jobs.data_refresh_requests import run_pending_data_refresh_requests
    scheduler.add_job(
        run_pending_data_refresh_requests,
        trigger=IntervalTrigger(minutes=2, timezone=_TIMEZONE),
        id="data_refresh_requests_poll",
        name="MCP Data Refresh Requests",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Write an immediate heartbeat so the /healthz/scheduler endpoint doesn't
    # report stale for the first 2 minutes after startup.
    _write_heartbeat(_process_started_at)

    # Register SIGTERM / SIGINT handlers for clean shutdown
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, f: _shutdown(s, f, scheduler))

    log.info("GEX scheduler starting. PID %s. Timezone: %s.", os.getpid(), _TIMEZONE)
    log.info("Jobs registered: %s", [j.id for j in scheduler.get_jobs()])

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
    finally:
        _release_pid_lock()
