"""Validated global presentation theme for content-only EOD social images."""
from __future__ import annotations

import hashlib
import io
import os
import re
import struct
from pathlib import Path

from app.models import platform_settings
from app.services.social_chart import _chart_dir


MAX_LOGO_BYTES = 1 * 1024 * 1024
MIN_LOGO_DIMENSION = 64
MAX_LOGO_DIMENSION = 1024
COLOR_FIELDS = (
    "background_color",
    "surface_color",
    "text_color",
    "muted_color",
    "positive_color",
    "negative_color",
    "amber_color",
    "accent_color",
    "spot_color",
)
EDITABLE_FIELDS = ("brand_name", "brand_handle", *COLOR_FIELDS)
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_HANDLE_RE = re.compile(r"^@[A-Za-z0-9_]{1,15}$")


def default_eod_theme() -> dict:
    return {
        "brand_name": os.environ.get("SOCIAL_BRAND_NAME", "GEX Intelligence"),
        "brand_handle": os.environ.get("SOCIAL_TWITTER_HANDLE", "@gexintelligence"),
        "background_color": os.environ.get("SOCIAL_CHART_BG", "#0D1117"),
        "surface_color": os.environ.get("SOCIAL_EOD_IMAGE_SURFACE", "#151A21"),
        "text_color": os.environ.get("SOCIAL_CHART_TEXT", "#E6EDF3"),
        "muted_color": os.environ.get("SOCIAL_EOD_IMAGE_MUTED", "#8B949E"),
        "positive_color": os.environ.get("SOCIAL_EOD_IMAGE_COLOR_POS", "#22C55E"),
        "negative_color": os.environ.get("SOCIAL_EOD_IMAGE_COLOR_NEG", "#EF4444"),
        "amber_color": os.environ.get("SOCIAL_EOD_IMAGE_COLOR_AMBER", "#D29922"),
        "accent_color": os.environ.get("SOCIAL_EOD_IMAGE_COLOR_ACCENT", "#1D9BF0"),
        "spot_color": os.environ.get("SOCIAL_CHART_COLOR_SPOT", "#FFD700"),
        "logo_path": None,
    }


def _rgb(hex_color: str) -> tuple[float, float, float]:
    return tuple(int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _luminance(hex_color: str) -> float:
    values = []
    for channel in _rgb(hex_color):
        values.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


def _contrast(left: str, right: str) -> float:
    lighter, darker = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if (
        len(content) < 24
        or content[:8] != b"\x89PNG\r\n\x1a\n"
        or content[12:16] != b"IHDR"
    ):
        raise ValueError("EOD theme logo must be a valid PNG image")
    return struct.unpack(">II", content[16:24])


def validate_logo(content: bytes, filename: str) -> tuple[int, int]:
    if not isinstance(content, bytes) or not content:
        raise ValueError("EOD theme logo is empty")
    if len(content) > MAX_LOGO_BYTES:
        raise ValueError("EOD theme logo exceeds the 1 MB limit")
    if Path(filename or "").suffix.lower() != ".png":
        raise ValueError("EOD theme logo must use PNG format")
    width, height = _png_dimensions(content)
    try:
        from PIL import Image, UnidentifiedImageError

        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG":
                raise ValueError("EOD theme logo must be a valid PNG image")
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("EOD theme logo must be a valid PNG image") from exc
    if width != height:
        raise ValueError("EOD theme logo must be square")
    if not MIN_LOGO_DIMENSION <= width <= MAX_LOGO_DIMENSION:
        raise ValueError("EOD theme logo dimensions must be between 64 and 1024 pixels")
    return width, height


def _safe_logo_path(value) -> Path | None:
    if not value:
        return None
    root = (_chart_dir() / "theme").resolve()
    path = Path(value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def validate_eod_theme(theme: dict, *, require_logo_file: bool = True) -> dict:
    if not isinstance(theme, dict):
        raise ValueError("EOD image theme must be an object")
    missing = sorted(set((*EDITABLE_FIELDS, "logo_path")) - set(theme))
    unknown = sorted(set(theme) - set((*EDITABLE_FIELDS, "logo_path")))
    if missing:
        raise ValueError(f"EOD image theme is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"EOD image theme contains unknown fields: {', '.join(unknown)}")

    cleaned = dict(theme)
    brand_name = str(cleaned["brand_name"]).strip()
    brand_handle = str(cleaned["brand_handle"]).strip()
    if not 1 <= len(brand_name) <= 32:
        raise ValueError("Brand name must contain 1 to 32 characters")
    if "\n" in brand_name or "\r" in brand_name:
        raise ValueError("Brand name must be one line")
    if not _HANDLE_RE.fullmatch(brand_handle):
        raise ValueError("Brand handle must begin with @ and contain at most 15 letters, numbers, or underscores")
    cleaned["brand_name"] = brand_name
    cleaned["brand_handle"] = brand_handle

    for field in COLOR_FIELDS:
        value = str(cleaned[field]).strip().upper()
        if not _HEX_RE.fullmatch(value):
            raise ValueError(f"{field} must be a six-digit hex color")
        cleaned[field] = value

    background = cleaned["background_color"]
    surface = cleaned["surface_color"]
    text = cleaned["text_color"]
    muted = cleaned["muted_color"]
    if _luminance(background) > 0.18 or _luminance(surface) > 0.24:
        raise ValueError("Background and surface colors must remain dark")
    if _contrast(text, background) < 4.5 or _contrast(text, surface) < 4.5:
        raise ValueError("Text color must have at least 4.5:1 contrast against the background and surface")
    if _contrast(muted, background) < 3.0 or _contrast(muted, surface) < 3.0:
        raise ValueError("Muted text color must have at least 3:1 contrast against the background and surface")
    for field in ("positive_color", "negative_color", "amber_color", "accent_color", "spot_color"):
        if _contrast(cleaned[field], background) < 2.0 or _contrast(cleaned[field], surface) < 2.0:
            raise ValueError(
                f"{field} does not have enough contrast against the background and surface"
            )

    logo_path = _safe_logo_path(cleaned.get("logo_path"))
    if cleaned.get("logo_path") and logo_path is None:
        raise ValueError("EOD theme logo path is outside the managed theme directory")
    if logo_path is not None and require_logo_file:
        if not logo_path.is_file() or logo_path.stat().st_size <= 0:
            raise ValueError("EOD theme logo file is missing")
        validate_logo(logo_path.read_bytes(), logo_path.name)
    cleaned["logo_path"] = str(logo_path) if logo_path else None
    return cleaned


def get_eod_theme(db) -> dict:
    theme = default_eod_theme()
    overrides = platform_settings.get_eod_theme_overrides(db)
    for key in (*EDITABLE_FIELDS, "logo_path"):
        if key in overrides:
            theme[key] = overrides[key]
    return validate_eod_theme(theme)


def save_eod_theme(
    db,
    values: dict,
    actor_id,
    *,
    logo_content: bytes | None = None,
    logo_filename: str = "",
    clear_logo: bool = False,
) -> dict:
    current = get_eod_theme(db)
    candidate = {**current, **{key: values.get(key, "") for key in EDITABLE_FIELDS}}
    old_logo = _safe_logo_path(current.get("logo_path"))
    new_logo: Path | None = None
    if clear_logo:
        candidate["logo_path"] = None
    if logo_content is not None:
        validate_logo(logo_content, logo_filename)
        digest = hashlib.sha256(logo_content).hexdigest()[:16]
        logo_dir = (_chart_dir() / "theme").resolve()
        logo_dir.mkdir(parents=True, exist_ok=True)
        new_logo = logo_dir / f"logo-{digest}.png"
        new_logo.write_bytes(logo_content)
        candidate["logo_path"] = str(new_logo)

    try:
        candidate = validate_eod_theme(candidate)
        platform_settings.set_eod_theme_overrides(db, candidate, actor_id)
    except Exception:
        if new_logo is not None and new_logo != old_logo:
            new_logo.unlink(missing_ok=True)
        raise
    if old_logo is not None and old_logo != new_logo and old_logo != _safe_logo_path(candidate.get("logo_path")):
        old_logo.unlink(missing_ok=True)
    return candidate


def reset_eod_theme(db, actor_id) -> dict:
    current = get_eod_theme(db)
    logo_path = _safe_logo_path(current.get("logo_path"))
    platform_settings.reset_eod_theme_overrides(db, actor_id)
    if logo_path is not None:
        logo_path.unlink(missing_ok=True)
    return validate_eod_theme(default_eod_theme())
