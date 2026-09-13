"""
GEX collection jobs — called by scheduler/runner.py on their respective triggers.

Data source: Zerodha Kite Connect only (this fork covers Indian markets;
the Schwab/CBOE/yfinance US-market pipeline was removed — see RetailGex for
the original multi-provider version).

Every job:
  1. Guards against non-trading days / outside trading window.
  2. Reads the active symbol list from symbols_config (DB-driven).
  3. Fetches the option chain from Zerodha.
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
from data_sources import zerodha_client
from data_sources.strike_filter import filter_chain

from scheduler.job_types import (
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
# Chain fetch
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
    """Fetch an option chain via Zerodha (the only supported provider in this fork).

    chain_dict: {"options": [...], "spot_price": float} — options are filtered
    to the asset-type strike band before return.
    Raises RuntimeError if the provider is unsupported or Zerodha returns
    nothing usable.
    """
    provider = str((symbol_doc or {}).get("provider") or "zerodha").lower()
    if provider != "zerodha":
        raise RuntimeError(
            f"Unsupported data provider '{provider}' for {symbol} — "
            "this fork only supports Zerodha."
        )

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
