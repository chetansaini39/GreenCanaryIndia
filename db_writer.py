"""
DB writer — normalises GEX engine output into Mongo document shapes
and performs idempotent upserts into the correct collection.

Every write is an upsert keyed on the natural uniqueness constraint for
that collection, so re-running a job for the same symbol/period is safe.

All functions accept a PyMongo Database object so they work both inside
Flask (pass mongo.db) and in the standalone scheduler (pass a raw client db).
"""
from datetime import datetime

from pymongo import ASCENDING
from app.utils.time import now_ct

# Reuse the model modules for collection names and index guarantees
from app.models import (
    gex_intraday,
    gex_weekly,
    gex_monthly_opex,
    gex_rolling_21d,
)


# ── Intraday / 0DTE ───────────────────────────────────────────────────────

def write_intraday(
    db,
    *,
    symbol: str,
    asset_type: str,
    timestamp: datetime,
    trade_date: datetime,
    snapshot_type: str,
    source: str,
    spot_price: float,
    gex_result: dict,
    term_structure: list | None = None,
    market_metadata: dict | None = None,
) -> None:
    """Upsert a 5-min or 0DTE snapshot into gex_intraday.

    Keyed on (symbol, snapshot_type, timestamp) so a job re-run replaces
    the previous document rather than duplicating it.
    term_structure is index-only; omit (None) for stock symbols.
    """
    doc = _base_gex_doc(symbol, asset_type, source, spot_price, gex_result)
    doc.update({
        "timestamp": timestamp,
        "trade_date": trade_date,
        "snapshot_type": snapshot_type,
    })
    if term_structure is not None:
        doc["term_structure"] = term_structure
    doc.update(_market_metadata(market_metadata))
    key = {"symbol": symbol, "snapshot_type": snapshot_type, "timestamp": timestamp}
    db[gex_intraday.COLLECTION].replace_one(key, doc, upsert=True)


# ── Weekly ─────────────────────────────────────────────────────────────────

def write_weekly(
    db,
    *,
    symbol: str,
    asset_type: str,
    week_of: datetime,
    expiry_date: datetime,
    trade_date: datetime,
    source: str,
    spot_price: float,
    gex_result: dict,
    market_metadata: dict | None = None,
) -> None:
    """Upsert a weekly snapshot for (symbol, week_of, trade_date).

    Runs every trading day so the daily evolution of each expiry week is captured.
    """
    doc = _base_gex_doc(symbol, asset_type, source, spot_price, gex_result)
    doc.update({
        "week_of": week_of,
        "expiry_date": expiry_date,
        "trade_date": trade_date,
        "snapshot_type": "weekly",
    })
    doc.update(_market_metadata(market_metadata))
    gex_weekly.upsert(db, doc)


# ── Monthly OPEX ───────────────────────────────────────────────────────────

def write_monthly_opex(
    db,
    *,
    symbol: str,
    asset_type: str,
    expiry_date: datetime,
    trade_date: datetime,
    source: str,
    spot_price: float,
    gex_result: dict,
    market_metadata: dict | None = None,
) -> None:
    """Upsert a monthly OPEX snapshot. Keyed on (symbol, expiry_date, trade_date)."""
    doc = _base_gex_doc(symbol, asset_type, source, spot_price, gex_result)
    doc.update({
        "month": expiry_date.strftime("%Y-%m"),
        "expiry_date": expiry_date,
        "trade_date": trade_date,
        "snapshot_type": "monthly_opex",
    })
    doc.update(_market_metadata(market_metadata))
    gex_monthly_opex.upsert(db, doc)


# ── Rolling 21-day EOD ─────────────────────────────────────────────────────

def write_rolling_21d(
    db,
    *,
    symbol: str,
    asset_type: str,
    trade_date: datetime,
    source: str,
    spot_price: float,
    gex_result: dict,
    market_metadata: dict | None = None,
) -> None:
    """Upsert an EOD snapshot. Keyed on (symbol, trade_date).

    Full history is retained permanently; the 'rolling 21d' view is
    computed at query time by slicing the last 21 trade_dates.
    """
    doc = {
        "symbol": symbol,
        "asset_type": asset_type,
        "trade_date": trade_date,
        "snapshot_type": "rolling_21d_eod",
        "source": source,
        "spot_price": spot_price,
        "created_at": now_ct(),
    }
    doc.update(_gex_detail_fields(gex_result))
    doc.update(_market_metadata(market_metadata))
    gex_rolling_21d.upsert(db, doc)


# ── Internal helpers ───────────────────────────────────────────────────────

def _gex_detail_fields(gex_result: dict) -> dict:
    """Strike, expiry, and per-contract fields used by EOD charts."""
    return {
        "net_gex": gex_result["net_gex"],
        "call_wall": gex_result["call_wall"],
        "put_wall": gex_result["put_wall"],
        "gex_by_strike": gex_result.get("gex_by_strike", []),
        "gex_by_expiration": gex_result.get("gex_by_expiration", []),
        "gex_by_expiry_strike": gex_result.get("gex_by_expiry_strike", []),
        "options_slice": gex_result.get("options_slice", []),
    }


def _base_gex_doc(
    symbol: str,
    asset_type: str,
    source: str,
    spot_price: float,
    gex_result: dict,
) -> dict:
    """Common fields shared by all GEX collection documents."""
    doc = {
        "symbol": symbol,
        "asset_type": asset_type,
        "source": source,
        "spot_price": spot_price,
        "created_at": now_ct(),
    }
    doc.update(_gex_detail_fields(gex_result))
    return doc


def _market_metadata(value: dict | None) -> dict:
    allowed = {
        "provider", "market", "market_timezone", "currency", "display_unit",
        "pricing_model", "futures_price", "risk_free_rate", "data_quality",
    }
    return {key: value[key] for key in allowed if value and key in value}
