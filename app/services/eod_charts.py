"""Plotly chart builders for the EOD GEX Analysis dashboard card."""
from __future__ import annotations

from datetime import date, datetime

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def _scaled(value: float, currency: str = "USD", display_unit: str = "billion") -> float:
    return value / (1e7 if currency == "INR" and display_unit == "crore" else 1e9)


def _unit_labels(currency: str, display_unit: str) -> tuple[str, str]:
    if currency == "INR" and display_unit == "crore":
        return "₹ Cr", "₹%{y:.3f} Cr"
    return "Bn$", "$%{y:.3f}B"


# Shared height for the side-by-side EOD expiration + surface charts.
_EOD_PAIR_CHART_HEIGHT = 420


def gex_over_time_chart(days: list[dict]) -> dict | None:
    """Bar chart of total net GEX in the symbol's configured display unit."""
    currency = days[0].get("currency", "USD") if days else "USD"
    display_unit = days[0].get("display_unit", "billion") if days else "billion"
    unit_label, _hover_label = _unit_labels(currency, display_unit)
    points = []
    for day in days:
        trade_date = day.get("trade_date")
        net = day.get("net_gex")
        if trade_date is None or net is None:
            continue
        if isinstance(trade_date, str):
            dt = datetime.fromisoformat(trade_date[:10])
        elif isinstance(trade_date, datetime):
            dt = trade_date
        else:
            dt = datetime.combine(trade_date, datetime.min.time())
        points.append((dt, _scaled(net, currency, display_unit)))

    if not points:
        return None

    points.sort(key=lambda x: x[0])
    colors = ["rgb(0, 255, 0)" if v >= 0 else "rgb(255, 0, 0)" for _, v in points]
    fig = go.Figure(go.Bar(
        x=[p[0] for p in points],
        y=[p[1] for p in points],
        marker_color=colors,
        name="GEX",
    ))
    fig.update_layout(
        title="GEX Values Over Time",
        xaxis_title="Date",
        yaxis_title=f"Notional GEX ({unit_label})",
        template="plotly_dark",
        hovermode="x unified",
        height=380,
        autosize=True,
        margin=dict(l=50, r=20, t=50, b=40),
    )
    return fig.to_plotly_json()


def gex_by_strike_chart(
    symbol: str,
    spot: float,
    gex_by_strike: list[dict],
    *,
    currency: str = "USD",
    display_unit: str = "billion",
) -> dict | None:
    if not gex_by_strike:
        return None

    limit = 0.50
    rows = [
        r for r in gex_by_strike
        if spot * (1 - limit) < float(r["strike"]) < spot * (1 + limit)
    ]
    if not rows:
        rows = gex_by_strike

    strikes = [float(r["strike"]) for r in rows]
    unit_label, hover_label = _unit_labels(currency, display_unit)
    net_gex = [
        _scaled(
            float(r.get("call_gex", 0)) + float(r.get("put_gex", 0)),
            currency,
            display_unit,
        )
        for r in rows
    ]
    oi_vals = [
        int(r.get("call_greeks", {}).get("open_interest", 0))
        + int(r.get("put_greeks", {}).get("open_interest", 0))
        for r in rows
    ]

    title_text = f"{symbol} @ {spot:.2f} GEX by Strike"
    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=("", ""),
        vertical_spacing=0.12,
        row_heights=[0.6, 0.4],
    )
    fig.add_trace(go.Bar(
        x=strikes, y=net_gex, marker_color="#74EF33", opacity=0.9, name="GEX",
        hovertemplate=f"Strike: %{{x}}<br>GEX: {hover_label}<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=strikes, y=oi_vals, marker_color="#FF6B6B", opacity=0.9, name="Open Interest",
        hovertemplate="Strike: %{x}<br>OI: %{y:,}<extra></extra>",
    ), row=2, col=1)
    fig.add_vline(
        x=spot, line_dash="dash", line_color="#FE53BB", line_width=2,
        annotation_text=f"Spot: {spot:.2f}",
        annotation_position="top",
        annotation_font_color="#FE53BB",
        annotation_font_size=12,
        row=1, col=1,
    )
    fig.update_layout(
        title=dict(text=title_text, x=0, xanchor="left", y=0.98, yanchor="top"),
        template="plotly_dark",
        height=520,
        autosize=True,
        showlegend=True,
        margin=dict(l=50, r=20, t=80, b=40),
    )
    fig.update_xaxes(title_text="Strike", row=1, col=1)
    fig.update_xaxes(title_text="Strike", row=2, col=1)
    fig.update_yaxes(title_text=f"Gamma Exposure ({unit_label})", row=1, col=1)
    fig.update_yaxes(title_text="Open Interest", row=2, col=1)
    return fig.to_plotly_json()


def gex_by_expiration_chart(
    symbol: str,
    gex_by_expiration: list[dict],
    *,
    currency: str = "USD",
    display_unit: str = "billion",
) -> dict | None:
    if not gex_by_expiration:
        return None

    unit_label, hover_label = _unit_labels(currency, display_unit)
    expiries = []
    values = []
    for row in gex_by_expiration:
        expiry = row.get("expiry")
        if isinstance(expiry, str):
            expiries.append(expiry[:10])
        elif isinstance(expiry, (date, datetime)):
            expiries.append(expiry.isoformat()[:10])
        else:
            continue
        values.append(_scaled(float(row.get("net_gex", 0)), currency, display_unit))

    if not expiries:
        return None

    fig = go.Figure(go.Bar(
        x=expiries, y=values, marker_color="#FE53BB", opacity=0.6,
        hovertemplate=f"Expiry: %{{x}}<br>GEX: {hover_label}<extra></extra>",
    ))
    fig.update_layout(
        title=f"{symbol} GEX by Expiration",
        template="plotly_dark",
        height=_EOD_PAIR_CHART_HEIGHT,
        autosize=True,
        xaxis_title="Expiration date",
        yaxis_title=f"Gamma Exposure ({unit_label})",
        xaxis_tickangle=-45,
        margin=dict(l=50, r=20, t=50, b=80),
    )
    return fig.to_plotly_json()


def gex_surface_3d_chart(
    spot: float,
    gex_by_expiry_strike: list[dict],
    *,
    currency: str = "USD",
    display_unit: str = "billion",
) -> dict | None:
    if not gex_by_expiry_strike:
        return None

    filtered = [
        r for r in gex_by_expiry_strike
        if spot * 0.85 < float(r["strike"]) < spot * 1.15
    ]
    if not filtered:
        filtered = gex_by_expiry_strike

    strikes = sorted({float(r["strike"]) for r in filtered})
    expiries = sorted({r["expiry"][:10] if isinstance(r["expiry"], str) else str(r["expiry"])[:10]
                       for r in filtered})

    if len(strikes) < 2 or len(expiries) < 2:
        return None

    is_inr_crore = currency == "INR" and display_unit == "crore"
    unit_label = "₹ Cr" if is_inr_crore else "M$"
    scale = 1e7 if is_inr_crore else 1e6
    lookup = {}
    for r in filtered:
        exp = r["expiry"][:10] if isinstance(r["expiry"], str) else str(r["expiry"])[:10]
        lookup[(exp, float(r["strike"]))] = float(r["gex"]) / scale

    z = []
    for exp in expiries:
        z.append([lookup.get((exp, s), 0.0) for s in strikes])

    fig = go.Figure(data=[go.Surface(x=strikes, y=expiries, z=z, colorscale="RdBu", reversescale=True)])
    fig.update_layout(
        title="GEX Surface",
        template="plotly_dark",
        height=_EOD_PAIR_CHART_HEIGHT,
        autosize=True,
        scene=dict(
            xaxis_title="Strike Price",
            yaxis_title="Expiration Date",
            zaxis_title=f"Gamma ({unit_label} / 1% move)",
        ),
        margin=dict(l=0, r=0, t=50, b=0),
    )
    return fig.to_plotly_json()


def parallel_coords_chart(spot: float, options_slice: list[dict]) -> dict | None:
    """Backward-compatible wrapper — prefer contracts_for_snapshot + parallel_coords_chart_json."""
    from app.charts.contracts import from_options_slice

    contracts = from_options_slice(options_slice)
    return parallel_coords_chart_json(spot, contracts)
