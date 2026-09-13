"""
GEX-by-strike chart renderer for Module 10 social posts.

Branding is fully config-driven — swap real values in .env without touching code:
  SOCIAL_BRAND_NAME        display name watermarked on chart  (default: "GEX Intelligence")
  SOCIAL_TWITTER_HANDLE    handle shown on chart              (default: "@gexintelligence")
  SOCIAL_CHART_COLOR_POS   bar color for positive GEX         (default: "#2196F3")
  SOCIAL_CHART_COLOR_NEG   bar color for negative GEX         (default: "#EF5350")
  SOCIAL_CHART_COLOR_SPOT  spot-price line color              (default: "#FFD700")
  SOCIAL_CHART_BG          background color                   (default: "#0D1117")
  SOCIAL_CHART_TEXT        axis / label text color            (default: "#E0E0E0")
  SOCIAL_CHART_DIR         output directory for PNG files     (default: "output/social_charts")

# TODO (open item): confirm brand name + handle + color scheme before public launch.
# These defaults are placeholder values only — they are NOT the real production values.
"""
import os
from pathlib import Path

import logging
log = logging.getLogger(__name__)


def _cfg(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _chart_dir() -> Path:
    return Path(_cfg("SOCIAL_CHART_DIR", "output/social_charts"))


def render_gex_chart(snapshot: dict, post_id: str) -> str:
    """
    Render a GEX-by-strike PNG for an EOD or EOW post.
    Returns the absolute file path of the saved PNG.
    Raises RuntimeError if matplotlib is not installed.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise RuntimeError(
            "matplotlib is required for chart rendering — "
            "add 'matplotlib' to requirements.txt and reinstall."
        )

    out_dir = _chart_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{post_id}.png"

    color_pos  = _cfg("SOCIAL_CHART_COLOR_POS",  "#2196F3")
    color_neg  = _cfg("SOCIAL_CHART_COLOR_NEG",  "#EF5350")
    color_spot = _cfg("SOCIAL_CHART_COLOR_SPOT", "#FFD700")
    color_bg   = _cfg("SOCIAL_CHART_BG",         "#0D1117")
    color_text = _cfg("SOCIAL_CHART_TEXT",        "#E0E0E0")
    brand_name = _cfg("SOCIAL_BRAND_NAME",        "GEX Intelligence")
    handle     = _cfg("SOCIAL_TWITTER_HANDLE",    "@gexintelligence")

    strikes_data = snapshot.get("gex_by_strike") or []
    spot      = snapshot.get("spot_price")
    net_gex   = snapshot.get("net_gex")
    call_wall = snapshot.get("call_wall")
    put_wall  = snapshot.get("put_wall")

    strikes  = [s["strike"] for s in strikes_data]
    gex_vals = [(s.get("call_gex") or 0) + (s.get("put_gex") or 0) for s in strikes_data]

    fig, (ax, ax_stat) = plt.subplots(
        2, 1,
        figsize=(10, 6.5),
        gridspec_kw={"height_ratios": [5, 1]},
        facecolor=color_bg,
    )
    ax.set_facecolor(color_bg)
    ax_stat.set_facecolor(color_bg)

    # ── Bar chart ─────────────────────────────────────────────────────────
    if strikes:
        span = max(strikes) - min(strikes)
        bar_w = (span / len(strikes)) * 0.8 if len(strikes) > 1 else 5
        colors = [color_pos if v >= 0 else color_neg for v in gex_vals]
        ax.bar(strikes, gex_vals, color=colors, width=bar_w)

    ax.axhline(0, color="#555555", linewidth=0.8)

    if spot is not None:
        ax.axvline(x=spot, color=color_spot, linewidth=2, linestyle="--",
                   label=f"Spot {spot:,.0f}")
        ax.legend(fontsize=9, facecolor=color_bg, labelcolor=color_text)

    ax.set_xlabel("Strike", color=color_text, fontsize=10)
    ax.set_ylabel("GEX ($B)", color=color_text, fontsize=10)
    ax.tick_params(colors=color_text)
    for spine in ax.spines.values():
        spine.set_color("#333333")

    # Title
    symbol = snapshot.get("symbol", "SPX")
    td = snapshot.get("trade_date")
    try:
        date_label = f" — {td.strftime('%b %d, %Y')}"
    except Exception:
        date_label = ""
    ax.set_title(
        f"${symbol} GEX by Strike{date_label}",
        color=color_text, fontsize=13, fontweight="bold", pad=10,
    )

    # ── Stat cards row ────────────────────────────────────────────────────
    ax_stat.set_xlim(0, 1)
    ax_stat.set_ylim(0, 1)
    ax_stat.axis("off")

    regime = "POSITIVE" if (net_gex or 0) >= 0 else "NEGATIVE"
    hot_zone = _compute_hot_zone(strikes_data)

    cards = [
        ("Put Wall",  f"{put_wall:,.0f}"    if put_wall  is not None else "N/A", color_neg),
        ("Hot Zone",  f"{hot_zone:,.0f}"    if hot_zone  is not None else "N/A", color_neg),
        ("Net GEX",   f"{net_gex:+,.2f}B"  if net_gex   is not None else "N/A",
         color_pos if (net_gex or 0) >= 0 else color_neg),
        ("Call Wall", f"{call_wall:,.0f}"   if call_wall is not None else "N/A", color_pos),
        ("Regime",    regime, color_pos if regime == "POSITIVE" else color_neg),
    ]
    n = len(cards)
    for i, (label, value, color) in enumerate(cards):
        x = (i + 0.5) / n
        ax_stat.text(x, 0.75, label, ha="center", va="center",
                     fontsize=8, color="#888888")
        ax_stat.text(x, 0.25, value, ha="center", va="center",
                     fontsize=9, fontweight="bold", color=color)

    # ── Branding watermark ────────────────────────────────────────────────
    fig.text(0.99, 0.005, f"{brand_name}  {handle}",
             ha="right", va="bottom", fontsize=7, color="#444444")

    plt.tight_layout(pad=0.5)
    fig.savefig(str(out_path), dpi=150, bbox_inches="tight", facecolor=color_bg)
    plt.close(fig)

    log.info("Chart saved: %s", out_path)
    return str(out_path)


def _compute_hot_zone(strikes_data: list[dict]) -> float | None:
    """Strike with the largest single negative net GEX."""
    if not strikes_data:
        return None
    worst = min(
        strikes_data,
        key=lambda s: (s.get("call_gex") or 0) + (s.get("put_gex") or 0),
    )
    val = (worst.get("call_gex") or 0) + (worst.get("put_gex") or 0)
    return worst["strike"] if val < 0 else None
