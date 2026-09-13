from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.image as mpimg
import pytest

from app.services import eod_social_cards


CT = ZoneInfo("America/Chicago")
GENERATED_AT = datetime(2026, 8, 26, 15, 15, tzinfo=CT)


def _analysis(net_gex_b=1.25):
    positive = net_gex_b >= 0
    return {
        "source_trade_date": datetime(2026, 8, 26, tzinfo=CT),
        "spot_price": 5272.0,
        "regime": "POSITIVE" if positive else "NEGATIVE",
        "dealer_position": "LONG" if positive else "SHORT",
        "net_gex_b": net_gex_b,
        "gamma_flip": 5280.0,
        "call_wall": 5350.0,
        "call_wall_gex_b": 5.33,
        "put_wall": 5180.0,
        "put_wall_gex_b": -4.80,
        "hot_zone": 5260.0,
        "hot_zone_gex_b": -14.20,
    }


def _snapshot(net_gex_b=1.25):
    return {
        "symbol": "SPX",
        "trade_date": datetime(2026, 8, 26, tzinfo=CT),
        "spot_price": 5272.0,
        "net_gex": net_gex_b * 1e9,
        "gex_by_strike": [
            {"strike": 5180, "call_gex": 0.5e9, "put_gex": -4.8e9},
            {"strike": 5220, "call_gex": 0.8e9, "put_gex": -2.0e9},
            {"strike": 5260, "call_gex": 0.7e9, "put_gex": -14.9e9},
            {"strike": 5280, "call_gex": 1.2e9, "put_gex": -0.3e9},
            {"strike": 5310, "call_gex": 2.4e9, "put_gex": -0.2e9},
            {"strike": 5350, "call_gex": 5.33e9, "put_gex": -0.1e9},
        ],
    }


def _narrative():
    return {
        "regime_explanation": "Dealers dampen moves and favor a range.",
        "hot_zone_explanation": "Extreme short gamma can accelerate a break.",
        "bull_behavior": "Dealers buy dips and volatility compresses.",
        "bear_behavior": "Dealer selling can accelerate the decline.",
        "wildcard_commentary": "Opening flows will set the first directional tone.",
        "verdict": "Controlled trade is favored unless support fails.",
    }


def _tweets():
    return [f"Accessible EOD post {index} #SPX" for index in range(1, 6)]


@pytest.mark.parametrize("net_gex_b", [1.25, -3.45])
def test_render_five_exact_pngs_with_ordered_manifest(monkeypatch, tmp_path, net_gex_b):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    assets = eod_social_cards.render_eod_thread_media(
        _snapshot(net_gex_b), _analysis(net_gex_b), _narrative(), _tweets(),
        f"fixture-{net_gex_b}", GENERATED_AT,
    )

    assert [asset["post_index"] for asset in assets] == [0, 1, 2, 3, 4]
    assert [asset["kind"] for asset in assets] == [
        "chart", "regime", "levels", "playbook", "verdict"
    ]
    assert eod_social_cards.MEDIA_VERSION == 4
    assert all(asset["width"] == 1200 and asset["height"] == 675 for asset in assets)
    assert all(0 < asset["size_bytes"] <= 5 * 1024 * 1024 for asset in assets)
    assert all(Path(asset["path"]).is_file() for asset in assets)
    assert eod_social_cards.validate_eod_media_assets(assets)["media_post_indexes"] == [0, 1, 2, 3, 4]

    # The first chart contains both the configured red and green bar colors.
    pixels = mpimg.imread(assets[0]["path"])[..., :3]
    green = tuple(int(value * 255) for value in pixels.reshape(-1, 3).max(axis=0))
    assert pixels.shape[:2] == (675, 1200)
    assert (pixels[..., 1] > 0.65).any()
    assert (pixels[..., 0] > 0.8).any()
    assert max(green) >= 240


def test_missing_chart_data_fails_atomically(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    snapshot = _snapshot()
    snapshot["gex_by_strike"] = []
    with pytest.raises(ValueError, match="without GEX strike data"):
        eod_social_cards.render_eod_thread_media(
            snapshot, _analysis(), _narrative(), _tweets(),
            "missing-chart", GENERATED_AT,
        )
    assert not (tmp_path / "missing-chart").exists()


def test_text_overflow_fails_atomically(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    narrative = _narrative()
    narrative["verdict"] = "overflow " * 200
    with pytest.raises(ValueError, match="overflow"):
        eod_social_cards.render_eod_thread_media(
            _snapshot(), _analysis(), narrative, _tweets(),
            "overflow", GENERATED_AT,
        )
    assert not (tmp_path / "overflow").exists()


def test_cleanup_only_removes_generated_media_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    assets = eod_social_cards.render_eod_thread_media(
        _snapshot(), _analysis(), _narrative(), _tweets(),
        "cleanup", GENERATED_AT,
    )
    unrelated = tmp_path.parent / "unrelated-eod-file.png"
    unrelated.write_bytes(b"keep")
    try:
        eod_social_cards.cleanup_eod_media({"media_assets": assets})
        assert not (tmp_path / "cleanup").exists()
        assert unrelated.read_bytes() == b"keep"
    finally:
        unrelated.unlink(missing_ok=True)


def test_validation_rejects_image_over_x_limit(tmp_path):
    path = (tmp_path / "oversized.png").resolve()
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
        + (1200).to_bytes(4, "big") + (675).to_bytes(4, "big")
        + b"0" * (eod_social_cards.MAX_IMAGE_BYTES + 1)
    )
    assets = [
        {
            "post_index": index,
            "kind": kind,
            "path": str(path),
            "alt_text": "Alt text",
            "width": 1200,
            "height": 675,
            "size_bytes": path.stat().st_size,
        }
        for index, kind in enumerate(eod_social_cards.MEDIA_KINDS)
    ]
    with pytest.raises(ValueError, match="exceeds X's 5 MB limit"):
        eod_social_cards.validate_eod_media_assets(assets)


def test_content_only_footer_has_brand_without_mock_x_chrome():
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    theme = eod_social_cards.default_eod_theme()
    colors = eod_social_cards._colors(theme)
    eod_social_cards._add_brand_footer(
        ax, theme=theme, analysis=_analysis(), colors=colors,
    )
    labels = [item.get_text() for item in ax.texts]
    plt.close(fig)
    assert "GX" in labels
    assert any("GEX Intelligence" in label for label in labels)
    assert any("@gexintelligence" in label for label in labels)
    assert any(
        "RESEARCH SNAPSHOT" in label and "AUG 26, 2026" in label
        for label in labels
    )
    assert all("PM ET" not in label for label in labels)
    assert all("/5" not in label for label in labels)
    assert all("GammaMap" not in label for label in labels)
    assert not hasattr(eod_social_cards, "_add_header")


def test_bundled_ibm_plex_fonts_are_present_and_registered():
    assert all(path.is_file() and path.stat().st_size > 0 for path in eod_social_cards._FONT_FILES.values())
    eod_social_cards._register_fonts()
    assert eod_social_cards.SANS_FAMILY == "IBM Plex Sans"
    assert eod_social_cards.MONO_FAMILY == "IBM Plex Mono"


def test_focused_chart_window_contains_all_control_levels_and_removes_far_strikes():
    snapshot = _snapshot()
    snapshot["gex_by_strike"] = [
        {"strike": 3000, "call_gex": 1, "put_gex": 0},
        *snapshot["gex_by_strike"],
        {"strike": 8000, "call_gex": 1, "put_gex": 0},
    ]
    rows, (lower, upper) = eod_social_cards._focused_strike_rows(
        snapshot["gex_by_strike"], _analysis()
    )
    controls = [_analysis()[key] for key in (
        "spot_price", "gamma_flip", "call_wall", "put_wall", "hot_zone"
    )]
    assert all(lower <= value <= upper for value in controls)
    assert 3000 not in [row["strike"] for row in rows]
    assert 8000 not in [row["strike"] for row in rows]


def test_key_level_cards_use_three_stacked_full_width_rows():
    import matplotlib.pyplot as plt

    eod_social_cards._register_fonts()
    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    theme = eod_social_cards.default_eod_theme()
    colors = eod_social_cards._colors(theme)
    eod_social_cards._render_levels_post(ax, _analysis(), _narrative(), theme, colors)
    row_cards = [
        patch for patch in ax.patches
        if patch.get_width() == pytest.approx(0.89)
        and patch.get_height() == pytest.approx(0.17)
    ]
    plt.close(fig)
    assert len(row_cards) == 3
    assert sorted(round(card.get_y(), 3) for card in row_cards) == [0.17, 0.375, 0.58]


def test_professional_typography_uses_readable_body_and_mono_numbers(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    captured = []
    original = eod_social_cards._assert_no_clipping

    def inspect_text(fig, texts):
        captured.extend(texts)
        original(fig, texts)

    monkeypatch.setattr(eod_social_cards, "_assert_no_clipping", inspect_text)
    eod_social_cards.render_eod_thread_media(
        _snapshot(), _analysis(), _narrative(), _tweets(),
        "typography", GENERATED_AT,
    )

    descriptions = [
        item for item in captured
        if item.get_text() in {
            "Dealer selling can cap rallies",
            "Dealer hedging can support drops",
        }
    ]
    numeric = [item for item in captured if item.get_text() == "5,350"]
    assert descriptions and all(item.get_fontsize() >= 12.5 for item in descriptions)
    assert all(item.get_fontfamily()[0] == eod_social_cards.SANS_FAMILY for item in descriptions)
    assert numeric and all(item.get_fontfamily()[0] == eod_social_cards.MONO_FAMILY for item in numeric)


def test_boundary_length_narrative_and_large_values_render_without_overflow(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    analysis = _analysis()
    analysis.update({
        "spot_price": 98_750,
        "gamma_flip": 98_800,
        "call_wall": 99_500,
        "put_wall": 97_250,
        "hot_zone": 98_250,
        "call_wall_gex_b": 123.45,
        "put_wall_gex_b": -98.76,
        "hot_zone_gex_b": -145.20,
    })
    snapshot = _snapshot()
    snapshot["spot_price"] = analysis["spot_price"]
    snapshot["gex_by_strike"] = [
        {"strike": strike, "call_gex": (index + 1) * 1e9, "put_gex": -(index % 2) * 2e9}
        for index, strike in enumerate((97_000, 97_250, 98_000, 98_250, 98_750, 98_800, 99_000, 99_500, 100_000))
    ]
    narrative = {
        "regime_explanation": "Institutional hedging may compress realized movement into a controlled range",
        "hot_zone_explanation": "Breaks can run",
        "bull_behavior": "Systematic demand absorbs dips",
        "bear_behavior": "Hedging supply follows weakness",
        "wildcard_commentary": "Watch open flow",
        "verdict": "Controlled positioning remains favored unless structural support fails with persistent flow",
    }

    assets = eod_social_cards.render_eod_thread_media(
        snapshot, analysis, narrative, _tweets(), "boundary-values", GENERATED_AT,
    )
    assert len(assets) == 5
    assert all(Path(asset["path"]).is_file() for asset in assets)


def test_rendered_text_contains_no_mock_x_interface(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    captured = []
    original = eod_social_cards._assert_no_clipping

    def inspect_text(fig, texts):
        captured.extend(item.get_text() for item in texts)
        original(fig, texts)

    monkeypatch.setattr(eod_social_cards, "_assert_no_clipping", inspect_text)
    eod_social_cards.render_eod_thread_media(
        _snapshot(), _analysis(), _narrative(), _tweets(),
        "content-only", GENERATED_AT,
    )
    combined = "\n".join(captured)
    assert "4:15 PM ET" not in combined
    assert all(f"{index}/5" not in combined for index in range(1, 6))
    assert "Engagement" not in combined
    assert "GammaMap" not in combined
