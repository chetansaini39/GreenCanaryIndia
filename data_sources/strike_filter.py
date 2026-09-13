"""
Strike-range filtering for GEX pipeline storage.

Index symbols (asset_type=index): ±20% of spot.
Stock symbols (asset_type=stock): ±50% of spot.

Bounds are inclusive: spot × (1 − band) ≤ strike ≤ spot × (1 + band).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

INDEX_STRIKE_BAND = 0.20
STOCK_STRIKE_BAND = 0.50


def strike_band(asset_type: str) -> float:
    """Return the ± fraction around spot for *asset_type*."""
    return INDEX_STRIKE_BAND if asset_type == "index" else STOCK_STRIKE_BAND


def strike_bounds(spot_price: float, asset_type: str) -> tuple[float, float]:
    band = strike_band(asset_type)
    return spot_price * (1.0 - band), spot_price * (1.0 + band)


def filter_options(
    options: list[dict],
    spot_price: float,
    asset_type: str,
) -> list[dict]:
    """Keep option contracts whose strike falls within the asset-type band."""
    if not options or spot_price <= 0:
        return options

    lo, hi = strike_bounds(spot_price, asset_type)
    kept = [
        opt for opt in options
        if lo <= float(opt.get("strike", 0.0)) <= hi
    ]
    dropped = len(options) - len(kept)
    if dropped:
        log.debug(
            "Strike filter (%s ±%.0f%%): %d → %d contracts",
            asset_type,
            strike_band(asset_type) * 100,
            len(options),
            len(kept),
        )
    return kept


def filter_chain(chain: dict, asset_type: str) -> dict:
    """Return a chain dict with options filtered to the spot band."""
    spot = float(chain.get("spot_price") or 0.0)
    options = chain.get("options") or []
    filtered = filter_options(options, spot, asset_type)
    return {**chain, "options": filtered, "spot_price": spot}


def _filter_strike_rows(rows: list[dict], lo: float, hi: float) -> list[dict]:
    return [
        row for row in rows
        if lo <= float(row.get("strike", 0.0)) <= hi
    ]


def trim_gex_result(gex_result: dict, spot_price: float, asset_type: str) -> dict:
    """Trim stored GEX arrays and recompute summary fields (migration helper)."""
    if spot_price <= 0:
        return gex_result

    lo, hi = strike_bounds(spot_price, asset_type)
    gex_by_strike = _filter_strike_rows(gex_result.get("gex_by_strike") or [], lo, hi)
    gex_by_expiry_strike = _filter_strike_rows(
        gex_result.get("gex_by_expiry_strike") or [], lo, hi
    )
    options_slice = [
        row for row in (gex_result.get("options_slice") or [])
        if lo <= float(row.get("strike", 0.0)) <= hi
    ]

    if options_slice:
        from app.charts.contracts import (
            from_options_slice,
            gex_by_expiration_from_contracts,
            gex_by_expiry_strike_from_contracts,
        )

        contracts = from_options_slice(options_slice)
        gex_by_expiration = gex_by_expiration_from_contracts(contracts)
        if gex_by_expiry_strike:
            pass  # keep strike-filtered stored rows
        else:
            gex_by_expiry_strike = gex_by_expiry_strike_from_contracts(contracts)
    elif gex_by_expiry_strike:
        expiry_totals: dict[str, dict[str, float]] = {}
        for row in gex_by_expiry_strike:
            expiry = str(row.get("expiry", ""))[:10]
            net = float(row.get("gex", 0.0))
            bucket = expiry_totals.setdefault(expiry, {"call_gex": 0.0, "put_gex": 0.0})
            if net >= 0:
                bucket["call_gex"] += net
            else:
                bucket["put_gex"] += net
        gex_by_expiration = [
            {
                "expiry": expiry,
                "call_gex": totals["call_gex"],
                "put_gex": totals["put_gex"],
                "net_gex": totals["call_gex"] + totals["put_gex"],
            }
            for expiry, totals in sorted(expiry_totals.items())
        ]
    else:
        gex_by_expiration = gex_result.get("gex_by_expiration") or []

    net_gex = sum(
        float(r.get("call_gex", 0.0)) + float(r.get("put_gex", 0.0))
        for r in gex_by_strike
    )
    call_wall = (
        max(gex_by_strike, key=lambda r: float(r.get("call_gex", 0.0)))["strike"]
        if gex_by_strike else None
    )
    put_wall = (
        min(gex_by_strike, key=lambda r: float(r.get("put_gex", 0.0)))["strike"]
        if gex_by_strike else None
    )

    return {
        **gex_result,
        "net_gex": net_gex,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gex_by_strike": gex_by_strike,
        "gex_by_expiration": gex_by_expiration,
        "gex_by_expiry_strike": gex_by_expiry_strike,
        "options_slice": options_slice,
    }
