"""Small, I/O-free Black-76 implementation for Zerodha NIFTY options."""
from __future__ import annotations

import math
from datetime import date, datetime, time
from statistics import median
from zoneinfo import ZoneInfo

from scipy.optimize import brentq
from scipy.stats import norm


IST = ZoneInfo("Asia/Kolkata")
CALENDAR_DAYS_PER_YEAR = 365
TRADING_DAYS_PER_YEAR = 252
DEFAULT_RISK_FREE_RATE = 0.055
MIN_TIME_YEARS = 1.0 / (CALENDAR_DAYS_PER_YEAR * 24 * 60)


def _as_ist(value: datetime) -> datetime:
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def time_to_expiry(expiry: date | datetime | str, snapshot_at: datetime) -> float:
    """Precise calendar-year fraction to the 15:30 IST contract expiry."""
    if isinstance(expiry, str):
        expiry_date = date.fromisoformat(expiry[:10])
    elif isinstance(expiry, datetime):
        expiry_date = expiry.date()
    else:
        expiry_date = expiry
    expiry_at = datetime.combine(expiry_date, time(15, 30), tzinfo=IST)
    seconds = (expiry_at - _as_ist(snapshot_at)).total_seconds()
    if seconds <= 0:
        return math.nan
    return max(seconds / (CALENDAR_DAYS_PER_YEAR * 86400), MIN_TIME_YEARS)


class Black76Calculator:
    def __init__(self, risk_free_rate: float = DEFAULT_RISK_FREE_RATE):
        self.risk_free_rate = float(risk_free_rate)

    @staticmethod
    def _d1(futures: float, strike: float, years: float, volatility: float) -> float:
        return (
            math.log(futures / strike) + 0.5 * volatility * volatility * years
        ) / (volatility * math.sqrt(years))

    def price(
        self,
        futures: float,
        strike: float,
        years: float,
        volatility: float,
        option_type: str,
    ) -> float:
        if years <= 0:
            return max(futures - strike, 0.0) if option_type == "C" else max(strike - futures, 0.0)
        d1 = self._d1(futures, strike, years, volatility)
        d2 = d1 - volatility * math.sqrt(years)
        discount = math.exp(-self.risk_free_rate * years)
        if option_type == "C":
            return discount * (futures * norm.cdf(d1) - strike * norm.cdf(d2))
        return discount * (strike * norm.cdf(-d2) - futures * norm.cdf(-d1))

    def implied_volatility(
        self,
        market_price: float,
        futures: float,
        strike: float,
        years: float,
        option_type: str,
    ) -> float | None:
        if not all(math.isfinite(v) and v > 0 for v in (market_price, futures, strike, years)):
            return None
        discount = math.exp(-self.risk_free_rate * years)
        if option_type == "C":
            intrinsic = discount * max(futures - strike, 0.0)
            maximum = discount * futures
        else:
            intrinsic = discount * max(strike - futures, 0.0)
            maximum = discount * strike
        price = min(max(float(market_price), intrinsic), maximum)

        def objective(volatility: float) -> float:
            return self.price(futures, strike, years, volatility, option_type) - price

        try:
            return float(brentq(objective, 1e-6, 10.0, xtol=1e-8, maxiter=200))
        except ValueError:
            return 1e-6 if price - intrinsic < 0.01 else None
        except RuntimeError:
            return None

    def greeks(
        self,
        futures: float,
        strike: float,
        years: float,
        volatility: float,
        option_type: str,
    ) -> dict[str, float]:
        if not all(math.isfinite(v) and v > 0 for v in (futures, strike, years, volatility)):
            raise ValueError("Black-76 inputs must be finite positive values")
        d1 = self._d1(futures, strike, years, volatility)
        d2 = d1 - volatility * math.sqrt(years)
        sqrt_t = math.sqrt(years)
        discount = math.exp(-self.risk_free_rate * years)
        density = norm.pdf(d1)
        gamma = discount * density / (futures * volatility * sqrt_t)
        vega = futures * discount * density * sqrt_t / 100
        time_decay = -(futures * discount * density * volatility) / (2 * sqrt_t)
        if option_type == "C":
            value_term = futures * norm.cdf(d1) - strike * norm.cdf(d2)
            delta = discount * norm.cdf(d1)
        else:
            value_term = strike * norm.cdf(-d2) - futures * norm.cdf(-d1)
            delta = -discount * norm.cdf(-d1)
        theta = (time_decay + self.risk_free_rate * discount * value_term) / TRADING_DAYS_PER_YEAR
        rho = -years * discount * value_term / 100
        return {
            "delta": float(delta),
            "gamma": float(gamma),
            "theta": float(theta),
            "vega": float(vega),
            "rho": float(rho),
        }


def quote_price(quote: dict) -> tuple[float | None, str | None]:
    """Choose weighted-mid, mid, or LTP using the reference calculator rules."""
    depth = quote.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    bid = (buys[0] if buys else {}) or {}
    ask = (sells[0] if sells else {}) or {}
    bid_price = float(bid.get("price") or 0.0)
    ask_price = float(ask.get("price") or 0.0)
    if bid_price > 0 and ask_price > 0 and ask_price >= bid_price:
        midpoint = (bid_price + ask_price) / 2.0
        bid_qty = int(bid.get("quantity") or 0)
        ask_qty = int(ask.get("quantity") or 0)
        if ask_price - bid_price > 0.1 * midpoint and bid_qty > 0 and ask_qty > 0:
            value = (bid_price * ask_qty + ask_price * bid_qty) / (bid_qty + ask_qty)
            return value, "weighted_mid"
        return midpoint, "mid"
    last_price = float(quote.get("last_price") or 0.0)
    return (last_price, "ltp") if last_price > 0 else (None, None)


def process_contracts(
    contracts: list[dict],
    *,
    futures_price: float,
    snapshot_at: datetime,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> list[dict]:
    """Convert paired Zerodha quotes into the normalized RetailGex shape."""
    calculator = Black76Calculator(risk_free_rate)
    rows: list[dict] = []
    grouped: dict[tuple[date, float], dict[str, dict]] = {}

    for contract in contracts:
        expiry = contract["expiry"]
        if isinstance(expiry, str):
            expiry = date.fromisoformat(expiry[:10])
        years = time_to_expiry(expiry, snapshot_at)
        price, source = quote_price(contract.get("quote") or {})
        option_type = str(contract["type"]).upper()
        strike = float(contract["strike"])
        iv = None if price is None or math.isnan(years) else calculator.implied_volatility(
            price, futures_price, strike, years, option_type
        )
        row = {
            **contract,
            "type": option_type,
            "strike": strike,
            "expiry": expiry,
            "market_price": price,
            "market_price_source": source,
            "T_years": years,
            "iv": iv,
        }
        rows.append(row)
        grouped.setdefault((expiry, strike), {})[option_type] = row

    # Liquid-leg canonical IV, applied independently to each expiry.
    for (_, strike), pair in grouped.items():
        call_iv = pair.get("C", {}).get("iv")
        put_iv = pair.get("P", {}).get("iv")
        valid_call = call_iv is not None and call_iv > 0
        valid_put = put_iv is not None and put_iv > 0
        if valid_call and valid_put:
            canonical = (
                put_iv if strike < futures_price * 0.999
                else call_iv if strike > futures_price * 1.001
                else (call_iv + put_iv) / 2.0
            )
        elif valid_put:
            canonical = put_iv
        elif valid_call:
            canonical = call_iv
        else:
            continue
        for row in pair.values():
            row["iv"] = canonical

        call_price = pair.get("C", {}).get("market_price")
        put_price = pair.get("P", {}).get("market_price")
        years = next(iter(pair.values()))["T_years"]
        if call_price is not None and put_price is not None and math.isfinite(years):
            residual = (
                call_price - put_price
                - math.exp(-risk_free_rate * years) * (futures_price - strike)
            )
            residual_pct = residual / max(call_price, put_price, 0.01) * 100
            for row in pair.values():
                row["parity_residual"] = residual
                row["parity_residual_pct"] = residual_pct

    # Match the reference wing filter per expiry.
    for expiry in {row["expiry"] for row in rows}:
        near_atm = [
            row["iv"] for row in rows
            if row["expiry"] == expiry
            and abs(row["strike"] - futures_price) < 300
            and row.get("iv") is not None
        ]
        anchor = median(near_atm) if near_atm else None
        if anchor and anchor > 0:
            for row in rows:
                if row["expiry"] == expiry and row.get("iv") is not None:
                    if abs(row["iv"] - anchor) / anchor > 3.0:
                        row["iv"] = None
        valid_rows = [
            row for row in rows
            if row["expiry"] == expiry and row.get("iv") is not None
        ]
        atm_iv = (
            min(valid_rows, key=lambda row: abs(row["strike"] - futures_price))["iv"]
            if valid_rows else None
        )
        for row in rows:
            if row["expiry"] == expiry:
                row["atm_iv"] = atm_iv

    normalized: list[dict] = []
    for row in rows:
        oi = int((row.get("quote") or {}).get("oi") or row.get("open_interest") or 0)
        iv = row.get("iv")
        greek_values = (
            calculator.greeks(
                futures_price, row["strike"], row["T_years"], iv, row["type"]
            )
            if iv is not None else {}
        )
        quote = row.get("quote") or {}
        normalized.append({
            "type": row["type"],
            "strike": row["strike"],
            "expiry": row["expiry"],
            "iv": float(iv) if iv is not None else 0.0,
            "gamma": greek_values.get("gamma") if oi > 0 else greek_values.get("gamma", 0.0),
            "delta": greek_values.get("delta", 0.0),
            "theta": greek_values.get("theta", 0.0),
            "vega": greek_values.get("vega", 0.0),
            "rho": greek_values.get("rho", 0.0),
            "open_interest": oi,
            "volume": int(quote.get("volume") or 0),
            "contract_multiplier": float(row["contract_multiplier"]),
            "instrument_token": int(row["instrument_token"]),
            "tradingsymbol": row["tradingsymbol"],
            "market_price": row.get("market_price"),
            "market_price_source": row.get("market_price_source"),
            "parity_residual": row.get("parity_residual"),
            "parity_residual_pct": row.get("parity_residual_pct"),
            "atm_iv": row.get("atm_iv"),
            "quote_timestamp": quote.get("timestamp") or quote.get("last_trade_time"),
        })
    return normalized
