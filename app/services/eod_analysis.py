"""Build EOD GEX Analysis payloads for the dashboard (mirrors DailyIndexRangeFinder EOD card)."""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

from app.models import gex_intraday, gex_rolling_21d, gex_weekly
from app.charts.contracts import expiry_detail_for_snapshot
from app.services import eod_charts

log = logging.getLogger(__name__)

INDEX_SYMBOLS = {"SPY", "QQQ", "SPX", "NDX", "RUT", "VIX", "IWM"}

_YF_SYMBOLS = {
    "SPX": "^GSPC",
    "NDX": "^NDX",
    "RUT": "^RUT",
    "VIX": "^VIX",
}


_CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))


def _trade_date_midnight(d: date, timezone_name: str = str(_CT)) -> datetime:
    # CT midnight — must match how storage writes trade_date via
    # trade_date_ct(week_of_monday(...)) in scheduler/jobs/gex_collection.py.
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(timezone_name))


def _as_date(value, timezone_name: str = str(_CT)) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(timezone_name)).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _date_label(value, timezone_name: str = str(_CT)) -> str:
    d = _as_date(value, timezone_name)
    return d.isoformat() if d else str(value)


def _week_monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _asset_type(symbol: str) -> str:
    return "index" if symbol.upper() in INDEX_SYMBOLS else "stock"


def _yf_symbol(symbol: str) -> str:
    return _YF_SYMBOLS.get(symbol.upper(), symbol)


def _fetch_stock_bar_inner(symbol: str, trade_date: date) -> dict | None:
    yf_sym = _yf_symbol(symbol)
    df = yf.Ticker(yf_sym).history(period="1mo", interval="1d")
    if df is None or df.empty:
        return None

    target = trade_date.isoformat()
    for ts, row in df.iterrows():
        if ts.date().isoformat() == target:
            return {
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]),
            }
    return None


def _fetch_stock_bar(symbol: str, trade_date: date, provider: str = "schwab") -> dict | None:
    """OHLCV for a single trading day via yfinance (2s timeout, non-blocking)."""
    if provider == "zerodha":
        try:
            from data_sources.zerodha_client import get_historical_bars

            start = datetime.combine(trade_date, datetime.min.time(), tzinfo=ZoneInfo("Asia/Kolkata"))
            rows = get_historical_bars(symbol, start, start + timedelta(days=1), interval="day")
            return rows[-1] if rows else None
        except Exception as exc:
            log.debug("Zerodha OHLCV skipped for %s: %s", symbol, exc)
            return None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_fetch_stock_bar_inner, symbol, trade_date)
            return fut.result(timeout=2.0)
    except FuturesTimeout:
        log.debug("yfinance OHLCV timed out for %s", symbol)
    except Exception as exc:
        log.debug("yfinance OHLCV skipped for %s: %s", symbol, exc)
    return None


def _snapshot_has_charts(doc: dict | None) -> bool:
    return bool(doc and doc.get("gex_by_strike"))


def _find_weekly_for_date(
    db, symbol: str, ref_date: date, timezone_name: str = str(_CT)
) -> dict | None:
    """Weekly snapshot for the week containing ref_date, or matching expiry."""
    monday = _week_monday_of(ref_date)
    weekly = gex_weekly.find_latest_for_week(
        db, symbol, _trade_date_midnight(monday, timezone_name)
    )
    if weekly:
        return weekly

    midnight = _trade_date_midnight(ref_date, timezone_name)
    return db[gex_weekly.COLLECTION].find_one({"symbol": symbol, "expiry_date": midnight})


def _resolve_eod_snapshot(
    db, symbol: str, trade_date: date, timezone_name: str = str(_CT)
) -> dict | None:
    """Best available GEX snapshot for an EOD trading day."""
    midnight = _trade_date_midnight(trade_date, timezone_name)

    rolling = gex_rolling_21d.find_by_symbol_date(db, symbol, midnight)
    if _snapshot_has_charts(rolling):
        return rolling

    intraday_docs = gex_intraday.find_by_symbol_date(db, symbol, midnight)
    for doc in reversed(intraday_docs):
        if _snapshot_has_charts(doc):
            return doc

    weekly = _find_weekly_for_date(db, symbol, trade_date, timezone_name)
    if _snapshot_has_charts(weekly):
        return weekly

    # Rolling doc may exist without strike detail — enrich from weekly for same expiry/week
    if rolling and weekly and _snapshot_has_charts(weekly):
        return weekly

    return rolling or (intraday_docs[-1] if intraday_docs else None) or weekly


def _day_label(symbol: str, trade_date: date) -> str:
    return f"{symbol}_{trade_date.isoformat()}"


def _build_day_payload(
    db,
    symbol: str,
    trade_date: date,
    snap: dict | None = None,
    market_timezone: str = str(_CT),
    provider: str = "schwab",
) -> dict | None:
    snap = snap or _resolve_eod_snapshot(db, symbol, trade_date, market_timezone)
    if not snap:
        return None

    spot = float(snap.get("spot_price") or 0)
    gex_by_strike = snap.get("gex_by_strike") or []
    gex_by_expiration, gex_by_expiry_strike = expiry_detail_for_snapshot(snap)

    currency = snap.get("currency", "USD")
    display_unit = snap.get("display_unit", "billion")
    charts = {
        "gex_by_strike": eod_charts.gex_by_strike_chart(
            symbol, spot, gex_by_strike,
            currency=currency, display_unit=display_unit,
        ),
        "gex_by_expiration": eod_charts.gex_by_expiration_chart(
            symbol, gex_by_expiration,
            currency=currency, display_unit=display_unit,
        ),
        "gex_surface_3d": eod_charts.gex_surface_3d_chart(
            spot, gex_by_expiry_strike,
            currency=currency, display_unit=display_unit,
        ),
    }

    net_gex = float(snap.get("net_gex") or 0)
    display_date = _as_date(snap.get("expiry_date"), market_timezone) or trade_date

    # snap_type tells the frontend which type= param to use when calling
    # /api/gex/parallel-chart. Docs from gex_rolling_21d don't have this field
    # so we fall back to "weekly", which _fetch_snapshot handles correctly via
    # its latest-for-week fallback query.
    snap_type = snap.get("snapshot_type") or snap.get("type") or "weekly"

    # Parallel coords has data if options_slice (preferred) or gex_by_strike is present.
    has_parallel_coords = bool(snap.get("options_slice") or gex_by_strike)

    return {
        "trade_date": display_date.isoformat(),
        "label": _day_label(symbol, display_date),
        "symbol": symbol,
        "snap_type": snap_type,
        "spot_price": spot,
        "net_gex": net_gex,
        "total_notional_gex_bn": round(net_gex / 1e9, 4),
        "call_wall": snap.get("call_wall"),
        "put_wall": snap.get("put_wall"),
        "source": snap.get("source"),
        "created_at": snap.get("created_at"),
        "stock": _fetch_stock_bar(symbol, display_date, provider),
        "market_timezone": market_timezone,
        "currency": currency,
        "display_unit": display_unit,
        "total_notional_gex_display": round(
            net_gex / (1e7 if snap.get("display_unit") == "crore" else 1e9), 4
        ),
        "charts": charts,
        "has_detail_charts": bool(gex_by_strike),
        "has_parallel_coords": has_parallel_coords,
    }


def _build_stock_week_payload(
    db, symbol: str, week_ref_date: date,
    market_timezone: str = str(_CT), provider: str = "schwab",
) -> dict | None:
    """Stocks use weekly snapshots keyed by week_of Monday (same as /api/gex?type=weekly)."""
    weekly = _find_weekly_for_date(db, symbol, week_ref_date, market_timezone)
    if not weekly or not _snapshot_has_charts(weekly):
        return None

    week_monday = _week_monday_of(week_ref_date)
    display_date = _as_date(weekly.get("expiry_date"), market_timezone) or week_monday
    return _build_day_payload(
        db, symbol, display_date, snap=weekly,
        market_timezone=market_timezone, provider=provider,
    )


def build_eod_analysis(
    db,
    symbol: str,
    end_date: date,
    num_days: int = 1,
    asset_type: str | None = None,
    market_timezone: str = str(_CT),
    provider: str = "schwab",
) -> dict:
    """Return EOD GEX Analysis card data for up to num_days ending at end_date."""
    asset_type = asset_type or _asset_type(symbol)

    if asset_type == "stock":
        payload = _build_stock_week_payload(
            db, symbol, end_date, market_timezone, provider
        )
        days = [payload] if payload else []
        message = None
        if not days:
            week_mon = _week_monday_of(end_date)
            message = (
                f"No EOD GEX data for week of {week_mon.isoformat()} yet. "
                "Weekly snapshots are saved on the last trading day of each week."
            )
        return {
            "symbol": symbol,
            "end_date": end_date.isoformat(),
            "num_days": len(days),
            "days": days,
            "gex_over_time": None,
            "last_updated": days[0].get("created_at") if days else None,
            "message": message,
        }

    midnight = _trade_date_midnight(end_date, market_timezone)
    snapshots = gex_rolling_21d.find_last_n_before(db, symbol, midnight, n=num_days)

    if not snapshots:
        single = _build_day_payload(
            db, symbol, end_date, market_timezone=market_timezone, provider=provider
        )
        days = [single] if single else []
    else:
        days = []
        seen: set[str] = set()
        for snap in snapshots:
            td = _date_label(snap.get("trade_date", end_date), market_timezone)
            if td in seen:
                continue
            seen.add(td)
            day_date = date.fromisoformat(td)
            payload = _build_day_payload(
                db, symbol, day_date,
                market_timezone=market_timezone, provider=provider,
            )
            if payload and payload.get("has_detail_charts"):
                days.append(payload)

        if not days:
            single = _build_day_payload(
                db, symbol, end_date,
                market_timezone=market_timezone, provider=provider,
            )
            if single and single.get("has_detail_charts"):
                days = [single]

    gex_over_time = eod_charts.gex_over_time_chart(days) if len(days) > 1 else None
    message = None
    if not days:
        message = f"No EOD GEX data available for {symbol} on or before {end_date.isoformat()}."

    last_updated = days[0].get("created_at") if days else None

    return {
        "symbol": symbol,
        "end_date": end_date.isoformat(),
        "num_days": len(days),
        "days": days,
        "gex_over_time": gex_over_time,
        "last_updated": last_updated,
        "message": message,
    }
