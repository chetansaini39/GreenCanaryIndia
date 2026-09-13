"""Deterministic SPX EOD analysis used by the review-only X pipeline."""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import gex_engine

from app.models import gex_rolling_21d
from app.utils.time import now_ct
from scheduler.market_utils import is_trading_day, trade_date_ct


_CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))
_SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0


def _expiry_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def filter_same_day_zero_dte(options: list[dict], trade_day: date) -> list[dict]:
    """Return only contracts whose stored expiry equals the source trade date."""
    return [row for row in options if _expiry_date(row.get("expiry")) == trade_day]


def _aware_ct(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} is missing or invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=_CT)
    return value.astimezone(_CT)


def validate_snapshot_freshness(
    snapshot: dict,
    trade_day: date,
    generated_at: datetime,
    *,
    max_age_minutes: int,
) -> datetime:
    """Validate that the EOD snapshot belongs to today and is still fresh."""
    snapshot_at = _aware_ct(snapshot.get("created_at"), "snapshot created_at")
    generated_at = _aware_ct(generated_at, "generation time")
    stored_trade_day = _expiry_date(snapshot.get("trade_date"))
    if stored_trade_day != trade_day or snapshot_at.date() != trade_day:
        raise ValueError("SPX EOD snapshot is not from the requested trade date")

    age = generated_at - snapshot_at
    if age < timedelta(minutes=-5):
        raise ValueError("SPX EOD snapshot timestamp is in the future")
    if age > timedelta(minutes=max_age_minutes):
        raise ValueError(
            f"SPX EOD snapshot is stale ({int(age.total_seconds() // 60)} minutes old)"
        )
    return snapshot_at


def calculate_zero_gamma_flip(
    contracts: list[dict],
    spot_price: float,
    snapshot_at: datetime,
    trade_day: date,
    *,
    grid_points: int = 2001,
) -> float:
    """Calculate the zero-gamma crossing nearest spot on a +/-20% spot grid.

    Black-Scholes gamma is recomputed with each contract's stored IV and OI.
    The rate and dividend terms are zero because their 0DTE effect is negligible.
    """
    try:
        spot = float(spot_price)
    except (TypeError, ValueError) as exc:
        raise ValueError("spot price is missing or invalid") from exc
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("spot price must be a positive finite number")
    if grid_points < 3:
        raise ValueError("zero-gamma grid requires at least three points")

    snapshot_at = _aware_ct(snapshot_at, "snapshot time")
    expiry_at = datetime.combine(trade_day, time(15, 0), tzinfo=_CT)
    seconds_to_expiry = (expiry_at - snapshot_at).total_seconds()
    if seconds_to_expiry <= 0:
        raise ValueError("snapshot is at or after the 3:00 PM CT 0DTE expiry time")
    years_to_expiry = seconds_to_expiry / _SECONDS_PER_YEAR

    valid: list[tuple[str, float, float, int]] = []
    option_types: set[str] = set()
    for row in contracts:
        option_type = str(row.get("type", "")).upper()
        if option_type not in ("C", "P"):
            continue
        if _expiry_date(row.get("expiry")) != trade_day:
            continue
        try:
            strike = float(row.get("strike"))
            iv = float(row.get("iv"))
            oi = int(row.get("open_interest"))
        except (TypeError, ValueError) as exc:
            raise ValueError("0DTE contract has invalid strike, IV, or OI") from exc
        if not math.isfinite(strike) or strike <= 0:
            raise ValueError("0DTE contract has an invalid strike")
        if oi < 0:
            raise ValueError("0DTE contract has negative open interest")
        if oi == 0:
            continue
        if not math.isfinite(iv) or iv <= 0 or iv > 5:
            raise ValueError("0DTE contract with open interest has invalid IV")
        valid.append((option_type, strike, iv, oi))
        option_types.add(option_type)

    if not valid:
        raise ValueError("0DTE slice has no contracts with valid IV and open interest")
    if option_types != {"C", "P"}:
        raise ValueError("0DTE slice needs valid call and put contracts for gamma flip")

    sqrt_t = math.sqrt(years_to_expiry)
    normalizer = math.sqrt(2.0 * math.pi)

    def total_gamma_exposure(grid_spot: float) -> float:
        total = 0.0
        for option_type, strike, iv, oi in valid:
            denominator = iv * sqrt_t
            d1 = (math.log(grid_spot / strike) + 0.5 * iv * iv * years_to_expiry) / denominator
            bs_gamma = math.exp(-0.5 * d1 * d1) / (
                normalizer * grid_spot * denominator
            )
            signed = 1.0 if option_type == "C" else -1.0
            total += signed * grid_spot * grid_spot * bs_gamma * oi
        return total

    low = spot * 0.8
    high = spot * 1.2
    step = (high - low) / (grid_points - 1)
    grid = [low + step * i for i in range(grid_points)]
    values = [total_gamma_exposure(grid_spot) for grid_spot in grid]

    crossings: list[float] = []
    for index, (left_spot, right_spot, left_gex, right_gex) in enumerate(zip(
        grid, grid[1:], values, values[1:]
    )):
        if not (math.isfinite(left_gex) and math.isfinite(right_gex)):
            continue
        if left_gex == 0:
            if index > 0 and values[index - 1] * right_gex < 0:
                crossings.append(left_spot)
            continue
        if right_gex == 0:
            next_index = index + 2
            if next_index < len(values) and left_gex * values[next_index] < 0:
                crossings.append(right_spot)
            continue
        if left_gex * right_gex < 0:
            slope = right_gex - left_gex
            if slope == 0:
                continue
            crossing = left_spot - left_gex * (right_spot - left_spot) / slope
            if left_spot <= crossing <= right_spot:
                crossings.append(crossing)

    if not crossings:
        raise ValueError("no reliable zero-gamma crossing exists within +/-20% of spot")
    return min(crossings, key=lambda value: abs(value - spot))


def _row_at_strike(rows: list[dict], strike: float) -> dict:
    for row in rows:
        if float(row.get("strike")) == float(strike):
            return row
    raise ValueError(f"GEX row is missing for strike {strike}")


def analyze_eod_contracts(
    contracts: list[dict],
    spot_price: float,
    trade_day: date,
    snapshot_at: datetime,
    *,
    source_snapshot_ref=None,
) -> tuple[dict, dict]:
    """Run the production SPX EOD math against an in-memory contract list."""
    snapshot_at = _aware_ct(snapshot_at, "snapshot time")
    zero_dte = filter_same_day_zero_dte(contracts, trade_day)
    if not zero_dte:
        raise ValueError("Same-day SPX EOD option chain has an empty 0DTE slice")

    try:
        spot = float(spot_price)
    except (TypeError, ValueError) as exc:
        raise ValueError("Same-day SPX EOD option chain has no valid spot price") from exc
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("Same-day SPX EOD option chain has no valid spot price")

    recomputed = gex_engine.compute(zero_dte, spot)
    rows = recomputed.get("gex_by_strike") or []
    if not rows:
        raise ValueError("0DTE GEX recomputation produced no strike rows")

    call_wall = recomputed.get("call_wall")
    put_wall = recomputed.get("put_wall")
    if call_wall is None or put_wall is None:
        raise ValueError("0DTE GEX recomputation produced no call or put wall")
    call_row = _row_at_strike(rows, call_wall)
    put_row = _row_at_strike(rows, put_wall)
    hot_row = min(rows, key=lambda row: (row.get("call_gex") or 0) + (row.get("put_gex") or 0))

    call_wall_gex = float(call_row.get("call_gex") or 0)
    put_wall_gex = float(put_row.get("put_gex") or 0)
    hot_zone_gex = float((hot_row.get("call_gex") or 0) + (hot_row.get("put_gex") or 0))
    if not all(math.isfinite(value) for value in (call_wall_gex, put_wall_gex, hot_zone_gex)):
        raise ValueError("0DTE wall or hot-zone exposure is not finite")
    if call_wall_gex <= 0:
        raise ValueError("0DTE call-wall exposure is not positive")
    if put_wall_gex >= 0:
        raise ValueError("0DTE put-wall exposure is not negative")
    if hot_zone_gex >= 0:
        raise ValueError("0DTE slice has no negative-GEX hot zone")

    gamma_flip = calculate_zero_gamma_flip(zero_dte, spot, snapshot_at, trade_day)
    net_gex = float(recomputed.get("net_gex") or 0)
    if not math.isfinite(net_gex):
        raise ValueError("0DTE net GEX is not finite")
    regime = "POSITIVE" if net_gex >= 0 else "NEGATIVE"
    dealer_position = "LONG" if net_gex >= 0 else "SHORT"
    expiry_at = datetime.combine(trade_day, time(15, 0), tzinfo=_CT)
    source_trade_date = trade_date_ct(trade_day)

    chart_snapshot = {
        **recomputed,
        "_id": source_snapshot_ref,
        "symbol": "SPX",
        "spot_price": spot,
        "trade_date": source_trade_date,
        "created_at": snapshot_at,
        "gamma_flip": gamma_flip,
    }
    analysis = {
        "source_snapshot_ref": source_snapshot_ref,
        "source_trade_date": source_trade_date,
        "snapshot_at": snapshot_at,
        "expiry_at": expiry_at,
        "spot_price": spot,
        "zero_dte_contract_count": len(zero_dte),
        "regime": regime,
        "dealer_position": dealer_position,
        "net_gex_raw": net_gex,
        "net_gex_b": net_gex / 1e9,
        "gamma_flip": gamma_flip,
        "call_wall": float(call_wall),
        "call_wall_gex_raw": call_wall_gex,
        "call_wall_gex_b": call_wall_gex / 1e9,
        "put_wall": float(put_wall),
        "put_wall_gex_raw": put_wall_gex,
        "put_wall_gex_b": put_wall_gex / 1e9,
        "hot_zone": float(hot_row["strike"]),
        "hot_zone_gex_raw": hot_zone_gex,
        "hot_zone_gex_b": hot_zone_gex / 1e9,
    }
    return chart_snapshot, analysis


def build_eod_analysis(
    db,
    *,
    trade_day: date | None = None,
    generated_at: datetime | None = None,
) -> tuple[dict, dict]:
    """Load and recompute a validated same-day SPX 0DTE EOD snapshot."""
    generated_at = generated_at or now_ct()
    generated_at = _aware_ct(generated_at, "generation time")
    trade_day = trade_day or generated_at.date()
    if not is_trading_day(trade_day):
        raise ValueError(f"{trade_day.isoformat()} is not an SPX trading day")

    source_trade_date = trade_date_ct(trade_day)
    source = gex_rolling_21d.find_by_symbol_date(db, "SPX", source_trade_date)
    if source is None:
        raise ValueError(f"No same-day SPX EOD option chain for {trade_day.isoformat()}")

    try:
        max_age = int(os.environ.get("SOCIAL_EOD_MAX_SNAPSHOT_AGE_MINUTES", "120"))
    except ValueError as exc:
        raise ValueError("SOCIAL_EOD_MAX_SNAPSHOT_AGE_MINUTES must be an integer") from exc
    if max_age <= 0:
        raise ValueError("SOCIAL_EOD_MAX_SNAPSHOT_AGE_MINUTES must be positive")
    snapshot_at = validate_snapshot_freshness(
        source, trade_day, generated_at, max_age_minutes=max_age
    )

    return analyze_eod_contracts(
        source.get("options_slice") or [],
        source.get("spot_price"),
        trade_day,
        snapshot_at,
        source_snapshot_ref=source.get("_id"),
    )
