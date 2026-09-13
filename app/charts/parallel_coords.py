"""
Parallel coordinates chart for EOD GEX analysis.

Ported from DailyIndexRangeFinder FavTickersGex.print_gex_parallel():
  - ±20% spot band
  - Top-5 feature axes via coefficient-of-variation scoring
  - Must-include strike, GEX, expiry (when available)
  - Lines coloured by option type (Call=blue, Put=red)
  - Option Type always appended as final filterable axis
"""
from __future__ import annotations

from datetime import date, datetime

import plotly.graph_objects as go

from app.charts.contracts import SPOT_BAND, filter_near_spot

_FEATURE_LABELS = {
    "strike": "Strike",
    "GEX": "GEX (Bn$)",
    "iv": "Impl. Vol",
    "volume": "Volume",
    "open_interest": "Open Int",
    "expiration_ord": "Expiry Date",
}

_CANDIDATE_FEATURES = ("strike", "GEX", "iv", "volume", "open_interest", "expiration_ord")
_MUST_HAVE = ("strike", "GEX", "expiration_ord")


def _coefficient_of_variation(values: list[float]) -> float:
    non_zero = [float(v) for v in values if v is not None and float(v) != 0.0]
    if len(non_zero) < 10:
        return 0.0
    mean = abs(sum(non_zero) / len(non_zero))
    if mean <= 0:
        return 0.0
    variance = sum((v - sum(non_zero) / len(non_zero)) ** 2 for v in non_zero) / len(non_zero)
    return (variance ** 0.5) / mean


def _encode_expiry_ordinal(contracts: list[dict]) -> tuple[list[dict], list[date]]:
    """Add expiration_ord to each contract; return sorted unique expiry dates."""
    expiries: list[date] = []
    seen: set[str] = set()
    for row in contracts:
        exp = row.get("expiry")
        if not exp:
            continue
        if isinstance(exp, str):
            exp_date = date.fromisoformat(exp[:10])
        elif isinstance(exp, datetime):
            exp_date = exp.date()
        elif isinstance(exp, date):
            exp_date = exp
        else:
            continue
        key = exp_date.isoformat()
        if key not in seen:
            seen.add(key)
            expiries.append(exp_date)

    expiries.sort()
    exp_to_ord = {exp: idx for idx, exp in enumerate(expiries)}

    enriched: list[dict] = []
    for row in contracts:
        copy = dict(row)
        exp = copy.get("expiry")
        if exp:
            if isinstance(exp, str):
                exp_date = date.fromisoformat(exp[:10])
            elif isinstance(exp, datetime):
                exp_date = exp.date()
            else:
                exp_date = exp
            copy["expiration_ord"] = exp_to_ord.get(exp_date)
        copy["type_ord"] = 0.0 if copy.get("type") == "C" else 1.0
        enriched.append(copy)
    return enriched, expiries


def _select_feature_axes(contracts: list[dict]) -> list[str]:
    usable: list[str] = []
    for feat in _CANDIDATE_FEATURES:
        if feat == "expiration_ord":
            if any(c.get("expiration_ord") is not None for c in contracts):
                usable.append(feat)
        elif all(feat in c for c in contracts):
            usable.append(feat)

    scored = sorted(
        usable,
        key=lambda f: _coefficient_of_variation(_column_values(contracts, f)),
        reverse=True,
    )
    must = [f for f in _MUST_HAVE if f in usable]
    others = [f for f in scored if f not in must]
    return (must + others)[: max(5, len(must))][:5]


def _column_values(
    contracts: list[dict], feature: str, *, gex_divisor: float = 1e9
) -> list[float]:
    vals = []
    for row in contracts:
        if feature == "GEX":
            vals.append(float(row.get("GEX", 0.0)) / gex_divisor)
        else:
            v = row.get(feature)
            vals.append(0.0 if v is None else float(v))
    return vals


def build_parallel_coords_figure(
    spot: float,
    contracts: list[dict],
    option_type: str | None = None,
    *,
    currency: str = "USD",
    display_unit: str = "billion",
) -> go.Figure | None:
    """
    Build a Plotly parallel-coordinates figure from normalised contract rows.

    Each contract dict should include: type, strike, GEX, and optionally
    expiry, iv, volume, open_interest, greeks.

    option_type: 'C', 'P', or None (all). When a single side is given the
    Option Type axis is omitted — it carries no information.
    """
    if not contracts:
        return None

    near = filter_near_spot(contracts, spot, band=SPOT_BAND)
    data = near if near else list(contracts)
    if not data:
        return None

    data, unique_expiries = _encode_expiry_ordinal(data)
    if len(data) < 2:
        return None

    features = _select_feature_axes(data)
    is_inr_crore = currency == "INR" and display_unit == "crore"
    gex_divisor = 1e7 if is_inr_crore else 1e9
    feature_labels = dict(_FEATURE_LABELS)
    feature_labels["GEX"] = "GEX (₹ Cr)" if is_inr_crore else "GEX (Bn$)"

    color_vals = [row.get("type_ord", 0.5) for row in data]
    colorscale = [[0.0, "#4C78A8"], [1.0, "#E45756"]]
    colorbar_title = "Type (C/P)"

    dimensions: list[dict] = []
    for feat in features:
        col = _column_values(data, feat, gex_divisor=gex_divisor)
        dim: dict = {
            "range": [min(col), max(col)] if col else [0, 1],
            "label": feature_labels.get(feat, feat),
            "values": col,
        }
        if feat == "expiration_ord" and unique_expiries:
            dim["tickvals"] = list(range(len(unique_expiries)))
            dim["ticktext"] = [exp.strftime("%m/%d") for exp in unique_expiries]
        dimensions.append(dim)

    # Omit the Option Type axis when all visible contracts are one side —
    # the axis has a single possible value and adds no information.
    single_side = option_type in ("C", "P")
    if not single_side and all("type_ord" in row for row in data):
        dimensions.append({
            "range": [0, 1],
            "label": "Option Type",
            "values": [row.get("type_ord", 0.5) for row in data],
            "tickvals": [0, 1],
            "ticktext": ["Call", "Put"],
        })

    feature_summary = ", ".join(feature_labels.get(f, f) for f in features)
    fig = go.Figure(data=go.Parcoords(
        line=dict(
            color=color_vals,
            colorscale=colorscale,
            showscale=True,
            colorbar=dict(title=colorbar_title, thickness=12, len=0.8),
            cmin=min(color_vals),
            cmax=max(color_vals),
        ),
        dimensions=dimensions,
    ))

    fig.update_layout(
        title=dict(
            text=f"Parallel Coordinates — Top Features: {feature_summary}",
            font=dict(size=13),
        ),
        template="plotly_dark",
        height=625,
        autosize=True,
        margin=dict(l=80, r=80, t=70, b=40),
        plot_bgcolor="#212946",
        paper_bgcolor="#212946",
    )
    return fig


def parallel_coords_chart_json(
    spot: float,
    contracts: list[dict],
    option_type: str | None = None,
    *,
    currency: str = "USD",
    display_unit: str = "billion",
) -> dict | None:
    """Return Plotly JSON for the parallel-coordinates chart, or None."""
    fig = build_parallel_coords_figure(
        spot,
        contracts,
        option_type=option_type,
        currency=currency,
        display_unit=display_unit,
    )
    return fig.to_plotly_json() if fig else None
