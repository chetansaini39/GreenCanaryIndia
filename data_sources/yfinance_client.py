"""
yfinance fallback client.

Used when Schwab and/or CBOE are unavailable or rate-limited.
No authentication required.

Limitations (free tier):
  - Intraday intervals < 1d are limited to the last 60 days of history.
  - Option chains reflect end-of-day data, not real-time.
  - Not a substitute for the platform's own permanent Mongo history.
"""
import logging
from datetime import date, datetime

import yfinance as yf

from data_sources.retry import with_retry

log = logging.getLogger(__name__)


# ── Option chain ───────────────────────────────────────────────────────────

def get_option_chain(symbol: str, expiry: date | None = None) -> dict:
    """Fetch and normalise an option chain via yfinance.

    Args:
        symbol: underlying ticker (e.g. "SPY", "AAPL").
                For SPX use "^GSPC"; for NDX use "^NDX".
        expiry: if given, fetch that specific expiry date string;
                otherwise uses the nearest available expiry.

    Returns:
        {"options": [<normalised records>], "spot_price": float}
    """
    def _fetch():
        ticker = yf.Ticker(symbol)
        spot_price = _get_spot(ticker)

        if expiry:
            expiry_str = expiry.strftime("%Y-%m-%d")
        else:
            available = ticker.options
            if not available:
                raise ValueError(f"yfinance: no option expiries available for {symbol}")
            expiry_str = available[0]

        chain = ticker.option_chain(expiry_str)
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        records = (
            _normalise_df(chain.calls, "C", expiry_date)
            + _normalise_df(chain.puts, "P", expiry_date)
        )
        return {"options": records, "spot_price": spot_price}

    return with_retry(_fetch, label=f"yfinance.get_option_chain({symbol})")


# ── OHLCV history ─────────────────────────────────────────────────────────

def get_ohlcv(symbol: str, period: str = "5d", interval: str = "1d") -> list[dict]:
    """Fetch OHLCV bars for intraday chart overlays or historical context.

    Args:
        period:   yfinance period string — "1d", "5d", "1mo", etc.
        interval: yfinance interval string — "5m", "1h", "1d", etc.
                  Intraday intervals are limited to the last 60 days.
    """
    def _fetch():
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        bars = []
        for ts, row in df.iterrows():
            bars.append({
                "datetime": ts.to_pydatetime(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]),
            })
        return bars

    return with_retry(_fetch, label=f"yfinance.get_ohlcv({symbol})")


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_spot(ticker: yf.Ticker) -> float:
    """Best-effort spot price from yfinance fast_info."""
    try:
        price = ticker.fast_info.get("last_price") or ticker.fast_info.get("regularMarketPrice")
        if price:
            return float(price)
    except Exception:
        pass
    # Last resort: latest close from 1-day history
    hist = ticker.history(period="1d")
    if not hist.empty:
        return float(hist["Close"].iloc[-1])
    raise ValueError(f"yfinance: cannot determine spot price for {ticker.ticker}")


def _normalise_df(df, opt_type: str, expiry_date: date) -> list[dict]:
    """Convert a yfinance calls/puts DataFrame to normalised option records.

    yfinance column names: strike, gamma, delta, theta, vega,
    impliedVolatility, openInterest.
    """
    records = []
    for _, row in df.iterrows():
        try:
            records.append({
                "type": opt_type,
                "strike": float(row["strike"]),
                "expiry": expiry_date,
                "gamma": float(row.get("gamma", 0.0)),
                "delta": float(row.get("delta", 0.0)),
                "theta": float(row.get("theta", 0.0)),
                "vega": float(row.get("vega", 0.0)),
                "iv": float(row.get("impliedVolatility", 0.0)),
                "open_interest": int(row.get("openInterest", 0)),
            })
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("Skipping malformed yfinance row: %s", exc)
    return records
