"""Deterministic five-image content renderer for review-required EOD X threads."""
from __future__ import annotations

import os
import shutil
import struct
import textwrap
from datetime import datetime
from pathlib import Path
from statistics import median

from app.services.eod_social_theme import default_eod_theme, validate_eod_theme
from app.services.social_chart import _chart_dir


IMAGE_WIDTH = 1200
IMAGE_HEIGHT = 675
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MEDIA_VERSION = 4
MEDIA_KINDS = ("chart", "regime", "levels", "playbook", "verdict")

_FONT_DIR = Path(__file__).resolve().parents[1] / "static" / "fonts" / "ibm-plex"
_FONT_FILES = {
    "sans_regular": _FONT_DIR / "IBMPlexSans-Regular.ttf",
    "sans_semibold": _FONT_DIR / "IBMPlexSans-SemiBold.ttf",
    "mono_regular": _FONT_DIR / "IBMPlexMono-Regular.ttf",
    "mono_semibold": _FONT_DIR / "IBMPlexMono-SemiBold.ttf",
}
SANS_FAMILY = "IBM Plex Sans"
MONO_FAMILY = "IBM Plex Mono"


def _register_fonts() -> None:
    """Register the bundled fonts so rendering is identical on every host."""
    from matplotlib import font_manager

    missing = [path.name for path in _FONT_FILES.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"Bundled EOD font assets are missing: {', '.join(missing)}")
    for path in _FONT_FILES.values():
        font_manager.fontManager.addfont(str(path))


def _font_kwargs(*, mono: bool = False, semibold: bool = False) -> dict:
    return {
        "fontfamily": MONO_FAMILY if mono else SANS_FAMILY,
        "fontweight": "semibold" if semibold else "regular",
    }


def _level(value) -> str:
    return f"{float(value):,.0f}"


def _billions(value, *, signed: bool = False) -> str:
    number = float(value)
    if signed:
        return f"{'+' if number >= 0 else '-'}${abs(number):,.2f}B"
    return f"{'+' if number >= 0 else '-'}${abs(number):,.2f}B"


def _wrap(value: str, width: int, max_lines: int, label: str) -> str:
    lines: list[str] = []
    for paragraph in str(value).splitlines() or [""]:
        wrapped = textwrap.wrap(
            paragraph,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
        lines.extend(wrapped)
    if len(lines) > max_lines:
        raise ValueError(f"EOD media overflow in {label}: {len(lines)} lines (maximum {max_lines})")
    return "\n".join(lines)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"Generated EOD media is not a valid PNG: {path.name}")
    return struct.unpack(">II", header[16:24])


def _safe_media_path(path_value: str | os.PathLike) -> Path | None:
    root = _chart_dir().resolve()
    path = Path(path_value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def cleanup_eod_media(post_or_assets) -> None:
    """Remove generated EOD files, but only from SOCIAL_CHART_DIR."""
    if isinstance(post_or_assets, dict):
        assets = post_or_assets.get("media_assets") or []
        candidate_paths = [asset.get("path") for asset in assets]
        if not candidate_paths and post_or_assets.get("chart_path"):
            candidate_paths = [post_or_assets["chart_path"]]
    else:
        candidate_paths = [asset.get("path") for asset in (post_or_assets or [])]

    safe_paths = [_safe_media_path(value) for value in candidate_paths if value]
    safe_paths = [path for path in safe_paths if path is not None]
    parents = {path.parent for path in safe_paths}
    for path in safe_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    root = _chart_dir().resolve()
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        if parent != root and parent.parent == root:
            shutil.rmtree(parent, ignore_errors=True)


def validate_eod_media_assets(media_assets: list[dict] | None) -> dict:
    if not isinstance(media_assets, list) or len(media_assets) != 5:
        raise ValueError(
            f"EOD media version {MEDIA_VERSION} requires exactly five image assets"
        )

    indexes: list[int] = []
    for expected_index, asset in enumerate(media_assets):
        if not isinstance(asset, dict):
            raise ValueError(f"EOD media asset {expected_index + 1} is invalid")
        if asset.get("post_index") != expected_index:
            raise ValueError("EOD media assets must map in exact post order 0 through 4")
        if asset.get("kind") != MEDIA_KINDS[expected_index]:
            raise ValueError(
                f"EOD media asset {expected_index + 1} must be kind "
                f"{MEDIA_KINDS[expected_index]!r}"
            )
        path_value = asset.get("path")
        path = Path(path_value) if path_value else None
        if path is None or not path.is_absolute():
            raise ValueError(f"EOD media asset {expected_index + 1} requires an absolute path")
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"EOD media asset {expected_index + 1} is missing or empty")
        actual_size = path.stat().st_size
        if actual_size > MAX_IMAGE_BYTES:
            raise ValueError(f"EOD media asset {expected_index + 1} exceeds X's 5 MB limit")
        width, height = _png_dimensions(path)
        if (width, height) != (IMAGE_WIDTH, IMAGE_HEIGHT):
            raise ValueError(
                f"EOD media asset {expected_index + 1} must be "
                f"{IMAGE_WIDTH}x{IMAGE_HEIGHT}"
            )
        if asset.get("width") != width or asset.get("height") != height:
            raise ValueError(f"EOD media asset {expected_index + 1} dimensions are inconsistent")
        if asset.get("size_bytes") != actual_size:
            raise ValueError(f"EOD media asset {expected_index + 1} file size is inconsistent")
        alt_text = asset.get("alt_text")
        if not isinstance(alt_text, str) or not alt_text.strip():
            raise ValueError(f"EOD media asset {expected_index + 1} requires alt text")
        if len(alt_text) > 1000:
            raise ValueError(f"EOD media asset {expected_index + 1} alt text exceeds 1,000 characters")
        indexes.append(expected_index)

    return {"ok": True, "media_version": MEDIA_VERSION, "media_post_indexes": indexes}


def _blend(foreground: str, background: str, weight: float) -> str:
    def channels(value):
        return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))

    front = channels(foreground)
    back = channels(background)
    mixed = tuple(round(front[index] * weight + back[index] * (1 - weight)) for index in range(3))
    return "#" + "".join(f"{value:02X}" for value in mixed)


def _colors(theme: dict) -> dict:
    background = theme["background_color"]
    return {
        "bg": background,
        "panel": theme["surface_color"],
        "text": theme["text_color"],
        "muted": theme["muted_color"],
        "green": theme["positive_color"],
        "red": theme["negative_color"],
        "amber": theme["amber_color"],
        "blue": theme["accent_color"],
        "gold": theme["spot_color"],
        "line": _blend(theme["text_color"], background, 0.18),
        "green_tint": _blend(theme["positive_color"], background, 0.13),
        "red_tint": _blend(theme["negative_color"], background, 0.13),
        "amber_tint": _blend(theme["amber_color"], background, 0.13),
        "blue_tint": _blend(theme["accent_color"], background, 0.13),
    }


def _card(ax, xy, width, height, *, face, edge=None, linewidth=1.0, radius=0.008):
    from matplotlib.patches import FancyBboxPatch

    patch = FancyBboxPatch(
        xy, width, height,
        boxstyle=f"round,pad=0.008,rounding_size={radius}",
        transform=ax.transAxes,
        facecolor=face,
        edgecolor=edge or face,
        linewidth=linewidth,
    )
    ax.add_patch(patch)
    return patch


def _pill(ax, x, y, width, text, *, face, color):
    _card(ax, (x, y), width, 0.048, face=face, edge=face, radius=0.018)
    ax.text(
        x + width / 2, y + 0.024, text, transform=ax.transAxes,
        ha="center", va="center", color=color, fontsize=9.5,
        **_font_kwargs(semibold=True),
    )


def _section_title(ax, title: str, subtitle: str, colors: dict):
    ax.text(
        0.055, 0.955, "SPX  •  0DTE GAMMA  •  EOD", transform=ax.transAxes,
        color=colors["blue"], fontsize=8.5, va="top", **_font_kwargs(semibold=True),
    )
    ax.text(
        0.055, 0.905, title, transform=ax.transAxes, color=colors["text"],
        fontsize=24, va="top", **_font_kwargs(semibold=True),
    )
    ax.text(
        0.055, 0.848, subtitle, transform=ax.transAxes, color=colors["muted"],
        fontsize=11.5, va="top", **_font_kwargs(),
    )
    ax.plot(
        [0.055, 0.945], [0.805, 0.805], transform=ax.transAxes,
        color=colors["line"], linewidth=0.9,
    )


def _add_brand_footer(ax, *, theme: dict, analysis: dict, colors: dict) -> None:
    from matplotlib.patches import FancyBboxPatch

    ax.plot(
        [0.055, 0.945], [0.112, 0.112], transform=ax.transAxes,
        color=colors["line"], linewidth=0.9,
    )
    logo_path = theme.get("logo_path")
    if logo_path:
        import matplotlib.image as mpimg
        image = mpimg.imread(logo_path)
        ax.imshow(
            image, extent=(0.055, 0.085, 0.042, 0.092), transform=ax.transAxes,
            aspect="auto", zorder=5,
        )
    else:
        ax.add_patch(FancyBboxPatch(
            (0.055, 0.042), 0.036, 0.05,
            boxstyle="round,pad=0.002,rounding_size=0.005",
            transform=ax.transAxes,
            facecolor=colors["blue"], edgecolor=colors["blue"], linewidth=0,
        ))
        ax.text(
            0.073, 0.067, "GX", transform=ax.transAxes, ha="center", va="center",
            color="#FFFFFF", fontsize=8.5, **_font_kwargs(semibold=True),
        )
    brand = f"{theme['brand_name']}  •  {theme['brand_handle']}"
    ax.text(
        0.105, 0.067, brand, transform=ax.transAxes, ha="left", va="center",
        color=colors["muted"], fontsize=9.5, **_font_kwargs(semibold=True),
    )
    source_date = analysis["source_trade_date"].strftime("%b %d, %Y")
    ax.text(
        0.945, 0.067, f"RESEARCH SNAPSHOT  •  {source_date.upper()}",
        transform=ax.transAxes, ha="right", va="center", color=colors["muted"],
        fontsize=8.5, **_font_kwargs(mono=True),
    )


def _focused_strike_rows(strikes_data: list[dict], analysis: dict) -> tuple[list[dict], tuple[float, float]]:
    """Return a presentation-only strike window containing every control level."""
    rows = sorted(strikes_data, key=lambda item: float(item["strike"]))
    strikes = [float(row["strike"]) for row in rows]
    if not strikes:
        raise ValueError("EOD post 1 cannot render without GEX strike data")
    intervals = [right - left for left, right in zip(strikes, strikes[1:]) if right > left]
    interval = median(intervals) if intervals else max(abs(strikes[0]) * 0.002, 1.0)
    controls = [
        float(analysis[key])
        for key in ("spot_price", "gamma_flip", "call_wall", "put_wall", "hot_zone")
    ]
    lower = min(controls) - 2 * interval
    upper = max(controls) + 2 * interval
    focused = [row for row in rows if lower <= float(row["strike"]) <= upper]
    if len(focused) < min(5, len(rows)):
        center = float(analysis["spot_price"])
        focused = sorted(rows, key=lambda row: abs(float(row["strike"]) - center))[:min(9, len(rows))]
        focused.sort(key=lambda row: float(row["strike"]))
        lower = min(lower, float(focused[0]["strike"]) - interval)
        upper = max(upper, float(focused[-1]["strike"]) + interval)
    return focused, (lower, upper)


def _summary_value(ax, *, y, label, value, colors, accent=None):
    ax.text(
        0.75, y, label.upper(), transform=ax.transAxes, color=colors["muted"],
        fontsize=8.5, va="top", **_font_kwargs(semibold=True),
    )
    ax.text(
        0.92, y, value, transform=ax.transAxes, color=accent or colors["text"],
        fontsize=12.5, ha="right", va="top", **_font_kwargs(mono=True, semibold=True),
    )


def _render_chart_post(ax, snapshot, analysis, theme, colors):
    _section_title(ax, "Gamma Exposure Map", "Dealer positioning across the active SPX strike complex", colors)
    regime_color = colors["green"] if analysis["regime"] == "POSITIVE" else colors["red"]
    regime_tint = colors["green_tint"] if analysis["regime"] == "POSITIVE" else colors["red_tint"]
    _pill(ax, 0.785, 0.875, 0.16, f"{analysis['regime']} GAMMA", face=regime_tint, color=regime_color)

    _card(ax, (0.055, 0.16), 0.645, 0.605, face=colors["panel"], edge=colors["line"])
    _card(ax, (0.72, 0.16), 0.225, 0.605, face=colors["panel"], edge=colors["line"])
    chart_ax = ax.inset_axes([0.075, 0.205, 0.605, 0.505])
    chart_ax.set_facecolor(colors["panel"])
    strikes_data = snapshot.get("gex_by_strike") or []
    focused_rows, (lower, upper) = _focused_strike_rows(strikes_data, analysis)
    strikes = [float(row["strike"]) for row in focused_rows]
    values = [
        ((row.get("call_gex") or 0) + (row.get("put_gex") or 0)) / 1e9
        for row in focused_rows
    ]
    intervals = [right - left for left, right in zip(strikes, strikes[1:]) if right > left]
    bar_width = (median(intervals) if intervals else max((upper - lower) / 20, 1.0)) * 0.72
    chart_ax.bar(strikes, values, width=bar_width,
                 color=[colors["green"] if value >= 0 else colors["red"] for value in values],
                 alpha=0.88, zorder=3)
    chart_ax.axhline(0, color=colors["line"], linewidth=1, zorder=2)
    chart_ax.axvline(float(analysis["spot_price"]), color=colors["gold"],
                     linewidth=1.8, linestyle="--", zorder=5)
    chart_ax.axvline(float(analysis["gamma_flip"]), color=colors["blue"],
                     linewidth=1.6, linestyle=":", zorder=5)
    for key, color in (("call_wall", colors["green"]), ("put_wall", colors["red"]), ("hot_zone", colors["amber"])):
        chart_ax.axvline(float(analysis[key]), color=color, linewidth=1.0,
                         linestyle="-.", alpha=0.55, zorder=4)
    chart_ax.grid(axis="y", color=colors["line"], linewidth=0.6, alpha=0.5)
    chart_ax.set_xlim(lower, upper)
    chart_ax.tick_params(colors=colors["muted"], labelsize=8.5)
    chart_ax.set_ylabel("NET GEX  ($B)", color=colors["muted"], fontsize=8.5, fontfamily=SANS_FAMILY)
    chart_ax.set_xlabel("STRIKE", color=colors["muted"], fontsize=8.5, fontfamily=SANS_FAMILY)
    chart_ax.set_title(
        "NET GEX BY STRIKE", loc="left", pad=10, color=colors["muted"],
        fontsize=8.5, fontfamily=SANS_FAMILY, fontweight="semibold",
    )
    for label in (*chart_ax.get_xticklabels(), *chart_ax.get_yticklabels()):
        label.set_fontfamily(MONO_FAMILY)
    for spine in chart_ax.spines.values():
        spine.set_color(colors["line"])

    ax.text(
        0.75, 0.73, "POSITIONING SUMMARY", transform=ax.transAxes,
        color=colors["muted"], fontsize=8.5, va="top", **_font_kwargs(semibold=True),
    )
    ax.text(
        0.75, 0.677, analysis["dealer_position"], transform=ax.transAxes,
        color=regime_color, fontsize=22, va="top", **_font_kwargs(semibold=True),
    )
    ax.text(
        0.75, 0.625, _billions(analysis["net_gex_b"], signed=True),
        transform=ax.transAxes, color=regime_color, fontsize=15.5, va="top",
        **_font_kwargs(mono=True, semibold=True),
    )
    ax.plot([0.75, 0.915], [0.575, 0.575], transform=ax.transAxes,
            color=colors["line"], linewidth=0.8)
    _summary_value(ax, y=0.53, label="Spot", value=_level(analysis["spot_price"]), colors=colors, accent=colors["gold"])
    _summary_value(ax, y=0.455, label="Gamma flip", value=_level(analysis["gamma_flip"]), colors=colors, accent=colors["blue"])
    _summary_value(ax, y=0.38, label="Call wall", value=_level(analysis["call_wall"]), colors=colors, accent=colors["green"])
    _summary_value(ax, y=0.305, label="Put wall", value=_level(analysis["put_wall"]), colors=colors, accent=colors["red"])
    _summary_value(ax, y=0.23, label="Hot zone", value=_level(analysis["hot_zone"]), colors=colors, accent=colors["amber"])
    _add_brand_footer(ax, theme=theme, analysis=analysis, colors=colors)


def _render_regime_post(ax, analysis, narrative, theme, colors):
    _section_title(ax, "Gamma Regime", "A positioning-led read on tomorrow's market mechanics", colors)
    positive = analysis["regime"] == "POSITIVE"
    accent = colors["green"] if positive else colors["red"]
    tint = colors["green_tint"] if positive else colors["red_tint"]
    _card(ax, (0.055, 0.17), 0.345, 0.59, face=colors["panel"], edge=colors["line"])
    _card(ax, (0.42, 0.17), 0.525, 0.59, face=colors["panel"], edge=colors["line"])
    ax.plot([0.055, 0.055], [0.19, 0.74], transform=ax.transAxes,
            color=accent, linewidth=4, solid_capstyle="butt")
    ax.text(0.085, 0.72, "CURRENT REGIME", transform=ax.transAxes,
            color=colors["muted"], fontsize=9, va="top", **_font_kwargs(semibold=True))
    ax.text(0.085, 0.655, analysis["regime"], transform=ax.transAxes,
            color=accent, fontsize=25, va="top", **_font_kwargs(semibold=True))
    ax.text(0.085, 0.585, f"DEALERS  {analysis['dealer_position']}", transform=ax.transAxes,
            color=colors["text"], fontsize=11.5, va="top", **_font_kwargs(semibold=True))
    ax.text(0.085, 0.505, "NET GAMMA EXPOSURE", transform=ax.transAxes,
            color=colors["muted"], fontsize=9, va="top", **_font_kwargs(semibold=True))
    ax.text(0.085, 0.445, _billions(analysis["net_gex_b"], signed=True), transform=ax.transAxes,
            color=accent, fontsize=22, va="top", **_font_kwargs(mono=True, semibold=True))
    explanation = _wrap(narrative["regime_explanation"], 36, 3, "regime explanation")
    ax.text(0.085, 0.345, explanation, transform=ax.transAxes, color=colors["text"],
            fontsize=12.5, va="top", linespacing=1.35, **_font_kwargs())

    ax.text(0.45, 0.72, "PRIMARY CONTROL LEVEL", transform=ax.transAxes,
            color=colors["muted"], fontsize=9, va="top", **_font_kwargs(semibold=True))
    ax.text(0.45, 0.655, "GAMMA FLIP", transform=ax.transAxes,
            color=colors["blue"], fontsize=11, va="top", **_font_kwargs(semibold=True))
    ax.text(0.905, 0.665, _level(analysis["gamma_flip"]), transform=ax.transAxes,
            color=colors["text"], fontsize=22, ha="right", va="top",
            **_font_kwargs(mono=True, semibold=True))
    behavior = (
        (0.43, "ABOVE THE FLIP", "Dealer hedging can absorb moves", colors["green"], colors["green_tint"]),
        (0.235, "BELOW THE FLIP", "Directional moves can accelerate", colors["red"], colors["red_tint"]),
    )
    for y, label, body, card_color, card_tint in behavior:
        _card(ax, (0.45, y), 0.465, 0.145, face=card_tint, edge=colors["line"])
        ax.plot([0.47, 0.47], [y + 0.025, y + 0.12], transform=ax.transAxes,
                color=card_color, linewidth=3, solid_capstyle="butt")
        ax.text(0.495, y + 0.11, label, transform=ax.transAxes,
                color=card_color, fontsize=10, va="top", **_font_kwargs(semibold=True))
        ax.text(0.495, y + 0.06, body, transform=ax.transAxes,
                color=colors["text"], fontsize=12.5, va="top", **_font_kwargs())
    _add_brand_footer(ax, theme=theme, analysis=analysis, colors=colors)


def _render_levels_post(ax, analysis, narrative, theme, colors):
    _section_title(ax, "Key Levels", "Strike-level positioning, exposure, and expected hedge response", colors)
    cards = [
        ("CALL WALL", "CEILING", analysis["call_wall"], analysis["call_wall_gex_b"],
         "Dealer selling can cap rallies", colors["green"], colors["green_tint"]),
        ("PUT WALL", "FLOOR", analysis["put_wall"], analysis["put_wall_gex_b"],
         "Dealer hedging can support drops", colors["red"], colors["red_tint"]),
        ("HOT ZONE", "ACCELERATION", analysis["hot_zone"], analysis["hot_zone_gex_b"],
         narrative["hot_zone_explanation"], colors["amber"], colors["amber_tint"]),
    ]
    headers = ((0.085, "LEVEL / FUNCTION", "left"), (0.43, "STRIKE", "right"),
               (0.60, "GEX AT LEVEL", "right"), (0.65, "EXPECTED HEDGE RESPONSE", "left"))
    for x, label, align in headers:
        ax.text(x, 0.765, label, transform=ax.transAxes, color=colors["muted"],
                fontsize=9, ha=align, va="top", **_font_kwargs(semibold=True))
    for index, (label, role, level, exposure, description, accent, tint) in enumerate(cards):
        y = 0.58 - index * 0.205
        _card(ax, (0.055, y), 0.89, 0.17, face=colors["panel"], edge=colors["line"])
        ax.plot([0.067, 0.067], [y + 0.018, y + 0.152], transform=ax.transAxes,
                color=accent, linewidth=4, solid_capstyle="butt")
        ax.text(0.09, y + 0.125, label, transform=ax.transAxes,
                color=accent, fontsize=11.5, va="top", **_font_kwargs(semibold=True))
        ax.text(0.09, y + 0.07, role, transform=ax.transAxes,
                color=colors["muted"], fontsize=10, va="top", **_font_kwargs(mono=True))
        ax.text(0.43, y + 0.112, _level(level), transform=ax.transAxes,
                color=colors["text"], fontsize=20, ha="right", va="top",
                **_font_kwargs(mono=True, semibold=True))
        ax.text(0.60, y + 0.106, _billions(exposure), transform=ax.transAxes,
                color=accent, fontsize=16.5, ha="right", va="top",
                **_font_kwargs(mono=True, semibold=True))
        desc = _wrap(description, 38, 2, f"{label.lower()} explanation")
        ax.text(0.65, y + 0.12, desc, transform=ax.transAxes,
                color=colors["text"], fontsize=13.5, va="top", linespacing=1.3,
                **_font_kwargs())
    _add_brand_footer(ax, theme=theme, analysis=analysis, colors=colors)


def _render_playbook_post(ax, analysis, narrative, theme, colors):
    _section_title(ax, "Tomorrow's Playbook", "Scenario matrix for the next regular session", colors)
    cases = [
        ("BULL CASE", f"Holds above {_level(analysis['gamma_flip'])}",
         narrative["bull_behavior"], f"Target  {_level(analysis['call_wall'])}", colors["green"], colors["green_tint"]),
        ("BEAR CASE", f"Breaks below {_level(analysis['hot_zone'])}",
         narrative["bear_behavior"], f"Support  {_level(analysis['put_wall'])}", colors["red"], colors["red_tint"]),
        ("WILDCARD", f"{_level(analysis['hot_zone'])} at the open",
         narrative["wildcard_commentary"], "Opening flow sets direction", colors["amber"], colors["amber_tint"]),
    ]
    headers = ((0.085, "SCENARIO", "left"), (0.25, "TRIGGER", "left"),
               (0.49, "EXPECTED RESPONSE", "left"), (0.915, "OBJECTIVE", "right"))
    for x, label, align in headers:
        ax.text(x, 0.765, label, transform=ax.transAxes, color=colors["muted"],
                fontsize=9, ha=align, va="top", **_font_kwargs(semibold=True))
    for index, (label, trigger, body, target, accent, tint) in enumerate(cases):
        y = 0.58 - index * 0.205
        _card(ax, (0.055, y), 0.89, 0.17, face=colors["panel"], edge=colors["line"])
        ax.plot([0.067, 0.067], [y + 0.018, y + 0.152], transform=ax.transAxes,
                color=accent, linewidth=4, solid_capstyle="butt")
        ax.text(0.09, y + 0.105, label, transform=ax.transAxes,
                color=accent, fontsize=11, va="top", **_font_kwargs(semibold=True))
        ax.text(0.25, y + 0.105, trigger, transform=ax.transAxes,
                color=colors["text"], fontsize=13.5, va="top", **_font_kwargs(semibold=True))
        wrapped = _wrap(body, 35, 2, f"{label.lower()} behavior")
        ax.text(0.49, y + 0.118, wrapped, transform=ax.transAxes,
                color=colors["text"], fontsize=12.5, va="top", linespacing=1.25,
                **_font_kwargs())
        ax.text(0.915, y + 0.105, target, transform=ax.transAxes,
                color=accent, fontsize=11.5, ha="right", va="top",
                **_font_kwargs(mono=True, semibold=True))
    _add_brand_footer(ax, theme=theme, analysis=analysis, colors=colors)


def _render_verdict_post(ax, analysis, narrative, theme, colors):
    _section_title(ax, "Bottom Line", "The positioning conclusion to carry into the next session", colors)
    verdict = _wrap(
        f"Gamma flip {_level(analysis['gamma_flip'])} controls the setup. {narrative['verdict']}",
        74, 3, "verdict",
    )
    _card(ax, (0.055, 0.455), 0.89, 0.305, face=colors["blue_tint"], edge=colors["blue"], linewidth=1.3)
    ax.plot([0.08, 0.08], [0.49, 0.72], transform=ax.transAxes,
            color=colors["blue"], linewidth=4, solid_capstyle="butt")
    ax.text(0.11, 0.71, "POSITIONING VERDICT", transform=ax.transAxes,
            color=colors["blue"], fontsize=9, va="top", **_font_kwargs(semibold=True))
    ax.text(0.11, 0.645, verdict, transform=ax.transAxes, color=colors["text"],
            fontsize=17, va="top", linespacing=1.3, **_font_kwargs(semibold=True))

    recaps = [
        ("FLIP", analysis["gamma_flip"], colors["blue"]),
        ("RESISTANCE", analysis["call_wall"], colors["green"]),
        ("SUPPORT", analysis["put_wall"], colors["red"]),
        ("HOT ZONE", analysis["hot_zone"], colors["amber"]),
    ]
    for index, (label, value, accent) in enumerate(recaps):
        x = 0.055 + index * 0.225
        _card(ax, (x, 0.245), 0.215, 0.145, face=colors["panel"], edge=colors["line"])
        ax.text(x + 0.018, 0.35, label, transform=ax.transAxes,
                color=colors["muted"], fontsize=8.5, va="top", **_font_kwargs(semibold=True))
        ax.text(x + 0.197, 0.305, _level(value), transform=ax.transAxes,
                color=accent, fontsize=15.5, ha="right", va="top",
                **_font_kwargs(mono=True, semibold=True))
    ax.text(0.055, 0.185, "SAVE THE MAP  •  FOLLOW FOR THE NEXT EOD UPDATE", transform=ax.transAxes,
            color=colors["muted"], fontsize=9.5, va="top", **_font_kwargs(semibold=True))
    _add_brand_footer(ax, theme=theme, analysis=analysis, colors=colors)


def _assert_no_clipping(fig, tracked_texts) -> None:
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    figure_box = fig.bbox
    for item in tracked_texts:
        box = item.get_window_extent(renderer=renderer)
        if box.x0 < -1 or box.y0 < -1 or box.x1 > figure_box.x1 + 1 or box.y1 > figure_box.y1 + 1:
            label = str(item.get_text()).replace("\n", " ")[:80]
            raise ValueError(f"Generated EOD media contains clipped text: {label}")


def render_eod_thread_media(
    chart_snapshot: dict,
    analysis: dict,
    narrative: dict,
    tweets: list[str],
    post_id: str,
    generated_at: datetime,
    *,
    theme: dict | None = None,
) -> list[dict]:
    """Render and validate five professional 1200x675 content-only PNGs."""
    if len(tweets) != 5:
        raise ValueError("EOD media renderer requires exactly five accessible posts")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for EOD media rendering") from exc

    del generated_at  # Kept in the interface for call-site compatibility and audit stability.
    _register_fonts()
    plt.rcParams.update({
        "font.family": SANS_FAMILY,
        "font.sans-serif": [SANS_FAMILY],
        "font.monospace": [MONO_FAMILY],
        "axes.unicode_minus": False,
    })
    theme = validate_eod_theme(theme or default_eod_theme())
    colors = _colors(theme)
    output_dir = (_chart_dir() / str(post_id)).resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    renderers = (
        lambda ax: _render_chart_post(ax, chart_snapshot, analysis, theme, colors),
        lambda ax: _render_regime_post(ax, analysis, narrative, theme, colors),
        lambda ax: _render_levels_post(ax, analysis, narrative, theme, colors),
        lambda ax: _render_playbook_post(ax, analysis, narrative, theme, colors),
        lambda ax: _render_verdict_post(ax, analysis, narrative, theme, colors),
    )
    assets: list[dict] = []
    try:
        for index, render in enumerate(renderers):
            fig = plt.figure(figsize=(12, 6.75), dpi=100, facecolor=colors["bg"])
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_facecolor(colors["bg"])
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.axis("off")
            render(ax)
            _assert_no_clipping(fig, list(ax.texts))
            path = output_dir / f"post-{index + 1}.png"
            fig.savefig(path, dpi=100, facecolor=colors["bg"], format="png")
            plt.close(fig)
            width, height = _png_dimensions(path)
            size = path.stat().st_size
            alt_text = f"SPX EOD GEX analysis image {index + 1} of 5. {tweets[index]}"
            assets.append({
                "post_index": index,
                "kind": MEDIA_KINDS[index],
                "path": str(path.resolve()),
                "alt_text": alt_text[:1000],
                "width": width,
                "height": height,
                "size_bytes": size,
            })
        validate_eod_media_assets(assets)
        return assets
    except Exception:
        plt.close("all")
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
