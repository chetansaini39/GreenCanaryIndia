"""Fetch recent daily OHLCV bars for dashboard display (via Zerodha)."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_FETCH_TIMEOUT_SEC = 5.0


def fetch_ohlcv_history(
    symbol: str, days: int = 12, *, provider: str = "zerodha"
) -> list[dict]:
    """Return up to `days` most recent daily OHLCV bars, oldest first."""
    days = max(1, min(days, 45))
    if provider != "zerodha":
        log.warning("OHLCV fetch skipped for %s: unsupported provider '%s'", symbol, provider)
        return []
    try:
        from data_sources.zerodha_client import get_historical_bars

        with ThreadPoolExecutor(max_workers=1) as pool:
            end = datetime.now(ZoneInfo("Asia/Kolkata"))
            start = end - timedelta(days=max(90, days * 3))
            fut = pool.submit(
                get_historical_bars, symbol, start, end, interval="day"
            )
            return fut.result(timeout=_FETCH_TIMEOUT_SEC)[-days:]
    except FuturesTimeout:
        log.warning("OHLCV fetch timed out for %s", symbol)
    except Exception as exc:
        log.warning("OHLCV fetch failed for %s: %s", symbol, exc)
    return []
