"""Single-process Zerodha WebSocket cache used by the scheduler."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone


log = logging.getLogger(__name__)
_lock = threading.RLock()
_ticker = None
_tokens: set[int] = set()
_quotes: dict[int, dict] = {}
_connected = False
_auth_failed = False
_last_error: str | None = None


def _tick_time(tick: dict) -> datetime:
    value = tick.get("exchange_timestamp") or tick.get("timestamp")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def cached_quote(instrument_token: int, *, max_age_seconds: int = 15) -> dict | None:
    with _lock:
        quote = _quotes.get(int(instrument_token))
        if not quote:
            return None
        age = (datetime.now(timezone.utc) - quote["_received_at"]).total_seconds()
        return dict(quote) if age <= max_age_seconds else None


def start(api_key: str, access_token: str, instrument_tokens: list[int]) -> None:
    """Start once, then update the full-mode subscription set."""
    global _ticker, _tokens, _connected, _auth_failed, _last_error
    wanted = {int(token) for token in instrument_tokens}
    with _lock:
        previous = set(_tokens)
        _tokens = wanted
        if _ticker is not None:
            if _connected:
                removed = sorted(previous - wanted)
                added = sorted(wanted - previous)
                if removed:
                    _ticker.unsubscribe(removed)
                if added:
                    _ticker.subscribe(added)
                if wanted:
                    _ticker.set_mode(_ticker.MODE_FULL, sorted(wanted))
            return

        from kiteconnect import KiteTicker

        ticker = KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=10)

        def on_connect(ws, response):
            global _connected, _auth_failed
            with _lock:
                _connected = True
                _auth_failed = False
                current = sorted(_tokens)
            if current:
                ws.subscribe(current)
                ws.set_mode(ws.MODE_FULL, current)

        def on_ticks(ws, ticks):
            received_at = datetime.now(timezone.utc)
            with _lock:
                for tick in ticks or []:
                    token = tick.get("instrument_token")
                    if token is None:
                        continue
                    value = dict(tick)
                    value["timestamp"] = _tick_time(tick)
                    value["_received_at"] = received_at
                    _quotes[int(token)] = value

        def on_close(ws, code, reason):
            global _connected, _last_error
            with _lock:
                _connected = False
                _last_error = f"closed ({code}): {reason}"

        def on_error(ws, code, reason):
            global _connected, _auth_failed, _last_error
            with _lock:
                _connected = False
                _auth_failed = str(code) in {"403", "1008"}
                _last_error = f"error ({code}): {reason}"

        ticker.on_connect = on_connect
        ticker.on_ticks = on_ticks
        ticker.on_close = on_close
        ticker.on_error = on_error
        _ticker = ticker
        _last_error = None
        ticker.connect(threaded=True)


def stop() -> None:
    global _ticker, _tokens, _connected
    with _lock:
        ticker = _ticker
        _ticker = None
        _tokens = set()
        _connected = False
    if ticker is not None:
        try:
            ticker.close()
        except Exception:
            log.exception("Failed to close Zerodha WebSocket")


def status() -> dict:
    with _lock:
        newest = max(
            (quote["_received_at"] for quote in _quotes.values()),
            default=None,
        )
        return {
            "started": _ticker is not None,
            "connected": _connected,
            "auth_failed": _auth_failed,
            "subscribed_tokens": len(_tokens),
            "cached_quotes": len(_quotes),
            "last_tick_at": newest,
            "last_error": _last_error,
        }
