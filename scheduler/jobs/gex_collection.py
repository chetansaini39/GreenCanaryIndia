"""
GEX collection jobs — called by scheduler/runner.py on their respective triggers.

Source priority per spec:
  Index symbols:  Schwab → CBOE → yfinance
  Stock symbols:  Schwab → yfinance  (CBOE does not cover stocks)

Every job:
  1. Guards against non-trading days / outside trading window.
  2. Reads the active symbol list from symbols_config (DB-driven).
  3. Fetches option chain with fallback.
  4. Computes GEX via gex_engine.
  5. Upserts result to the appropriate Mongo collection via db_writer.
  6. Logs outcome to pipeline_health (success / failed / skipped).
"""
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pymongo import MongoClient

import gex_engine
import db_writer
from app.models import symbols_config, pipeline_health
from app.models.db import market_db_for_symbol
from app.utils.time import market_midnight, now_ct, mongo_client_kwargs
from data_sources import schwab_client, cboe_client, yfinance_client, zerodha_client
from data_sources.strike_filter import filter_chain
from scheduler.market_utils import (
    is_trading_day,
    is_in_0dte_window,
    is_friday,
    next_n_trading_days,
    next_n_upcoming_fridays,
    next_m_upcoming_opex_cycles,
    trade_date_ct,
    week_of_monday,
)

from scheduler.job_types import (
    EOD_ROLLING_5D_WEEKLY,
    INTRADAY_0DTE_5MIN,
    MONTHLY_OPEX_3RD_FRIDAY,
    ZERODHA_NIFTY_EOD,
    ZERODHA_NIFTY_INTRADAY,
    ZERODHA_NIFTY_MONTHLY,
)

log = logging.getLogger(__name__)
CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))
_db_client = None


# ---------------------------------------------------------------------------
# Term structure helper
# ---------------------------------------------------------------------------

def _build_term_structure(
    options: list[dict],
    spot_price: float,
    trading_days: list,
    *,
    timezone_name: str | None = None,
    include_expiry_index: bool = False,
) -> list[dict]:
    """Slice the option chain by each of the next 3 trading days' expiry and
    compute GEX for each slice. Returns a list of 0–3 entries (fewer if no
    contracts exist for a given expiry, which can happen near holidays).
    """
    from datetime import date as _date, datetime as _datetime
    result = []
    for i, expiry_date in enumerate(trading_days, start=1):
        expiry_opts = [
            o for o in options
            if _normalise_expiry(o.get("expiry")) == expiry_date
        ]
        if not expiry_opts:
            continue
        ts_result = gex_engine.compute(expiry_opts, spot_price)
        timezone = ZoneInfo(timezone_name) if timezone_name else CT
        panel = {
            "expiry_date": _datetime(
                expiry_date.year, expiry_date.month, expiry_date.day, tzinfo=timezone
            ),
            "trading_days_out": i,
            "net_gex": ts_result["net_gex"],
            "call_wall": ts_result["call_wall"],
            "put_wall": ts_result["put_wall"],
            "gex_by_strike": ts_result["gex_by_strike"],
        }
        # Zerodha's actual-listed-expiry display needs an explicit ordinal;
        # retain the pre-integration US term-structure document shape.
        if include_expiry_index:
            panel["expiry_index"] = i
        result.append(panel)
    return result


def _normalise_expiry(value):
    """Mirror of gex_engine._normalise_expiry — avoid circular import."""
    from datetime import date as _date, datetime as _dt
    if value is None:
        return None
    if isinstance(value, _dt):
        return value.date()
    if isinstance(value, _date):
        return value
    if isinstance(value, str):
        try:
            return _date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _slice_options_by_expiry(options: list[dict], expiry_date) -> list[dict]:
    """Return only the contracts expiring exactly on expiry_date.

    Weekly/monthly GEX must be computed per expiry — computing over the whole
    multi-expiry chain and reusing that one result for every tracked expiry
    blends all weeks together, so every week shows identical GEX.
    """
    return [o for o in options if _normalise_expiry(o.get("expiry")) == expiry_date]

# ---------------------------------------------------------------------------
# Shared DB connection (scheduler is a single long-running process)
# ---------------------------------------------------------------------------

def _get_db():
    global _db_client
    if _db_client is None:
        _db_client = MongoClient(os.environ["MONGO_URI"], **mongo_client_kwargs())
    return _db_client.get_default_database()


# ---------------------------------------------------------------------------
# Source-priority fetch with fallback
# ---------------------------------------------------------------------------

def _fetch_chain(
    symbol: str,
    asset_type: str,
    symbol_doc: dict | None = None,
    *,
    expiry_date=None,
    expiry_dates=None,
    max_expiries: int | None = None,
) -> tuple[dict, str]:
    """Try data sources in priority order. Returns (chain_dict, source_name).

    chain_dict: {"options": [...], "spot_price": float} — options are filtered
    to the asset-type strike band before return.
    Raises RuntimeError only after all sources are exhausted.
    """
    provider = str((symbol_doc or {}).get("provider") or "schwab").lower()
    if provider == "zerodha":
        kwargs = {
            "expiry_date": expiry_date,
            "expiry_dates": expiry_dates,
            "asset_type": asset_type,
            "risk_free_rate": float((symbol_doc or {}).get("risk_free_rate", 0.055)),
        }
        if max_expiries is not None:
            kwargs["max_expiries"] = max_expiries
        result = filter_chain(zerodha_client.get_option_chain(symbol, **kwargs), asset_type)
        if not result["options"]:
            raise RuntimeError(f"Zerodha returned no usable options for {symbol}")
        return result, "zerodha"

    sources: list[tuple[str, callable]] = [
        ("schwab", lambda: schwab_client.get_option_chain(symbol, asset_type=asset_type)),
    ]

    if asset_type == "index":
        # CBOE only covers index symbols
        sources.append(("cboe", lambda: cboe_client.get_option_chain(symbol)))

    sources.append(("yfinance", lambda: yfinance_client.get_option_chain(symbol)))

    last_exc = None
    for source_name, fetch_fn in sources:
        try:
            result = fetch_fn()
            filtered = filter_chain(result, asset_type)
            if not filtered["options"]:
                raise ValueError(
                    f"No options within strike band for {symbol} "
                    f"(spot={filtered['spot_price']}, asset_type={asset_type})"
                )
            if source_name != "schwab":
                log.info("[%s] served by fallback source: %s", symbol, source_name)
            return filtered, source_name
        except Exception as exc:
            log.warning("[%s] %s failed: %s", symbol, source_name, exc)
            last_exc = exc

    raise RuntimeError(
        f"All data sources exhausted for {symbol}: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# Health logging
# ---------------------------------------------------------------------------

def _log_health(
    db,
    job_name: str,
    status: str,
    detail: str = "",
    symbols_processed: int = 0,
) -> None:
    try:
        pipeline_health.insert_one(db, {
            "job_name": job_name,
            "run_at": now_ct(),
            "status": status,
            "detail": detail,
            "symbols_processed": symbols_processed,
        })
    except Exception as exc:
        log.error("Failed to write pipeline_health log: %s", exc)


# ---------------------------------------------------------------------------
# Symbol list helpers
# ---------------------------------------------------------------------------

def _get_symbols(db, tier: str | None = None) -> list[dict]:
    """Return active symbols from symbols_config, optionally filtered by tier."""
    if tier:
        return symbols_config.find_by_tier(db, tier)
    return symbols_config.find_all_active(db)


def _us_symbols(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if str(row.get("provider") or "schwab").lower() != "zerodha"
    ]


def _market_metadata(chain: dict, symbol_doc: dict, expiry=None) -> dict:
    futures_price = chain.get("futures_price")
    if expiry is not None:
        futures_price = (chain.get("futures_by_expiry") or {}).get(
            expiry.isoformat(), futures_price
        )
    data_quality = chain.get("data_quality")
    if expiry is not None and data_quality:
        data_quality = (data_quality.get("by_expiry") or {}).get(
            expiry.isoformat(), data_quality
        )
    return {
        "provider": "zerodha",
        "market": symbol_doc.get("market", "nse"),
        "market_timezone": symbol_doc.get("market_timezone", "Asia/Kolkata"),
        "currency": symbol_doc.get("currency", "INR"),
        "display_unit": symbol_doc.get("display_unit", "crore"),
        "pricing_model": symbol_doc.get("pricing_model", "black76"),
        "futures_price": futures_price,
        "risk_free_rate": float(symbol_doc.get("risk_free_rate", 0.055)),
        "data_quality": data_quality,
    }


def _zerodha_quality_detail(chain: dict) -> str:
    quality = chain.get("data_quality") or {}
    if quality.get("status") != "partial":
        return ""
    return (
        "Partial IV coverage: excluded "
        f"{quality.get('excluded_strikes', 0)} strike pair(s) / "
        f"{quality.get('excluded_contracts', 0)} contract(s) whose gamma was "
        "unavailable after IV quality validation; no unknown gamma was zero-filled."
    )


def _nifty_symbol(db) -> dict:
    symbol_doc = symbols_config.find_by_symbol(db, "NIFTY")
    if not symbol_doc or not symbol_doc.get("active", True):
        raise RuntimeError("Active NIFTY symbol configuration is missing")
    if str(symbol_doc.get("provider", "")).lower() != "zerodha":
        raise RuntimeError("NIFTY is not configured for the Zerodha provider")
    return symbol_doc


# ---------------------------------------------------------------------------
# Job: 0DTE intraday (every 5 min, 8:45 AM–2:55 PM CT)
# ---------------------------------------------------------------------------

def run_0dte_intraday() -> None:
    """Collect 5-min 0DTE GEX snapshots for index symbols (daily) and stock
    symbols (Fridays only, when weekly expiry = 0DTE).
    """
    job_name = INTRADAY_0DTE_5MIN

    if not is_in_0dte_window():
        # APScheduler fires on a fixed interval; guard handles off-hours ticks.
        return

    db = _get_db()
    now = now_ct()
    trade_date = trade_date_ct(now.date())

    all_symbols = _us_symbols(_get_symbols(db))
    target_symbols = [
        s for s in all_symbols
        if s["asset_type"] == "index"
        or (is_friday(now.date()) and s["asset_type"] == "stock")
    ]

    if not target_symbols:
        log.warning("[%s] No active symbols found in symbols_config.", job_name)
        return

    processed = 0
    errors: list[str] = []

    next_3_trading_days = next_n_trading_days(3, from_date=now.date())

    for sym_doc in target_symbols:
        symbol = sym_doc["symbol"]
        asset_type = sym_doc["asset_type"]
        try:
            chain, source = _fetch_chain(symbol, asset_type)
            result = gex_engine.compute(chain["options"], chain["spot_price"])

            term_structure = None
            if asset_type == "index":
                term_structure = _build_term_structure(
                    chain["options"], chain["spot_price"], next_3_trading_days
                )

            db_writer.write_intraday(
                db,
                symbol=symbol,
                asset_type=asset_type,
                timestamp=now,
                trade_date=trade_date,
                snapshot_type="0dte",
                source=source,
                spot_price=chain["spot_price"],
                gex_result=result,
                term_structure=term_structure,
            )
            processed += 1
        except Exception as exc:
            msg = f"{symbol}: {exc}"
            log.error("[%s] %s", job_name, msg)
            errors.append(msg)

    status = "success" if not errors else ("failed" if processed == 0 else "success")
    detail = "; ".join(errors) if errors else ""
    _log_health(db, job_name, status, detail, processed)


# ---------------------------------------------------------------------------
# Job: EOD (2:50 PM CT daily)
# ---------------------------------------------------------------------------

def run_eod() -> None:
    """End-of-day GEX collection:
      - Save rolling 21-day EOD snapshot for all symbols (every trading day).
      - Save weekly GEX for all symbols for each of the next N upcoming Friday
        expiries in parallel (N = platform_settings.gex_weekly_forward_weeks).
    """
    job_name = EOD_ROLLING_5D_WEEKLY

    db = _get_db()
    now = now_ct()

    if not is_trading_day(now.date()):
        _log_health(db, job_name, "skipped", "holiday or non-trading day", 0)
        return

    trade_date = trade_date_ct(now.date())
    all_symbols = _us_symbols(_get_symbols(db))
    if not all_symbols:
        log.warning("[%s] No active symbols found in symbols_config.", job_name)
        _log_health(db, job_name, "skipped", "no active symbols in symbols_config", 0)
        return

    from app.models import platform_settings as ps
    n_weeks = ps.get_weekly_forward_weeks(db)
    upcoming_fridays = next_n_upcoming_fridays(n_weeks, from_date=now.date())

    processed = 0
    errors: list[str] = []

    for sym_doc in all_symbols:
        symbol = sym_doc["symbol"]
        asset_type = sym_doc["asset_type"]
        try:
            chain, source = _fetch_chain(symbol, asset_type)
            result = gex_engine.compute(chain["options"], chain["spot_price"])

            # Rolling 21-day EOD — every symbol, every trading day
            db_writer.write_rolling_21d(
                db,
                symbol=symbol,
                asset_type=asset_type,
                trade_date=trade_date,
                source=source,
                spot_price=chain["spot_price"],
                gex_result=result,
            )

            # Weekly snapshot — one document per tracked expiry (N in parallel).
            # GEX is computed PER expiry: slice the chain to each Friday's own
            # contracts and compute on that slice. Computing once over the whole
            # blended chain and reusing it makes every week identical.
            for friday in upcoming_fridays:
                expiry_opts = _slice_options_by_expiry(chain["options"], friday)
                week_result = gex_engine.compute(expiry_opts, chain["spot_price"])
                expiry_dt = trade_date_ct(friday)
                monday = trade_date_ct(week_of_monday(friday))
                db_writer.write_weekly(
                    db,
                    symbol=symbol,
                    asset_type=asset_type,
                    week_of=monday,
                    expiry_date=expiry_dt,
                    trade_date=trade_date,
                    source=source,
                    spot_price=chain["spot_price"],
                    gex_result=week_result,
                )

            processed += 1
        except Exception as exc:
            msg = f"{symbol}: {exc}"
            log.error("[%s] %s", job_name, msg)
            errors.append(msg)

    status = "success" if not errors else ("failed" if processed == 0 else "success")
    _log_health(db, job_name, status, "; ".join(errors), processed)


# ---------------------------------------------------------------------------
# Job: Monthly OPEX — Monday + Friday EOD (~3:05 PM CT)
# ---------------------------------------------------------------------------

def run_monthly_opex_check() -> None:
    """Save monthly OPEX snapshots twice per week (Monday and Friday EOD).

    Writes one document per symbol per tracked OPEX cycle — M cycles in parallel
    (M = platform_settings.gex_monthly_forward_cycles, default 3).  The window
    is computed fresh each run as "the next M upcoming 3rd-Friday dates from today,"
    so an expiry that passes simply falls out of the window on its own — no special
    rollover branch needed.  Applies to all active symbols.
    """
    job_name = MONTHLY_OPEX_3RD_FRIDAY

    db = _get_db()
    now = now_ct()

    if not is_trading_day(now.date()):
        _log_health(db, job_name, "skipped", "holiday or non-trading day", 0)
        return

    trade_date = trade_date_ct(now.date())

    from app.models import platform_settings as ps
    m_cycles = ps.get_monthly_forward_cycles(db)
    upcoming_cycles = next_m_upcoming_opex_cycles(m_cycles, from_date=now.date())

    all_symbols = _us_symbols(_get_symbols(db))

    processed = 0
    errors: list[str] = []

    for sym_doc in all_symbols:
        symbol = sym_doc["symbol"]
        asset_type = sym_doc["asset_type"]
        try:
            chain, source = _fetch_chain(symbol, asset_type)
            # Same per-expiry rule as weekly: slice the chain to each OPEX
            # expiry and compute on that slice, not the whole blended chain.
            for expiry in upcoming_cycles:
                expiry_opts = _slice_options_by_expiry(chain["options"], expiry)
                cycle_result = gex_engine.compute(expiry_opts, chain["spot_price"])
                expiry_dt = trade_date_ct(expiry)
                db_writer.write_monthly_opex(
                    db,
                    symbol=symbol,
                    asset_type=asset_type,
                    expiry_date=expiry_dt,
                    trade_date=trade_date,
                    source=source,
                    spot_price=chain["spot_price"],
                    gex_result=cycle_result,
                )
            processed += 1
        except Exception as exc:
            msg = f"{symbol}: {exc}"
            log.error("[%s] %s", job_name, msg)
            errors.append(msg)

    status = "success" if not errors else ("failed" if processed == 0 else "success")
    _log_health(db, job_name, status, "; ".join(errors), processed)


# ---------------------------------------------------------------------------
# Zerodha/NIFTY jobs — separate IST schedule and isolated market database
# ---------------------------------------------------------------------------

def _zerodha_job_enabled(db, field: str, manual: bool) -> tuple[bool, dict]:
    from app.models import platform_settings

    settings = platform_settings.get_zerodha_settings(db)
    enabled = manual or (
        settings["zerodha_automation_enabled"] and settings[field]
    )
    return enabled, settings


def run_zerodha_intraday(*, manual: bool = False) -> None:
    primary_db = _get_db()
    enabled, settings = _zerodha_job_enabled(
        primary_db, "zerodha_intraday_enabled", manual
    )
    if not enabled:
        return

    from scheduler.india_market_utils import in_time_window, is_nse_trading_day, now_ist

    now = now_ist()
    if not manual and not in_time_window(
        now, settings["zerodha_intraday_start"], settings["zerodha_intraday_end"]
    ):
        return
    if (now.hour, now.minute) >= (15, 30):
        _log_health(
            primary_db, ZERODHA_NIFTY_INTRADAY, "skipped",
            "NIFTY 0DTE contracts expired at 15:30 IST", 0,
        )
        return
    if not is_nse_trading_day(now.date()):
        _log_health(primary_db, ZERODHA_NIFTY_INTRADAY, "skipped", "NSE holiday", 0)
        return

    try:
        expiries = zerodha_client.available_option_expiries(from_date=now.date())
        if now.date() not in expiries:
            _log_health(
                primary_db, ZERODHA_NIFTY_INTRADAY, "skipped",
                "NIFTY has no actual same-day expiry", 0,
            )
            return
        symbol_doc = _nifty_symbol(primary_db)
        chain, source = _fetch_chain(
            "NIFTY", "index", symbol_doc,
            max_expiries=max(3, settings["zerodha_weekly_forward_expiries"]),
        )
        today_options = _slice_options_by_expiry(chain["options"], now.date())
        if not today_options:
            raise RuntimeError("Actual NIFTY 0DTE slice is empty")
        result = gex_engine.compute(today_options, chain["spot_price"])
        term_dates = [expiry for expiry in expiries if expiry >= now.date()][:3]
        term_structure = _build_term_structure(
            chain["options"], chain["spot_price"], term_dates,
            timezone_name="Asia/Kolkata",
            include_expiry_index=True,
        )
        data_db = market_db_for_symbol(primary_db, symbol_doc)
        db_writer.write_intraday(
            data_db,
            symbol="NIFTY",
            asset_type="index",
            timestamp=now,
            trade_date=market_midnight(now.date(), "Asia/Kolkata"),
            snapshot_type="0dte",
            source=source,
            spot_price=chain["spot_price"],
            gex_result=result,
            term_structure=term_structure,
            market_metadata=_market_metadata(chain, symbol_doc, now.date()),
        )
        _log_health(
            primary_db, ZERODHA_NIFTY_INTRADAY, "success",
            _zerodha_quality_detail(chain), 1,
        )
    except Exception as exc:
        log.exception("[%s] NIFTY failed", ZERODHA_NIFTY_INTRADAY)
        _log_health(primary_db, ZERODHA_NIFTY_INTRADAY, "failed", str(exc), 0)
        if manual:
            raise


def run_zerodha_eod(*, manual: bool = False) -> None:
    primary_db = _get_db()
    enabled, settings = _zerodha_job_enabled(primary_db, "zerodha_eod_enabled", manual)
    if not enabled:
        return

    from scheduler.india_market_utils import is_nse_trading_day, now_ist

    now = now_ist()
    if not is_nse_trading_day(now.date()):
        _log_health(primary_db, ZERODHA_NIFTY_EOD, "skipped", "NSE holiday", 0)
        return
    try:
        symbol_doc = _nifty_symbol(primary_db)
        n_expiries = settings["zerodha_weekly_forward_expiries"]
        chain, source = _fetch_chain(
            "NIFTY", "index", symbol_doc, max_expiries=n_expiries
        )
        data_db = market_db_for_symbol(primary_db, symbol_doc)
        trade_date = market_midnight(now.date(), "Asia/Kolkata")
        metadata = _market_metadata(chain, symbol_doc)
        result = gex_engine.compute(chain["options"], chain["spot_price"])
        db_writer.write_rolling_21d(
            data_db,
            symbol="NIFTY",
            asset_type="index",
            trade_date=trade_date,
            source=source,
            spot_price=chain["spot_price"],
            gex_result=result,
            market_metadata=metadata,
        )

        expiries = [
            date.fromisoformat(value)
            for value in (chain.get("futures_by_expiry") or {})
        ][:n_expiries]
        for expiry in expiries:
            expiry_options = _slice_options_by_expiry(chain["options"], expiry)
            if not expiry_options:
                raise RuntimeError(f"NIFTY expiry slice is empty for {expiry}")
            expiry_result = gex_engine.compute(expiry_options, chain["spot_price"])
            monday = expiry - timedelta(days=expiry.weekday())
            db_writer.write_weekly(
                data_db,
                symbol="NIFTY",
                asset_type="index",
                week_of=market_midnight(monday, "Asia/Kolkata"),
                expiry_date=market_midnight(expiry, "Asia/Kolkata"),
                trade_date=trade_date,
                source=source,
                spot_price=chain["spot_price"],
                gex_result=expiry_result,
                market_metadata=_market_metadata(chain, symbol_doc, expiry),
            )
        _log_health(
            primary_db, ZERODHA_NIFTY_EOD, "success",
            _zerodha_quality_detail(chain), 1,
        )
    except Exception as exc:
        log.exception("[%s] NIFTY failed", ZERODHA_NIFTY_EOD)
        _log_health(primary_db, ZERODHA_NIFTY_EOD, "failed", str(exc), 0)
        if manual:
            raise


def run_zerodha_monthly(*, manual: bool = False) -> None:
    primary_db = _get_db()
    enabled, settings = _zerodha_job_enabled(
        primary_db, "zerodha_monthly_enabled", manual
    )
    if not enabled:
        return

    from scheduler.india_market_utils import is_nse_trading_day, now_ist

    now = now_ist()
    if not is_nse_trading_day(now.date()):
        _log_health(primary_db, ZERODHA_NIFTY_MONTHLY, "skipped", "NSE holiday", 0)
        return
    try:
        symbol_doc = _nifty_symbol(primary_db)
        cycles = zerodha_client.monthly_option_expiries(from_date=now.date())[
            :settings["zerodha_monthly_forward_cycles"]
        ]
        if not cycles:
            raise RuntimeError("No upcoming NIFTY monthly expiries are listed")
        chain, source = _fetch_chain(
            "NIFTY", "index", symbol_doc, expiry_dates=cycles
        )
        data_db = market_db_for_symbol(primary_db, symbol_doc)
        trade_date = market_midnight(now.date(), "Asia/Kolkata")
        for expiry in cycles:
            expiry_options = _slice_options_by_expiry(chain["options"], expiry)
            if not expiry_options:
                raise RuntimeError(f"NIFTY monthly slice is empty for {expiry}")
            result = gex_engine.compute(expiry_options, chain["spot_price"])
            db_writer.write_monthly_opex(
                data_db,
                symbol="NIFTY",
                asset_type="index",
                expiry_date=market_midnight(expiry, "Asia/Kolkata"),
                trade_date=trade_date,
                source=source,
                spot_price=chain["spot_price"],
                gex_result=result,
                market_metadata=_market_metadata(chain, symbol_doc, expiry),
            )
        _log_health(
            primary_db, ZERODHA_NIFTY_MONTHLY, "success",
            _zerodha_quality_detail(chain), 1,
        )
    except Exception as exc:
        log.exception("[%s] NIFTY failed", ZERODHA_NIFTY_MONTHLY)
        _log_health(primary_db, ZERODHA_NIFTY_MONTHLY, "failed", str(exc), 0)
        if manual:
            raise
