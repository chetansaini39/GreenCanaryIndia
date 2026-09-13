from datetime import datetime
import io
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from bson import ObjectId

from app.routes import social_studio as social_route
CT = ZoneInfo("America/Chicago")
POST_ID = "64b64b64b64b64b64b64b64b"


def _login_as(client, role):
    with client.session_transaction() as session:
        session["user_id"] = "000000000000000000000001"
        session["role"] = role
        session["subscription_status"] = "none"


def _post(path: Path, *, media_version=4):
    assets = [
        {
            "post_index": index,
            "kind": kind,
            "path": str(path),
            "alt_text": f"Alt text {index + 1}",
            "width": 1200,
            "height": 675,
            "size_bytes": path.stat().st_size,
        }
        for index, kind in enumerate(("chart", "regime", "levels", "playbook", "verdict"))
    ]
    return {
        "_id": ObjectId(POST_ID),
        "id": POST_ID,
        "post_type": "eod",
        "status": "draft",
        "trigger": "manual",
        "review_required": True,
        "created_at": datetime(2026, 8, 26, 15, 15, tzinfo=CT),
        "source_trade_date": datetime(2026, 8, 26, tzinfo=CT),
        "tweets": [f"Accessible post {index}" for index in range(1, 6)],
        "chart_path": str(path),
        "media_version": media_version,
        "media_assets": assets if media_version in (2, 3, 4) else [],
        "validation_result": {"ok": True},
        "llm_model": "test-model",
    }


def test_media_route_requires_staff_and_serves_selected_asset(client, monkeypatch, tmp_path):
    path = tmp_path / "asset.png"
    path.write_bytes(b"png")
    post = _post(path)
    monkeypatch.setattr(social_route.social_posts, "find_by_id", lambda *args: post)

    assert client.get(f"/social/media/{POST_ID}/2").status_code in (302, 403)
    _login_as(client, "staff")
    response = client.get(f"/social/media/{POST_ID}/2")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == b"png"
    assert client.get(f"/social/media/{POST_ID}/8").status_code == 404


def test_authenticated_media_does_not_exhaust_global_rate_limit(monkeypatch, tmp_path):
    from app import create_app
    from app.config import TestingConfig

    path = tmp_path / "asset.png"
    path.write_bytes(b"png")
    post = _post(path)
    monkeypatch.setattr(social_route.social_posts, "find_by_id", lambda *args: post)
    monkeypatch.setattr(TestingConfig, "RATELIMIT_ENABLED", True)
    monkeypatch.setattr(TestingConfig, "RATELIMIT_STORAGE_URI", "memory://")
    isolated_app = create_app("testing")
    isolated_client = isolated_app.test_client()
    _login_as(isolated_client, "admin")

    statuses = {
        isolated_client.get(f"/social/media/{POST_ID}/0").status_code
        for _ in range(60)
    }

    assert statuses == {200}


def test_studio_keeps_original_thread_chrome_around_five_images(client, monkeypatch, tmp_path):
    path = tmp_path / "asset.png"
    path.write_bytes(b"png")
    post = _post(path)
    monkeypatch.setattr(social_route.platform_settings, "get_settings", lambda db: {})
    monkeypatch.setattr(social_route.social_posts, "find_recent", lambda db, limit: [post])
    _login_as(client, "admin")

    response = client.get("/social")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert body.count(f"/social/media/{POST_ID}/") == 5
    assert "Editing the post copy below does not change the computed media" in body
    assert "Accessible X text — post 5" in body
    assert 'class="visually-hidden"' in body
    assert body.count("GEX Intelligence</span>") >= 5
    assert body.count("@gexintelligence</span>") >= 5
    assert "Tweet 1" in body
    assert "5/5" in body
    assert body.count("background:#3a3a3a") == 4
    assert "max-width:760px" in body
    assert "max-height:428px" in body


def test_studio_keeps_v2_images_viewable_but_unpublishable(client, monkeypatch, tmp_path):
    path = tmp_path / "asset.png"
    path.write_bytes(b"png")
    post = _post(path, media_version=2)
    monkeypatch.setattr(social_route.platform_settings, "get_settings", lambda db: {})
    monkeypatch.setattr(social_route.social_posts, "find_recent", lambda db, limit: [post])
    _login_as(client, "admin")

    body = client.get("/social").get_data(as_text=True)
    assert body.count(f"/social/media/{POST_ID}/") == 5
    assert "Legacy media version 2" in body
    assert "Legacy draft — regenerate to publish" in body


def test_studio_keeps_v3_images_viewable_but_unpublishable(client, monkeypatch, tmp_path):
    path = tmp_path / "asset.png"
    path.write_bytes(b"png")
    post = _post(path, media_version=3)
    monkeypatch.setattr(social_route.platform_settings, "get_settings", lambda db: {})
    monkeypatch.setattr(social_route.social_posts, "find_recent", lambda db, limit: [post])
    _login_as(client, "admin")

    body = client.get("/social").get_data(as_text=True)
    assert body.count(f"/social/media/{POST_ID}/") == 5
    assert "Legacy media version 3" in body
    assert "Legacy draft — regenerate to publish" in body


def test_studio_marks_legacy_eod_unpublishable(client, monkeypatch, tmp_path):
    path = tmp_path / "legacy.png"
    path.write_bytes(b"png")
    post = _post(path, media_version=None)
    monkeypatch.setattr(social_route.platform_settings, "get_settings", lambda db: {})
    monkeypatch.setattr(social_route.social_posts, "find_recent", lambda db, limit: [post])
    _login_as(client, "staff")

    body = client.get("/social").get_data(as_text=True)
    assert "legacy single-image EOD draft" in body
    assert "Legacy draft — regenerate to publish" in body


def test_manual_delete_cleans_generated_eod_media(client, monkeypatch, tmp_path):
    path = tmp_path / "asset.png"
    path.write_bytes(b"png")
    post = _post(path)
    monkeypatch.setattr(social_route.social_posts, "find_by_id", lambda *args: post)
    monkeypatch.setattr(social_route.social_posts, "delete_by_id", lambda *args: True)
    cleanup = MagicMock()
    monkeypatch.setattr(social_route, "cleanup_eod_media", cleanup)
    _login_as(client, "admin")

    response = client.post(f"/social/delete/{POST_ID}")
    assert response.status_code == 302
    cleanup.assert_called_once_with(post)


def test_studio_marks_preview_only_and_disables_publish(client, monkeypatch, tmp_path):
    path = tmp_path / "asset.png"
    path.write_bytes(b"png")
    post = _post(path)
    post.update({
        "trigger": "preview",
        "preview_only": True,
        "input_mode": "metrics",
        "input_format": "json",
        "input_filename": "eod_preview_negative.json",
        "narrative_source": "file",
    })
    monkeypatch.setattr(social_route.platform_settings, "get_settings", lambda db: {})
    monkeypatch.setattr(social_route.social_posts, "find_recent", lambda db, limit: [post])
    _login_as(client, "admin")

    body = client.get("/social").get_data(as_text=True)
    assert "Manual EOD Preview" in body
    assert "Preview only — hardcoded data" in body
    assert "Preview only — publishing disabled" in body
    assert "eod_preview_negative.json" in body
    assert "Mode: <strong>METRICS</strong>" in body


def test_builtin_preview_route_generates_preview_draft(client, monkeypatch):
    preview = {"mode": "metrics"}
    loader = MagicMock(return_value=preview)
    generator = MagicMock(return_value=(POST_ID, True))
    monkeypatch.setattr(social_route, "load_builtin_fixture", loader)
    monkeypatch.setattr(social_route, "generate_eod_preview_draft", generator)
    _login_as(client, "staff")

    response = client.post("/social/preview/eod/negative")
    assert response.status_code == 302
    loader.assert_called_once_with("negative", narrative_source="file")
    assert generator.call_args.args[1] is preview


def test_preview_upload_route_passes_file_without_saving_market_data(client, monkeypatch):
    preview = {"mode": "raw"}
    loader = MagicMock(return_value=preview)
    generator = MagicMock(return_value=(POST_ID, True))
    monkeypatch.setattr(social_route, "load_preview_bytes", loader)
    monkeypatch.setattr(social_route, "generate_eod_preview_draft", generator)
    _login_as(client, "admin")

    response = client.post(
        "/social/preview/eod/upload",
        data={
            "preview_file": (io.BytesIO(b"{}"), "manual.json"),
            "narrative_source": "lm_studio",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    loader.assert_called_once_with(
        b"{}", "manual.json", narrative_source="lm_studio"
    )
    assert generator.call_args.args[1] is preview
