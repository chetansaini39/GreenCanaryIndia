"""
Schwab API client — options chain and price history.

Auth: OAuth2 Authorization Code flow (one-time browser login).
Token is cached at SCHWAB_TOKEN_PATH and auto-refreshed by schwab-py.

⚠  Single-instance constraint: only one process may hold the token file
   at a time. The scheduler runner enforces this via a PID lock file.
   The Flask web app never calls this module directly.

Env vars required:
  SCHWAB_API_KEY
  SCHWAB_API_SECRET
  SCHWAB_TOKEN_PATH     path to the cached OAuth token JSON
  SCHWAB_CALLBACK_URL   e.g. https://127.0.0.1:8182 (one-time setup only)

Option-chain fetch parameters:
  - Cash-settled indices (SPX, NDX, …): $-prefixed symbol, SINGLE strategy,
    21-day expiry window, current calendar month.
  - ETF index symbols (SPY, QQQ, IWM): plain symbol, ANALYTICAL strategy,
    14-day window.
  - Stocks: plain symbol, ANALYTICAL strategy, 90-day window (full 12-week weekly).
"""
import os
import logging
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

_CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))

import schwab  # schwab-py
from schwab.client import Client

from data_sources.retry import with_retry

log = logging.getLogger(__name__)

_INDEX_CASH_STRIKE_COUNT = 25   # SPX/NDX ±20% storage band
_INDEX_ETF_STRIKE_COUNT = 25    # SPY/QQQ/IWM
_STOCK_STRIKE_COUNT = 35        # stocks ±50% storage band
_INDEX_DAYS_FORWARD = 21
_INDEX_ETF_DAYS_FORWARD = 14    # ETF index symbols (non cash-settled)
_STOCK_DAYS_FORWARD = 90        # covers the 12-week weekly window (12th Friday ≤ 83 days out) + margin
_INDEX_FALLBACK_DAYS_FORWARD = 42  # wider retry when cash index chain is empty

# Normalised option record shape shared across all three data sources:
# {
#   "type": "C" | "P",
#   "strike": float,
#   "expiry": date,
#   "gamma": float,
#   "delta": float,
#   "theta": float,
#   "vega": float,
#   "iv": float,          # decimal, e.g. 0.25 = 25 %
#   "open_interest": int,
# }


@lru_cache(maxsize=1)
def _get_client():
    """Return a cached Schwab client loaded from the token file.

    lru_cache means we build the client once per process lifetime, which
    satisfies the single-instance / single-token-writer constraint.
    """
    client = schwab.auth.client_from_token_file(
        token_path=os.environ["SCHWAB_TOKEN_PATH"],
        api_key=os.environ["SCHWAB_API_KEY"],
        app_secret=os.environ["SCHWAB_API_SECRET"],
    )
    client.set_timeout(30.0)
    return client


# Schwab uses a $ prefix for cash-settled index options (e.g. $SPX not SPX).
_SCHWAB_INDEX_SYMBOLS = {
    "SPX": "$SPX",
    "NDX": "$NDX",
    "RUT": "$RUT",
    "VIX": "$VIX",
}


def _schwab_symbol(symbol: str) -> str:
    return _SCHWAB_INDEX_SYMBOLS.get(symbol.upper(), symbol)


def _is_cash_settled_index(symbol: str) -> bool:
    """True for SPX/NDX-style symbols that require the $ prefix and SINGLE strategy."""
    return symbol.upper() in _SCHWAB_INDEX_SYMBOLS


def _current_exp_month():
    return Client.Options.ExpirationMonth.ALL


def _build_option_chain_kwargs(
    symbol: str,
    expiry_date: date | None = None,
    *,
    asset_type: str | None = None,
    strike_count: int | None = None,
    days_forward: int | None = None,
) -> dict:
    """Build get_option_chain kwargs tuned for GEX storage bands."""
    now = datetime.now()
    is_index = asset_type == "index"

    if expiry_date is not None:
        start_dt = datetime.combine(expiry_date, datetime.min.time())
        end_dt = start_dt + timedelta(days=1)
        strategy = Client.Options.Strategy.SINGLE
        count = strike_count or (
            _INDEX_CASH_STRIKE_COUNT if _is_cash_settled_index(symbol)
            else _INDEX_ETF_STRIKE_COUNT if is_index
            else _STOCK_STRIKE_COUNT
        )
        strike_range = Client.Options.StrikeRange.NEAR_THE_MONEY
    elif _is_cash_settled_index(symbol):
        start_dt = now
        end_dt = now + timedelta(days=days_forward or _INDEX_DAYS_FORWARD)
        strategy = Client.Options.Strategy.SINGLE
        count = strike_count or _INDEX_CASH_STRIKE_COUNT
        strike_range = Client.Options.StrikeRange.NEAR_THE_MONEY
    elif is_index:
        start_dt = now
        end_dt = now + timedelta(days=days_forward or _INDEX_ETF_DAYS_FORWARD)
        strategy = Client.Options.Strategy.ANALYTICAL
        count = strike_count or _INDEX_ETF_STRIKE_COUNT
        strike_range = Client.Options.StrikeRange.NEAR_THE_MONEY
    else:
        start_dt = now
        end_dt = now + timedelta(days=days_forward or _STOCK_DAYS_FORWARD)
        strategy = Client.Options.Strategy.ANALYTICAL
        count = strike_count or _STOCK_STRIKE_COUNT
        strike_range = Client.Options.StrikeRange.NEAR_THE_MONEY

    return {
        "contract_type": Client.Options.ContractType.ALL,
        "strike_count": count,
        "strategy": strategy,
        "interval": 5.0,
        "strike_range": strike_range,
        "from_date": start_dt,
        "to_date": end_dt,
        "entitlement": Client.Options.Entitlement.PAYING_PRO,
        "exp_month": _current_exp_month(),
        "option_type": Client.Options.Type.ALL,
    }


# ── Option chain ───────────────────────────────────────────────────────────

def get_option_chain(
    symbol: str,
    expiry_date: date | None = None,
    *,
    asset_type: str | None = None,
) -> dict:
    """Fetch and normalise the option chain for symbol.

    Args:
        symbol:      underlying ticker (e.g. "SPX", "SPY", "AAPL")
        expiry_date: if given, filter to a single expiry; otherwise near-term
                     expiries are returned using asset-type-aware windows
                     (21 days cash-settled index, 14 days ETF index, 21 days stock).
        asset_type:  ``index`` or ``stock`` from symbols_config — tunes
                     strike_count and expiry window for smaller API payloads.

    Returns:
        {"options": [<normalised records>], "spot_price": float}

    Raises:
        ValueError: if Schwab returns API errors or an empty chain.
    """
    schwab_symbol = _schwab_symbol(symbol)
    chain_kwargs = _build_option_chain_kwargs(symbol, expiry_date, asset_type=asset_type)

    def _fetch(kwargs: dict):
        client = _get_client()
        resp = client.get_option_chain(schwab_symbol, **kwargs)
        resp.raise_for_status()
        return resp.json()

    raw = with_retry(
        lambda: _fetch(chain_kwargs),
        label=f"schwab.get_option_chain({schwab_symbol})",
    )

    # Off-hours Schwab often returns an empty near-term index chain. Widen the
    # window once (still SINGLE strategy) before giving up on Schwab.
    if (
        expiry_date is None
        and _is_cash_settled_index(symbol)
        and not raw.get("callExpDateMap")
        and not raw.get("putExpDateMap")
    ):
        expanded = dict(chain_kwargs)
        expanded["to_date"] = datetime.now() + timedelta(days=_INDEX_FALLBACK_DAYS_FORWARD)
        log.info(
            "Near-term Schwab chain empty for %s — retrying with %d-day window",
            symbol,
            _INDEX_FALLBACK_DAYS_FORWARD,
        )
        raw = with_retry(
            lambda: _fetch(expanded),
            label=f"schwab.get_option_chain({schwab_symbol}, expanded)",
        )

    return _normalise_chain(raw, symbol)


def get_price_history_5min(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    """Fetch 5-minute OHLCV bars for intraday chart overlays.

    Returns a list of {"datetime": datetime, "open": float, "high": float,
    "low": float, "close": float, "volume": int}.
    """
    schwab_symbol = _schwab_symbol(symbol)

    def _fetch():
        client = _get_client()
        resp = client.get_price_history_every_five_minutes(
            schwab_symbol,
            start_datetime=start_dt,
            end_datetime=end_dt,
        )
        resp.raise_for_status()
        return resp.json()

    raw = with_retry(_fetch, label=f"schwab.get_price_history_5min({schwab_symbol})")
    return _normalise_bars(raw)


# ── Normalisation helpers ──────────────────────────────────────────────────

def _normalise_chain(raw: dict, symbol: str) -> dict:
    """Parse schwab-py option chain JSON into the common normalised shape.

    Verified field names (confirmed from ComputeGexDataFromSchwab.py line 864):
      raw["underlyingPrice"]                          → spot price
      raw["callExpDateMap"][expiry_str][strike_str]   → list of call dicts
      raw["putExpDateMap"][expiry_str][strike_str]    → list of put dicts

    Per-option fields (confirmed):
      "gamma", "delta", "theta", "vega", "openInterest"
      "volatility"  ← IV as a PERCENTAGE (e.g. 14.52 means 14.52%).
                      Divide by 100 to store as decimal (0.1452) consistent
                      with the normalised shape and yfinance's impliedVolatility.

    ⚠  Strike keys in callExpDateMap / putExpDateMap are STRINGS, not numbers
       (e.g. "5430.0").  Always coerce with float(strike_str).  Do not assume
       integer or numeric type — the Schwab API returns them as JSON object keys.
    """
    if raw.get("errors"):
        raise ValueError(f"Schwab option chain errors for {symbol}: {raw['errors']}")

    spot_price: float = float(raw.get("underlyingPrice") or 0.0)
    records: list[dict] = []

    for opt_type, exp_map_key in (("C", "callExpDateMap"), ("P", "putExpDateMap")):
        exp_map = raw.get(exp_map_key, {})
        for expiry_str, strikes in exp_map.items():
            # expiry_str format: "2024-04-19:5"  (date:daysToExpiration)
            expiry_date_str = expiry_str.split(":")[0]
            try:
                expiry = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
            except ValueError:
                log.warning("Cannot parse Schwab expiry string %r — skipping", expiry_str)
                continue

            for strike_str, option_list in strikes.items():
                # Strike keys are always strings ("5430.0") — coerce explicitly.
                strike = float(strike_str)
                for opt in option_list:
                    try:
                        records.append({
                            "type": opt_type,
                            "strike": strike,
                            "expiry": expiry,
                            "gamma": float(opt.get("gamma", 0.0)),
                            "delta": float(opt.get("delta", 0.0)),
                            "theta": float(opt.get("theta", 0.0)),
                            "vega": float(opt.get("vega", 0.0)),
                            # volatility is a percentage — divide by 100 for decimal IV
                            "iv": float(opt.get("volatility", 0.0)) / 100.0,
                            "open_interest": int(opt.get("openInterest", 0)),
                        })
                    except (KeyError, TypeError, ValueError) as exc:
                        log.debug("Skipping malformed Schwab option record: %s", exc)

    if not records:
        raise ValueError(
            f"Schwab returned no option contracts for {symbol} "
            f"(underlyingPrice={spot_price})"
        )

    return {"options": records, "spot_price": spot_price}


def _normalise_bars(raw: dict) -> list[dict]:
    """Parse schwab-py price history JSON into OHLCV dicts.

    Candle shape: raw["candles"] = [{"datetime": <epoch_ms>, "open", "high",
                                      "low", "close", "volume"}, ...]

    Datetime conversion (verified against SchwabOptionData._candles_json_to_df):
      The epoch-ms value is treated as a naive UTC timestamp by the library,
      then localised to UTC and converted to America/Chicago — matching the
      behaviour of tz_localize("UTC").tz_convert("America/Chicago") on a
      naive pandas DatetimeIndex.  Do NOT use datetime.fromtimestamp() (which
      applies the system timezone) or pd.to_datetime(..., utc=True) (wrong).
    """
    bars = []
    for candle in raw.get("candles", []):
        try:
            # Interpret epoch ms as UTC, then convert to CT — matches schwab-py
            dt_utc = datetime.fromtimestamp(candle["datetime"] / 1000, tz=timezone.utc)
            dt_ct = dt_utc.astimezone(_CT)
            bars.append({
                "datetime": dt_ct,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": int(candle["volume"]),
            })
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("Skipping malformed Schwab candle: %s", exc)
    return bars
