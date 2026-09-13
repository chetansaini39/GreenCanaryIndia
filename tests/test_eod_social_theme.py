import io
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.routes import social_studio as social_route
from app.services import eod_social_theme


USER_ID = "000000000000000000000001"


def _png_bytes(size=(96, 96), color=(29, 155, 240, 255)):
    stream = io.BytesIO()
    Image.new("RGBA", size, color).save(stream, format="PNG")
    return stream.getvalue()


def _values(**updates):
    values = eod_social_theme.default_eod_theme()
    values.pop("logo_path")
    values.update(updates)
    return values


def _login_as(client, role):
    with client.session_transaction() as session:
        session["user_id"] = USER_ID
        session["role"] = role
        session["subscription_status"] = "none"


def test_default_theme_is_valid_and_uses_expected_brand(monkeypatch):
    monkeypatch.setenv("SOCIAL_BRAND_NAME", "GEX Intelligence")
    monkeypatch.setenv("SOCIAL_TWITTER_HANDLE", "@gexintelligence")
    monkeypatch.setenv("SOCIAL_CHART_BG", "#0D1117")
    theme = eod_social_theme.validate_eod_theme(eod_social_theme.default_eod_theme())
    assert theme["brand_name"] == "GEX Intelligence"
    assert theme["brand_handle"] == "@gexintelligence"
    assert theme["background_color"] == "#0D1117"
    assert theme["logo_path"] is None


@pytest.mark.parametrize(
    "updates,error",
    [
        ({"brand_name": "x" * 33}, "1 to 32"),
        ({"brand_handle": "gex"}, "must begin with @"),
        ({"accent_color": "blue"}, "six-digit hex"),
        ({"background_color": "#FFFFFF"}, "must remain dark"),
        ({"text_color": "#151A21"}, "4.5:1 contrast"),
        ({"muted_color": "#151A21"}, "3:1 contrast"),
    ],
)
def test_theme_rejects_unsafe_brand_and_palette(updates, error):
    theme = {**eod_social_theme.default_eod_theme(), **updates}
    with pytest.raises(ValueError, match=error):
        eod_social_theme.validate_eod_theme(theme)


def test_logo_validation_requires_real_square_png():
    assert eod_social_theme.validate_logo(_png_bytes(), "brand.png") == (96, 96)
    with pytest.raises(ValueError, match="PNG format"):
        eod_social_theme.validate_logo(_png_bytes(), "brand.jpg")
    with pytest.raises(ValueError, match="must be square"):
        eod_social_theme.validate_logo(_png_bytes((96, 64)), "brand.png")
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (96).to_bytes(4, "big") * 2
    with pytest.raises(ValueError, match="valid PNG"):
        eod_social_theme.validate_logo(corrupt, "brand.png")


def test_save_theme_persists_validated_values_and_managed_logo(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    monkeypatch.setattr(
        eod_social_theme.platform_settings, "get_eod_theme_overrides", lambda db: {}
    )
    persist = MagicMock()
    monkeypatch.setattr(
        eod_social_theme.platform_settings, "set_eod_theme_overrides", persist
    )

    theme = eod_social_theme.save_eod_theme(
        object(),
        _values(brand_name="Gamma Desk", brand_handle="@gammadesk"),
        USER_ID,
        logo_content=_png_bytes(),
        logo_filename="brand.png",
    )

    assert theme["brand_name"] == "Gamma Desk"
    assert theme["brand_handle"] == "@gammadesk"
    assert theme["logo_path"].startswith(str((tmp_path / "theme").resolve()))
    assert persist.call_args.args[1] == theme


def test_restore_defaults_removes_managed_logo(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_CHART_DIR", str(tmp_path))
    monkeypatch.setenv("SOCIAL_BRAND_NAME", "GEX Intelligence")
    logo_dir = tmp_path / "theme"
    logo_dir.mkdir()
    logo_path = logo_dir / "logo-current.png"
    logo_path.write_bytes(_png_bytes())
    overrides = {
        **_values(brand_name="Custom Theme"),
        "logo_path": str(logo_path.resolve()),
    }
    monkeypatch.setattr(
        eod_social_theme.platform_settings,
        "get_eod_theme_overrides",
        lambda db: overrides,
    )
    reset = MagicMock()
    monkeypatch.setattr(
        eod_social_theme.platform_settings, "reset_eod_theme_overrides", reset
    )

    restored = eod_social_theme.reset_eod_theme(object(), USER_ID)

    reset.assert_called_once()
    assert restored["brand_name"] == "GEX Intelligence"
    assert restored["logo_path"] is None
    assert not logo_path.exists()


def test_theme_routes_are_staff_admin_only_and_forward_fields(client, monkeypatch):
    save = MagicMock(return_value=eod_social_theme.default_eod_theme())
    reset = MagicMock(return_value=eod_social_theme.default_eod_theme())
    monkeypatch.setattr(social_route, "save_eod_theme", save)
    monkeypatch.setattr(social_route, "reset_eod_theme", reset)

    assert client.post("/social/theme/eod", data=_values()).status_code in (302, 403)
    _login_as(client, "staff")
    response = client.post("/social/theme/eod", data=_values(brand_name="Staff Theme"))
    assert response.status_code == 302
    assert save.call_args.args[1]["brand_name"] == "Staff Theme"

    response = client.post("/social/theme/eod/reset")
    assert response.status_code == 302
    reset.assert_called_once()
