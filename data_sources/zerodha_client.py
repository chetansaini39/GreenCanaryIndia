"""Zerodha Kite Connect adapter for NIFTY market data and margins."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from data_sources.black76 import DEFAULT_RISK_FREE_RATE, process_contracts


log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
SPOT_KEY = "NSE:NIFTY 50"
UNDERLYING_NAME = "NIFTY"
QUOTE_BATCH_SIZE = 500

_ROOT = Path(__file__).resolve().parents[1]
_instrument_lock = threading.Lock()
_quote_lock = threading.Lock()
_memory_instruments: tuple[date, list[dict]] | None = None
_last_quote_at = 0.0
_client = None


def _kite_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ZERODHA_API_KEY")
        access_token = os.environ.get("ZERODHA_ACCESS_TOKEN")
        if not api_key or not access_token:
            raise RuntimeError("ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are required")
        from kiteconnect import KiteConnect

        client = KiteConnect(api_key=api_key)
        client.set_access_token(access_token)
        _client = client
    return _client


def reset_caches() -> None:
    """Clear process caches; primarily used after rotating credentials in tests."""
    global _client, _memory_instruments, _last_quote_at
    _client = None
    _memory_instruments = None
    _last_quote_at = 0.0


def _cache_dir(*, create: bool = True) -> Path:
    path = Path(os.environ.get("ZERODHA_CACHE_DIR", _ROOT / "instance" / "zerodha"))
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(trade_date: date) -> Path:
    return _cache_dir() / f"instruments-{trade_date.isoformat()}.json"


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported instrument value: {type(value).__name__}")


def _normalise_instruments(rows: list[dict]) -> list[dict]:
    normalized = []
    for raw in rows:
        row = dict(raw)
        expiry = row.get("expiry")
        if isinstance(expiry, str) and expiry:
            row["expiry"] = date.fromisoformat(expiry[:10])
        normalized.append(row)
    return normalized


def get_instruments(*, force: bool = False) -> list[dict]:
    """Return the current instrument master using one on-disk file per IST day."""
    global _memory_instruments
    today = datetime.now(IST).date()
    with _instrument_lock:
        if not force and _memory_instruments and _memory_instruments[0] == today:
            return list(_memory_instruments[1])

        cache_file = _cache_path(today)
        if not force and cache_file.exists():
            rows = _normalise_instruments(json.loads(cache_file.read_text()))
            _memory_instruments = (today, rows)
            return list(rows)

        rows = _normalise_instruments(_kite_client().instruments())
        if not rows:
            raise RuntimeError("Zerodha returned an empty instrument master")
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, default=_json_default, separators=(",", ":")))
        tmp.replace(cache_file)
        _memory_instruments = (today, rows)
        return list(rows)


def _nifty_derivatives(instruments: list[dict] | None = None) -> list[dict]:
    rows = instruments if instruments is not None else get_instruments()
    return [
        row for row in rows
        if str(row.get("exchange", "")).upper() == "NFO"
        and str(row.get("name", "")).upper() == UNDERLYING_NAME
        and str(row.get("instrument_type", "")).upper() in {"CE", "PE", "FUT"}
    ]


def available_option_expiries(*, from_date: date | None = None) -> list[date]:
    start = from_date or datetime.now(IST).date()
    return sorted({
        row["expiry"] for row in _nifty_derivatives()
        if str(row.get("instrument_type", "")).upper() in {"CE", "PE"}
        and isinstance(row.get("expiry"), date)
        and row["expiry"] >= start
    })


def monthly_option_expiries(*, from_date: date | None = None) -> list[date]:
    monthly: dict[tuple[int, int], date] = {}
    for expiry in available_option_expiries(from_date=from_date):
        key = (expiry.year, expiry.month)
        monthly[key] = max(monthly.get(key, expiry), expiry)
    return sorted(monthly.values())


def subscription_tokens(max_expiries: int = 12) -> list[int]:
    """Tokens needed for the scheduler's focused NIFTY full-mode stream."""
    instruments = get_instruments()
    spot = next(
        (row for row in instruments
         if str(row.get("exchange", "")).upper() == "NSE"
         and str(row.get("tradingsymbol", "")).upper() == "NIFTY 50"),
        None,
    )
    if not spot:
        raise RuntimeError("NIFTY 50 spot instrument is absent from the instrument master")
    spot_quote = get_quotes([SPOT_KEY]).get(SPOT_KEY) or {}
    spot_price = float(spot_quote.get("last_price") or 0.0)
    if spot_price <= 0:
        raise RuntimeError("No NIFTY spot price available for WebSocket subscriptions")
    expiries = set(available_option_expiries()[:max(1, int(max_expiries))])
    lower, upper = spot_price * 0.8, spot_price * 1.2
    selected = [spot]
    for row in _nifty_derivatives(instruments):
        kind = str(row.get("instrument_type", "")).upper()
        if kind == "FUT" and row.get("expiry") >= datetime.now(IST).date():
            selected.append(row)
        elif (
            kind in {"CE", "PE"}
            and row.get("expiry") in expiries
            and lower <= float(row.get("strike") or 0.0) <= upper
        ):
            selected.append(row)
    tokens = list(dict.fromkeys(int(row["instrument_token"]) for row in selected))
    if len(tokens) > 3000:
        raise RuntimeError(f"NIFTY WebSocket subscription requires {len(tokens)} tokens; limit is 3000")
    return tokens


def _rate_limited_quote(keys: list[str]) -> dict:
    global _last_quote_at
    if not keys:
        return {}
    minimum = float(os.environ.get("ZERODHA_QUOTE_MIN_INTERVAL_SECONDS", "1.0"))
    with _quote_lock:
        delay = minimum - (time.monotonic() - _last_quote_at)
        if delay > 0:
            time.sleep(delay)
        result = _kite_client().quote(*keys)
        _last_quote_at = time.monotonic()
    return result or {}


def get_quotes(keys: list[str]) -> dict:
    quotes: dict = {}
    unique = list(dict.fromkeys(keys))
    for offset in range(0, len(unique), QUOTE_BATCH_SIZE):
        quotes.update(_rate_limited_quote(unique[offset:offset + QUOTE_BATCH_SIZE]))
    return quotes


def get_instrument_quotes(rows: list[dict]) -> dict:
    """Use fresh scheduler WebSocket ticks, then REST for the missing rows."""
    from data_sources import zerodha_stream

    quotes: dict = {}
    missing: list[str] = []
    max_age = int(os.environ.get("ZERODHA_TICK_MAX_AGE_SECONDS", "15"))
    for row in rows:
        key = _instrument_key(row)
        cached = zerodha_stream.cached_quote(
            int(row["instrument_token"]), max_age_seconds=max_age
        )
        if cached:
            quotes[key] = cached
        else:
            missing.append(key)
    quotes.update(get_quotes(missing))
    return quotes


def _instrument_key(row: dict) -> str:
    return f"{row['exchange']}:{row['tradingsymbol']}"


def _future_for_expiry(futures: list[dict], option_expiry: date) -> dict:
    eligible = [row for row in futures if row["expiry"] >= option_expiry]
    if eligible:
        return min(eligible, key=lambda row: row["expiry"])
    if futures:
        return max(futures, key=lambda row: row["expiry"])
    raise RuntimeError("No active NIFTY futures contract is available")


def _active_expiry_start(snapshot_at: datetime) -> date:
    """Earliest non-expired NIFTY option date at the snapshot time."""
    local = (
        snapshot_at.astimezone(IST)
        if snapshot_at.tzinfo
        else snapshot_at.replace(tzinfo=IST)
    )
    if (local.hour, local.minute) >= (15, 30):
        return local.date() + timedelta(days=1)
    return local.date()


def _validated_gex_contracts(rows: list[dict]) -> tuple[list[dict], dict]:
    """Exclude incomplete strike pairs without treating unknown gamma as zero."""
    unknown = [
        row for row in rows
        if row["open_interest"] > 0 and row.get("gamma") is None
    ]
    bad_pairs = {
        (row["expiry"], row["strike"])
        for row in unknown
    }
    kept = [
        row for row in rows
        if (row["expiry"], row["strike"]) not in bad_pairs
    ]

    expiry_values = sorted({row["expiry"] for row in rows})
    for expiry in expiry_values:
        if not any(
            row["expiry"] == expiry
            and row["open_interest"] > 0
            and row.get("gamma") is not None
            for row in kept
        ):
            raise RuntimeError(
                f"No validated non-zero-OI NIFTY contracts remain for {expiry}"
            )

    by_expiry = {}
    for expiry in expiry_values:
        expiry_rows = [row for row in rows if row["expiry"] == expiry]
        expiry_kept = [row for row in kept if row["expiry"] == expiry]
        expiry_unknown = [row for row in unknown if row["expiry"] == expiry]
        expiry_bad_pairs = {
            (row["expiry"], row["strike"])
            for row in expiry_unknown
        }
        by_expiry[expiry.isoformat()] = {
            "status": "partial" if expiry_unknown else "complete",
            "total_contracts": len(expiry_rows),
            "included_contracts": len(expiry_kept),
            "unknown_gamma_contracts": len(expiry_unknown),
            "excluded_contracts": len(expiry_rows) - len(expiry_kept),
            "excluded_strikes": len(expiry_bad_pairs),
            "excluded_open_interest": sum(
                row["open_interest"] for row in expiry_rows
                if (row["expiry"], row["strike"]) in expiry_bad_pairs
            ),
        }

    quality = {
        "status": "partial" if unknown else "complete",
        "total_contracts": len(rows),
        "included_contracts": len(kept),
        "unknown_gamma_contracts": len(unknown),
        "excluded_contracts": len(rows) - len(kept),
        "excluded_strikes": len(bad_pairs),
        "excluded_open_interest": sum(
            row["open_interest"] for row in rows
            if (row["expiry"], row["strike"]) in bad_pairs
        ),
        "reason": "canonical IV unavailable or rejected by the wing-quality filter",
        "by_expiry": by_expiry,
    }
    if unknown:
        log.warning(
            "NIFTY chain has partial IV coverage: excluded %d strike pairs "
            "(%d contracts; %d contracts had non-zero OI with unknown gamma)",
            quality["excluded_strikes"],
            quality["excluded_contracts"],
            quality["unknown_gamma_contracts"],
        )
    return kept, quality


def get_option_chain(
    symbol: str,
    expiry_date: date | None = None,
    *,
    expiry_dates: list[date] | None = None,
    asset_type: str | None = None,
    max_expiries: int = 12,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> dict:
    """Fetch and normalize an SPX-compatible NIFTY option-chain payload."""
    if symbol.upper() != "NIFTY":
        raise ValueError("The Zerodha provider currently supports NIFTY only")

    snapshot_at = datetime.now(IST)
    instruments = _nifty_derivatives()
    all_instruments = get_instruments()
    spot_instrument = next(
        (row for row in all_instruments
         if str(row.get("exchange", "")).upper() == "NSE"
         and str(row.get("tradingsymbol", "")).upper() == "NIFTY 50"),
        None,
    )
    if not spot_instrument:
        raise RuntimeError("NIFTY 50 spot instrument is absent from the instrument master")
    spot_quote = get_instrument_quotes([spot_instrument]).get(SPOT_KEY) or {}
    spot_price = float(spot_quote.get("last_price") or 0.0)
    if spot_price <= 0:
        raise RuntimeError("Zerodha returned no valid NIFTY spot price")

    expiries = available_option_expiries(from_date=_active_expiry_start(snapshot_at))
    if expiry_dates is not None:
        selected_expiries = [value for value in expiry_dates if value in expiries]
    elif expiry_date is not None:
        selected_expiries = [expiry_date] if expiry_date in expiries else []
    else:
        selected_expiries = expiries[:max(1, int(max_expiries))]
    if not selected_expiries:
        raise RuntimeError("No requested NIFTY option expiry is listed")

    lower, upper = spot_price * 0.8, spot_price * 1.2
    options = [
        row for row in instruments
        if str(row.get("instrument_type", "")).upper() in {"CE", "PE"}
        and row.get("expiry") in selected_expiries
        and lower <= float(row.get("strike") or 0.0) <= upper
    ]
    futures = [
        row for row in instruments
        if str(row.get("instrument_type", "")).upper() == "FUT"
        and isinstance(row.get("expiry"), date)
        and row["expiry"] >= snapshot_at.date()
    ]
    if not options:
        raise RuntimeError("No NIFTY contracts remain inside the configured strike band")

    futures_by_expiry = {
        expiry: _future_for_expiry(futures, expiry) for expiry in selected_expiries
    }
    unique_futures = {
        int(row["instrument_token"]): row for row in futures_by_expiry.values()
    }
    future_quotes = get_instrument_quotes(list(unique_futures.values()))
    option_quotes = get_instrument_quotes(options)

    normalized: list[dict] = []
    future_prices: dict[str, float] = {}
    for expiry in selected_expiries:
        future = futures_by_expiry[expiry]
        future_key = _instrument_key(future)
        future_price = float((future_quotes.get(future_key) or {}).get("last_price") or 0.0)
        if future_price <= 0:
            raise RuntimeError(f"No valid NIFTY futures price for option expiry {expiry}")
        future_prices[expiry.isoformat()] = future_price
        contracts = []
        for row in options:
            if row["expiry"] != expiry:
                continue
            key = _instrument_key(row)
            lot_size = float(row.get("lot_size") or 0)
            if lot_size <= 0:
                raise RuntimeError(
                    f"Invalid lot size for NIFTY instrument {row.get('tradingsymbol')}"
                )
            contracts.append({
                "type": "C" if row["instrument_type"] == "CE" else "P",
                "strike": float(row["strike"]),
                "expiry": expiry,
                "contract_multiplier": lot_size,
                "instrument_token": int(row["instrument_token"]),
                "tradingsymbol": row["tradingsymbol"],
                "exchange": row["exchange"],
                "quote": option_quotes.get(key) or {},
            })
        processed = process_contracts(
            contracts,
            futures_price=future_price,
            snapshot_at=snapshot_at,
            risk_free_rate=risk_free_rate,
        )
        for contract in processed:
            contract["reference_price"] = future_price
        normalized.extend(processed)

    if not normalized:
        raise RuntimeError("Zerodha NIFTY normalization produced an empty option chain")
    normalized, data_quality = _validated_gex_contracts(normalized)

    nearest_future = future_prices[selected_expiries[0].isoformat()]
    return {
        "options": normalized,
        "spot_price": spot_price,
        "reference_price": nearest_future,
        "futures_price": nearest_future,
        "futures_by_expiry": future_prices,
        "snapshot_at": snapshot_at,
        "provider": "zerodha",
        "market": "nse",
        "market_timezone": "Asia/Kolkata",
        "currency": "INR",
        "display_unit": "crore",
        "pricing_model": "black76",
        "data_quality": data_quality,
    }


def get_historical_bars(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    *,
    interval: str = "day",
) -> list[dict]:
    if symbol.upper() != "NIFTY":
        raise ValueError("The Zerodha provider currently supports NIFTY only")
    spot = next(
        (row for row in get_instruments()
         if str(row.get("exchange", "")).upper() == "NSE"
         and str(row.get("tradingsymbol", "")).upper() == "NIFTY 50"),
        None,
    )
    if not spot:
        raise RuntimeError("NIFTY 50 spot instrument is absent from the instrument master")
    rows = _kite_client().historical_data(
        int(spot["instrument_token"]), start_dt, end_dt, interval, oi=False
    )
    return [{
        "date": (row["date"].date().isoformat() if isinstance(row.get("date"), datetime)
                 else str(row.get("date", ""))[:10]),
        "open": round(float(row["open"]), 4),
        "high": round(float(row["high"]), 4),
        "low": round(float(row["low"]), 4),
        "close": round(float(row["close"]), 4),
        "volume": int(row.get("volume") or 0),
    } for row in rows]


def check_credentials() -> dict:
    profile = _kite_client().profile()
    return {
        "ok": True,
        "user_id": profile.get("user_id"),
        "user_name": profile.get("user_name"),
    }


def estimate_margins(orders: list[dict]) -> dict | list:
    """Estimate margins only; this module deliberately exposes no order methods."""
    if not orders:
        raise ValueError("At least one hypothetical order is required")
    client = _kite_client()
    if len(orders) == 1:
        return client.order_margins(orders)
    return client.basket_order_margins(orders, consider_positions=True)


def cache_status() -> dict:
    files = sorted(_cache_dir(create=False).glob("instruments-*.json"), reverse=True)
    latest = files[0] if files else None
    return {
        "path": str(latest) if latest else None,
        "exists": bool(latest),
        "modified_at": datetime.fromtimestamp(latest.stat().st_mtime, IST) if latest else None,
    }
