"""
Manual pipeline job triggers for the admin panel.

Wraps scheduler.jobs.gex_collection so the Flask app can re-run a job for a
specific symbol or for all symbols on a chosen date without starting the
full scheduler process.
"""
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.utils.time import now_ct as _now_ct, mongo_client_kwargs
from scheduler.job_types import (
    EOD_ROLLING_5D_WEEKLY,
    INTRADAY_0DTE_5MIN,
    JOB_TYPES,
    JobType,
    MONTHLY_OPEX_3RD_FRIDAY,
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
            INTRADAY_0DTE_5MIN: gc.run_0dte_intraday,
            EOD_ROLLING_5D_WEEKLY: gc.run_eod,
            MONTHLY_OPEX_3RD_FRIDAY: gc.run_monthly_opex_check,
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

    - *symbol* set → process that symbol only (optional *trade_date* for backfill).
    - *trade_date* set, no symbol → process all applicable symbols for that date.
    - neither set → run the full scheduled job for today (scheduler guards apply).
    """
    registry = _registry()
    if job not in registry:
        return {"ok": False, "error": f"unknown job '{job}'"}

    job_name, fn = registry[job]

    if job_name in {ZERODHA_NIFTY_INTRADAY, ZERODHA_NIFTY_EOD, ZERODHA_NIFTY_MONTHLY}:
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

    if symbol:
        return _rerun_single_symbol(job_name, symbol, trade_date)
    if trade_date:
        return _rerun_all_symbols(job_name, trade_date)

    try:
        fn()
        return {"ok": True, "job": job_name, "detail": "full job triggered"}
    except Exception as exc:
        log.exception("Manual rerun of %s failed", job_name)
        return {"ok": False, "error": str(exc)}


def _symbols_for_job(db, job_name: str, run_date: date) -> list[dict]:
    from app.models import symbols_config
    from scheduler.market_utils import is_friday

    all_symbols = [
        row for row in symbols_config.find_all_active(db)
        if str(row.get("provider") or "schwab").lower() != "zerodha"
    ]
    if job_name == INTRADAY_0DTE_5MIN:
        return [
            s for s in all_symbols
            if s["asset_type"] == "index"
            or (is_friday(run_date) and s["asset_type"] == "stock")
        ]
    if job_name == MONTHLY_OPEX_3RD_FRIDAY:
        return [s for s in all_symbols if s["asset_type"] == "index"]
    return all_symbols


def _process_symbol_job(
    db,
    job_name: str,
    symbol: str,
    asset_type: str,
    run_date: date,
    now: datetime,
) -> None:
    """Fetch, compute, and write GEX for one symbol. Raises on failure."""
    import gex_engine
    import db_writer
    from scheduler.jobs.gex_collection import _fetch_chain
    from scheduler.market_utils import (
        is_friday,
        is_monday,
        is_trading_day,
        trade_date_ct,
        upcoming_opex_expiry,
        week_of_monday,
    )

    td = trade_date_ct(run_date)
    chain, source = _fetch_chain(symbol, asset_type)
    result = gex_engine.compute(chain["options"], chain["spot_price"])

    if job_name == INTRADAY_0DTE_5MIN:
        db_writer.write_intraday(
            db,
            symbol=symbol,
            asset_type=asset_type,
            timestamp=now,
            trade_date=td,
            snapshot_type="0dte",
            source=source,
            spot_price=chain["spot_price"],
            gex_result=result,
        )
    elif job_name == EOD_ROLLING_5D_WEEKLY:
        db_writer.write_rolling_21d(
            db,
            symbol=symbol,
            asset_type=asset_type,
            trade_date=td,
            source=source,
            spot_price=chain["spot_price"],
            gex_result=result,
        )
        next_day = run_date + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        is_last_trading_day_of_week = is_friday(run_date) or (
            run_date.weekday() == 3 and not is_trading_day(next_day)
        )
        if is_last_trading_day_of_week:
            monday = trade_date_ct(week_of_monday(run_date))
            db_writer.write_weekly(
                db,
                symbol=symbol,
                asset_type=asset_type,
                week_of=monday,
                expiry_date=td,
                trade_date=td,
                source=source,
                spot_price=chain["spot_price"],
                gex_result=result,
            )
    elif job_name == MONTHLY_OPEX_3RD_FRIDAY:
        if asset_type != "index":
            raise ValueError("monthly OPEX applies to index symbols only")
        if not (is_monday(run_date) or is_friday(run_date)):
            raise ValueError("monthly OPEX reruns are only valid on Monday or Friday")
        expiry = upcoming_opex_expiry(run_date)
        expiry_dt = trade_date_ct(expiry)
        db_writer.write_monthly_opex(
            db,
            symbol=symbol,
            asset_type=asset_type,
            expiry_date=expiry_dt,
            trade_date=td,
            source=source,
            spot_price=chain["spot_price"],
            gex_result=result,
        )
    else:
        raise ValueError(f"rerun not supported for job '{job_name}'")


def _rerun_single_symbol(job_name: str, symbol: str, trade_date: date | None) -> dict:
    """Run GEX collection for one symbol only."""
    from pymongo import MongoClient

    from app.models import symbols_config
    from scheduler.jobs.gex_collection import _log_health
    from scheduler.market_utils import is_friday, is_monday

    symbol = symbol.upper().strip()
    client = MongoClient(os.environ["MONGO_URI"], **mongo_client_kwargs())
    db = client.get_default_database()

    sym_doc = symbols_config.find_by_symbol(db, symbol)
    if not sym_doc:
        client.close()
        return {"ok": False, "error": f"symbol '{symbol}' not found in symbols_config"}
    if str(sym_doc.get("provider") or "schwab").lower() == "zerodha":
        provider_jobs = {
            INTRADAY_0DTE_5MIN: ZERODHA_NIFTY_INTRADAY,
            EOD_ROLLING_5D_WEEKLY: ZERODHA_NIFTY_EOD,
            MONTHLY_OPEX_3RD_FRIDAY: ZERODHA_NIFTY_MONTHLY,
        }
        client.close()
        provider_job = provider_jobs.get(job_name)
        if not provider_job:
            return {"ok": False, "error": f"job '{job_name}' is not supported for Zerodha"}
        return rerun_job(provider_job, symbol=symbol, trade_date=trade_date)

    now = _now_ct()
    run_date = trade_date or now.date()
    asset_type = sym_doc["asset_type"]
    detail_extra = f"manual rerun for {symbol}"
    if trade_date:
        detail_extra += f" on {trade_date.isoformat()}"

    if job_name == MONTHLY_OPEX_3RD_FRIDAY:
        if asset_type != "index":
            return {"ok": False, "error": "monthly OPEX applies to index symbols only"}
        if not (is_monday(run_date) or is_friday(run_date)):
            return {"ok": False, "error": "monthly OPEX reruns are only valid on Monday or Friday"}

    try:
        _process_symbol_job(db, job_name, symbol, asset_type, run_date, now)
        _log_health(db, job_name, "success", detail_extra, 1)
        return {"ok": True, "job": job_name, "symbol": symbol, "detail": detail_extra}
    except Exception as exc:
        _log_health(db, job_name, "failed", f"{detail_extra}: {exc}", 0)
        log.exception("Single-symbol rerun failed for %s/%s", job_name, symbol)
        return {"ok": False, "error": str(exc)}
    finally:
        client.close()


def backfill_forward_window(symbol: str | None = None) -> dict:
    """Populate all N tracked weekly expiries and M tracked monthly OPEX cycles
    for one symbol or all symbols, using today's live chain data.

    Reuses the exact same write logic as run_eod() / run_monthly_opex_check() —
    idempotent upserts, so safe to run even if data already exists.
    """
    import gex_engine
    import db_writer
    from pymongo import MongoClient

    from app.models import symbols_config, platform_settings as ps
    from app.utils.time import now_ct
    from scheduler.jobs.gex_collection import _fetch_chain, _log_health
    from scheduler.market_utils import (
        next_n_upcoming_fridays,
        next_m_upcoming_opex_cycles,
        trade_date_ct,
        week_of_monday,
    )

    if symbol and symbol.upper().strip() == "NIFTY":
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

    client = MongoClient(os.environ["MONGO_URI"], **mongo_client_kwargs())
    db = client.get_default_database()
    now = now_ct()
    trade_date = trade_date_ct(now.date())

    n_weeks = ps.get_weekly_forward_weeks(db)
    m_cycles = ps.get_monthly_forward_cycles(db)
    upcoming_fridays = next_n_upcoming_fridays(n_weeks, from_date=now.date())
    upcoming_cycles = next_m_upcoming_opex_cycles(m_cycles, from_date=now.date())

    if symbol:
        symbol = symbol.upper().strip()
        sym_doc = symbols_config.find_by_symbol(db, symbol)
        if not sym_doc:
            client.close()
            return {"ok": False, "error": f"symbol '{symbol}' not found in symbols_config"}
        target_symbols = [sym_doc]
    else:
        target_symbols = [
            row for row in symbols_config.find_all_active(db)
            if str(row.get("provider") or "schwab").lower() != "zerodha"
        ]

    processed = 0
    errors: list[str] = []

    try:
        for sym_doc in target_symbols:
            sym = sym_doc["symbol"]
            asset_type = sym_doc["asset_type"]
            try:
                chain, source = _fetch_chain(sym, asset_type)
                result = gex_engine.compute(chain["options"], chain["spot_price"])

                for friday in upcoming_fridays:
                    expiry_dt = trade_date_ct(friday)
                    monday = trade_date_ct(week_of_monday(friday))
                    db_writer.write_weekly(
                        db,
                        symbol=sym,
                        asset_type=asset_type,
                        week_of=monday,
                        expiry_date=expiry_dt,
                        trade_date=trade_date,
                        source=source,
                        spot_price=chain["spot_price"],
                        gex_result=result,
                    )

                for expiry in upcoming_cycles:
                    expiry_dt = trade_date_ct(expiry)
                    db_writer.write_monthly_opex(
                        db,
                        symbol=sym,
                        asset_type=asset_type,
                        expiry_date=expiry_dt,
                        trade_date=trade_date,
                        source=source,
                        spot_price=chain["spot_price"],
                        gex_result=result,
                    )

                processed += 1
            except Exception as exc:
                msg = f"{sym}: {exc}"
                log.error("[backfill_forward_window] %s", msg)
                errors.append(msg)

        scope = symbol or "all symbols"
        detail = (
            f"backfill-forward-window for {scope}: "
            f"{n_weeks} weekly expiries + {m_cycles} monthly cycles"
        )
        if errors:
            detail += "; errors: " + "; ".join(errors)
        status = "success" if not errors else ("failed" if processed == 0 else "success")
        _log_health(db, "backfill_forward_window", status, detail, processed)

        if processed == 0:
            return {"ok": False, "error": detail, "symbols_processed": 0}
        return {"ok": True, "detail": detail, "symbols_processed": processed}
    finally:
        client.close()


def _rerun_all_symbols(job_name: str, trade_date: date) -> dict:
    """Run GEX collection for all applicable symbols on a specific date."""
    from pymongo import MongoClient

    from scheduler.jobs.gex_collection import _log_health
    from scheduler.market_utils import is_friday, is_monday, is_trading_day

    if not is_trading_day(trade_date):
        return {
            "ok": False,
            "error": f"{trade_date.isoformat()} is not a trading day",
        }

    if job_name == MONTHLY_OPEX_3RD_FRIDAY and not (is_monday(trade_date) or is_friday(trade_date)):
        return {"ok": False, "error": "monthly OPEX reruns are only valid on Monday or Friday"}

    client = MongoClient(os.environ["MONGO_URI"], **mongo_client_kwargs())
    db = client.get_default_database()
    now = _now_ct()

    try:
        target_symbols = _symbols_for_job(db, job_name, trade_date)
        if not target_symbols:
            return {"ok": False, "error": "no active symbols match this job and date"}

        processed = 0
        errors: list[str] = []
        for sym_doc in target_symbols:
            symbol = sym_doc["symbol"]
            asset_type = sym_doc["asset_type"]
            try:
                _process_symbol_job(db, job_name, symbol, asset_type, trade_date, now)
                processed += 1
            except Exception as exc:
                msg = f"{symbol}: {exc}"
                log.error("[%s] %s", job_name, msg)
                errors.append(msg)

        detail = f"manual rerun for all symbols on {trade_date.isoformat()}"
        if errors:
            detail += "; " + "; ".join(errors)
        status = "success" if not errors else ("failed" if processed == 0 else "success")
        _log_health(db, job_name, status, detail, processed)

        if processed == 0:
            return {"ok": False, "error": detail, "symbols_processed": 0}
        return {
            "ok": True,
            "job": job_name,
            "detail": detail,
            "symbols_processed": processed,
        }
    finally:
        client.close()
