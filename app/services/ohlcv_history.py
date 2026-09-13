"""Fetch recent daily OHLCV bars for dashboard display (via yfinance)."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

log = logging.getLogger(__name__)

_YF_SYMBOLS = {
    "SPX": "^GSPC",
    "NDX": "^NDX",
    "RUT": "^RUT",
    "VIX": "^VIX",
}

_FETCH_TIMEOUT_SEC = 5.0


def _yf_symbol(symbol: str) -> str:
    return _YF_SYMBOLS.get(symbol.upper(), symbol)


def _fetch_inner(symbol: str, days: int) -> list[dict]:
    yf_sym = _yf_symbol(symbol)
    df = yf.Ticker(yf_sym).history(period="3mo", interval="1d")
    if df is None or df.empty:
        return []

    bars: list[dict] = []
    for ts, row in df.iterrows():
        bars.append({
            "date": ts.date().isoformat(),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]),
        })

    return bars[-days:]


def fetch_ohlcv_history(
    symbol: str, days: int = 12, *, provider: str = "schwab"
) -> list[dict]:
    """Return up to `days` most recent daily OHLCV bars, oldest first."""
    days = max(1, min(days, 45))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            if provider == "zerodha":
                from data_sources.zerodha_client import get_historical_bars

                end = datetime.now(ZoneInfo("Asia/Kolkata"))
                start = end - timedelta(days=max(90, days * 3))
                fut = pool.submit(
                    get_historical_bars, symbol, start, end, interval="day"
                )
            else:
                fut = pool.submit(_fetch_inner, symbol, days)
            return fut.result(timeout=_FETCH_TIMEOUT_SEC)[-days:]
    except FuturesTimeout:
        log.warning("OHLCV fetch timed out for %s", symbol)
    except Exception as exc:
        log.warning("OHLCV fetch failed for %s: %s", symbol, exc)
    return []
