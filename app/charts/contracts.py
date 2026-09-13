"""
Option contract rows used by EOD parallel-coordinates charts.

Mirrors the per-contract option_data DataFrame in DailyIndexRangeFinder's
FavTickersGex.print_gex_parallel(), normalised to RetailGex snapshot shapes.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

# ±20% of spot — same band as DailyIndexRangeFinder FavTickersGex.print_gex_parallel
SPOT_BAND = 0.20

_FIELD_ALIASES = {
    "open_interest": ("open_interest", "oi"),
    "volume": ("volume", "vol"),
}


def _as_date(value) -> date | None:
    if value is None:
        return None
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


def _pick(row: dict, *keys: str, default=0):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def normalize_contract(row: dict) -> dict:
    """Normalise one contract dict to the parallel-coords input shape."""
    expiry = _as_date(row.get("expiry") or row.get("expiration"))
    opt_type = str(row.get("type", "")).upper()
    if opt_type not in ("C", "P"):
        opt_type = "C"

    return {
        "type": opt_type,
        "strike": float(row.get("strike", 0.0)),
        "expiry": expiry.isoformat() if expiry else None,
        "GEX": float(row.get("gex", row.get("GEX", 0.0))),
        "gamma": float(_pick(row, "gamma", default=0.0)),
        "delta": float(_pick(row, "delta", default=0.0)),
        "theta": float(_pick(row, "theta", default=0.0)),
        "vega": float(_pick(row, "vega", default=0.0)),
        "iv": float(_pick(row, "iv", default=0.0)),
        "open_interest": int(_pick(row, "open_interest", "oi", default=0)),
        "volume": int(_pick(row, "volume", "vol", default=0)),
    }


def filter_near_spot(contracts: list[dict], spot: float, band: float = SPOT_BAND) -> list[dict]:
    if not contracts or spot <= 0:
        return contracts
    lo, hi = spot * (1.0 - band), spot * (1.0 + band)
    return [c for c in contracts if lo <= float(c["strike"]) <= hi]


def from_options_slice(rows: list[dict]) -> list[dict]:
    return [normalize_contract(r) for r in rows if r]


def from_gex_by_strike(rows: list[dict]) -> list[dict]:
    """Rebuild pseudo-contract rows from aggregated strike data (legacy snapshots)."""
    contracts: list[dict] = []
    for row in rows:
        strike = float(row.get("strike", 0.0))
        for opt_type, gex_key, greeks_key in (
            ("C", "call_gex", "call_greeks"),
            ("P", "put_gex", "put_greeks"),
        ):
            gex = float(row.get(gex_key, 0.0))
            greeks = row.get(greeks_key) or {}
            oi = int(greeks.get("open_interest", 0) or 0)
            if gex == 0.0 and oi == 0:
                continue
            contracts.append(normalize_contract({
                "type": opt_type,
                "strike": strike,
                "expiry": None,
                "gex": gex,
                "gamma": greeks.get("gamma", 0.0),
                "delta": greeks.get("delta", 0.0),
                "theta": greeks.get("theta", 0.0),
                "vega": greeks.get("vega", 0.0),
                "iv": greeks.get("iv", 0.0),
                "open_interest": oi,
                "volume": 0,
            }))
    return contracts


def contracts_for_snapshot(snapshot: dict) -> list[dict]:
    """Return contract rows for parallel coords from a GEX snapshot document."""
    spot = float(snapshot.get("spot_price") or 0)
    options_slice = snapshot.get("options_slice") or []
    if options_slice:
        contracts = from_options_slice(options_slice)
    else:
        contracts = from_gex_by_strike(snapshot.get("gex_by_strike") or [])

    filtered = filter_near_spot(contracts, spot)
    return filtered if filtered else contracts


def contracts_for_multi_expiry_docs(docs: list[dict]) -> list[dict]:
    """Aggregate per-contract rows from multiple snapshot docs (one per expiry).

    For each doc, expiry is taken from its options_slice rows when present, or
    backfilled from the doc's own expiry_date field (gex_by_strike fallback).
    """
    all_contracts: list[dict] = []
    for doc in docs:
        doc_expiry = doc.get("expiry_date")
        if hasattr(doc_expiry, "date"):
            doc_expiry = doc_expiry.date().isoformat()
        elif doc_expiry is not None:
            doc_expiry = str(doc_expiry)[:10]

        spot = float(doc.get("spot_price") or 0)
        options_slice = doc.get("options_slice") or []
        if options_slice:
            rows = from_options_slice(options_slice)
        else:
            rows = from_gex_by_strike(doc.get("gex_by_strike") or [])
            # gex_by_strike loses expiry info — backfill from doc
            if doc_expiry:
                for row in rows:
                    if row.get("expiry") is None:
                        row["expiry"] = doc_expiry

        near = filter_near_spot(rows, spot)
        all_contracts.extend(near if near else rows)
    return all_contracts


def gex_by_expiration_from_contracts(contracts: list[dict]) -> list[dict]:
    """Aggregate net GEX by expiration from per-contract rows."""
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"C": 0.0, "P": 0.0})
    for row in contracts:
        expiry = row.get("expiry")
        if not expiry:
            continue
        totals[str(expiry)[:10]][row["type"]] += float(row.get("GEX", 0.0))

    return [
        {
            "expiry": expiry,
            "call_gex": totals[expiry]["C"],
            "put_gex": totals[expiry]["P"],
            "net_gex": totals[expiry]["C"] + totals[expiry]["P"],
        }
        for expiry in sorted(totals)
    ]


def gex_by_expiry_strike_from_contracts(contracts: list[dict]) -> list[dict]:
    """Aggregate net GEX by (expiration, strike) from per-contract rows."""
    buckets: dict[tuple[str, float], dict[str, float]] = defaultdict(
        lambda: {"C": 0.0, "P": 0.0}
    )
    for row in contracts:
        expiry = row.get("expiry")
        if not expiry:
            continue
        key = (str(expiry)[:10], float(row["strike"]))
        buckets[key][row["type"]] += float(row.get("GEX", 0.0))

    return [
        {
            "expiry": expiry,
            "strike": strike,
            "gex": buckets[(expiry, strike)]["C"] + buckets[(expiry, strike)]["P"],
        }
        for expiry, strike in sorted(buckets)
    ]


def _weekly_expiration_fallback(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """Single-expiry fallback for legacy weekly snapshots (strike data only)."""
    gex_by_strike = snapshot.get("gex_by_strike") or []
    if not gex_by_strike:
        return [], []

    expiry = _as_date(snapshot.get("expiry_date") or snapshot.get("week_of"))
    if expiry is None:
        return [], []

    expiry_str = expiry.isoformat()
    call_gex = sum(float(r.get("call_gex", 0.0)) for r in gex_by_strike)
    put_gex = sum(float(r.get("put_gex", 0.0)) for r in gex_by_strike)
    gex_by_expiration = [{
        "expiry": expiry_str,
        "call_gex": call_gex,
        "put_gex": put_gex,
        "net_gex": call_gex + put_gex,
    }]
    gex_by_expiry_strike = [
        {
            "expiry": expiry_str,
            "strike": float(r["strike"]),
            "gex": float(r.get("call_gex", 0.0)) + float(r.get("put_gex", 0.0)),
        }
        for r in gex_by_strike
    ]
    return gex_by_expiration, gex_by_expiry_strike


def expiry_detail_for_snapshot(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """Resolve expiration-level GEX rows from stored or derived snapshot fields."""
    by_exp = snapshot.get("gex_by_expiration") or []
    by_es = snapshot.get("gex_by_expiry_strike") or []
    if by_exp:
        return by_exp, by_es

    options_slice = snapshot.get("options_slice") or []
    if options_slice:
        contracts = from_options_slice(options_slice)
        by_exp = gex_by_expiration_from_contracts(contracts)
        by_es = gex_by_expiry_strike_from_contracts(contracts)
        if by_exp:
            return by_exp, by_es

    return _weekly_expiration_fallback(snapshot)
