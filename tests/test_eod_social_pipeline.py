from datetime import date, datetime
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import requests

from app.models import platform_settings, post_templates
from app.services import eod_narrative
from app.services import eod_social_analysis as analysis_service
from app.services import social_generation
from scheduler.jobs import social_post as social_job


CT = ZoneInfo("America/Chicago")
TRADE_DAY = date(2026, 8, 26)
SNAPSHOT_AT = datetime(2026, 8, 26, 14, 50, tzinfo=CT)
GENERATED_AT = datetime(2026, 8, 26, 15, 15, tzinfo=CT)


@pytest.fixture(autouse=True)
def _fixed_eod_theme(monkeypatch):
    theme = {
        "brand_name": "GEX Intelligence",
        "brand_handle": "@gexintelligence",
        "background_color": "#0D1117",
        "surface_color": "#151A21",
        "text_color": "#E6EDF3",
        "muted_color": "#8B949E",
        "positive_color": "#22C55E",
        "negative_color": "#EF4444",
        "amber_color": "#D29922",
        "accent_color": "#1D9BF0",
        "spot_color": "#FFD700",
        "logo_path": None,
    }
    monkeypatch.setattr(social_generation, "get_eod_theme", lambda db: dict(theme))


def _contracts(iv=0.25):
    return [
        {"type": "P", "strike": 99, "expiry": TRADE_DAY, "iv": iv, "open_interest": 100},
        {"type": "C", "strike": 101, "expiry": TRADE_DAY, "iv": iv, "open_interest": 100},
    ]


def _analysis():
    return {
        "source_trade_date": datetime(2026, 8, 26, tzinfo=CT),
        "snapshot_at": SNAPSHOT_AT,
        "expiry_at": datetime(2026, 8, 26, 15, 0, tzinfo=CT),
        "spot_price": 100.0,
        "zero_dte_contract_count": 2,
        "regime": "POSITIVE",
        "dealer_position": "LONG",
        "net_gex_raw": 1.25e9,
        "net_gex_b": 1.25,
        "gamma_flip": 100.0,
        "call_wall": 110.0,
        "call_wall_gex_raw": 3e9,
        "call_wall_gex_b": 3.0,
        "put_wall": 90.0,
        "put_wall_gex_raw": -4e9,
        "put_wall_gex_b": -4.0,
        "hot_zone": 95.0,
        "hot_zone_gex_raw": -3.8e9,
        "hot_zone_gex_b": -3.8,
    }


def _narrative():
    return {
        "regime_explanation": "Dealers dampen moves and favor a range",
        "hot_zone_explanation": "Hedging peaks",
        "bull_behavior": "Volatility compresses",
        "bear_behavior": "Dealer selling can accelerate",
        "wildcard_commentary": "Opening flows",
        "verdict": "Controlled trade is favored unless support fails",
    }


def _thread():
    tokens = social_generation._eod_tokens(_analysis(), _narrative())
    filled = social_generation._fill(post_templates._DEFAULTS["eod"], tokens)
    return social_generation._split_tweets(filled)


def _fake_media_assets(tmp_path):
    kinds = ("chart", "regime", "levels", "playbook", "verdict")
    assets = []
    for index, kind in enumerate(kinds):
        path = (tmp_path / f"post-{index + 1}.png").resolve()
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
            + struct.pack(">II", 1200, 675)
        )
        assets.append({
            "post_index": index,
            "kind": kind,
            "path": str(path),
            "alt_text": f"Post {index + 1} alt text",
            "width": 1200,
            "height": 675,
            "size_bytes": path.stat().st_size,
        })
    return assets


class FakeResponse:
    def __init__(self, content=None, *, status_code=200, detail=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = str(detail or "")
        self._content = content
        self._detail = detail

    def json(self):
        if not self.ok:
            return self._detail or {"error": "unavailable"}
        return {"choices": [{"message": {"content": self._content}}]}


def test_filter_same_day_zero_dte():
    rows = _contracts() + [
        {"type": "C", "strike": 120, "expiry": "2026-08-28", "iv": 0.2, "open_interest": 1},
        {"type": "P", "strike": 80, "expiry": None, "iv": 0.2, "open_interest": 1},
    ]
    result = analysis_service.filter_same_day_zero_dte(rows, TRADE_DAY)
    assert len(result) == 2
    assert {row["strike"] for row in result} == {99, 101}


def test_zero_gamma_uses_linear_interpolation_near_spot():
    flip = analysis_service.calculate_zero_gamma_flip(
        _contracts(), 100, SNAPSHOT_AT, TRADE_DAY, grid_points=101
    )
    assert 99.0 < flip < 101.0


@pytest.mark.parametrize(
    "contracts,error",
    [
        (_contracts(iv=0), "invalid IV"),
        ([{**row, "open_interest": -1} for row in _contracts()], "negative open interest"),
    ],
)
def test_zero_gamma_rejects_invalid_iv_or_oi(contracts, error):
    with pytest.raises(ValueError, match=error):
        analysis_service.calculate_zero_gamma_flip(
            contracts, 100, SNAPSHOT_AT, TRADE_DAY
        )


def test_zero_gamma_fails_without_crossing():
    contracts = [
        {"type": "C", "strike": 100, "expiry": TRADE_DAY, "iv": 0.2, "open_interest": 200},
        {"type": "P", "strike": 100, "expiry": TRADE_DAY, "iv": 0.2, "open_interest": 100},
    ]
    with pytest.raises(ValueError, match="no reliable zero-gamma crossing"):
        analysis_service.calculate_zero_gamma_flip(
            contracts, 100, SNAPSHOT_AT, TRADE_DAY
        )


def test_billion_conversion_wall_and_hot_zone_values(monkeypatch):
    source = {
        "_id": "snapshot-id",
        "symbol": "SPX",
        "trade_date": datetime(2026, 8, 26, tzinfo=CT),
        "created_at": SNAPSHOT_AT,
        "spot_price": 100,
        "options_slice": _contracts(),
    }
    recomputed = {
        "net_gex": -0.9e9,
        "call_wall": 110.0,
        "put_wall": 90.0,
        "gex_by_strike": [
            {"strike": 90.0, "call_gex": 0.2e9, "put_gex": -4e9},
            {"strike": 110.0, "call_gex": 3e9, "put_gex": -0.1e9},
        ],
        "options_slice": _contracts(),
    }
    monkeypatch.setattr(analysis_service, "is_trading_day", lambda _: True)
    monkeypatch.setattr(
        analysis_service.gex_rolling_21d,
        "find_by_symbol_date",
        lambda db, symbol, trade_date: source,
    )
    monkeypatch.setattr(analysis_service.gex_engine, "compute", lambda options, spot: recomputed)
    monkeypatch.setattr(analysis_service, "calculate_zero_gamma_flip", lambda *args: 100.5)

    chart_snapshot, result = analysis_service.build_eod_analysis(
        object(), trade_day=TRADE_DAY, generated_at=GENERATED_AT
    )
    assert result["net_gex_b"] == pytest.approx(-0.9)
    assert result["call_wall_gex_b"] == pytest.approx(3.0)
    assert result["put_wall_gex_b"] == pytest.approx(-4.0)
    assert result["hot_zone"] == 90.0
    assert result["hot_zone_gex_b"] == pytest.approx(-3.8)
    assert result["regime"] == "NEGATIVE"
    assert result["dealer_position"] == "SHORT"
    assert chart_snapshot["gamma_flip"] == 100.5
    assert social_generation.format_billions(result["net_gex_b"], signed_currency=True) == "-$0.90B"


def test_snapshot_freshness_rejects_stale_data():
    source = {
        "trade_date": datetime(2026, 8, 26, tzinfo=CT),
        "created_at": datetime(2026, 8, 26, 12, 0, tzinfo=CT),
    }
    with pytest.raises(ValueError, match="stale"):
        analysis_service.validate_snapshot_freshness(
            source, TRADE_DAY, GENERATED_AT, max_age_minutes=120
        )


def test_eod_analysis_refuses_missing_or_empty_same_day_data(monkeypatch):
    monkeypatch.setattr(analysis_service, "is_trading_day", lambda _: True)
    monkeypatch.setattr(
        analysis_service.gex_rolling_21d,
        "find_by_symbol_date",
        lambda *args: None,
    )
    with pytest.raises(ValueError, match="No same-day SPX EOD option chain"):
        analysis_service.build_eod_analysis(
            object(), trade_day=TRADE_DAY, generated_at=GENERATED_AT
        )

    empty_source = {
        "trade_date": datetime(2026, 8, 26, tzinfo=CT),
        "created_at": SNAPSHOT_AT,
        "spot_price": 100,
        "options_slice": [],
    }
    monkeypatch.setattr(
        analysis_service.gex_rolling_21d,
        "find_by_symbol_date",
        lambda *args: empty_source,
    )
    with pytest.raises(ValueError, match="empty 0DTE slice"):
        analysis_service.build_eod_analysis(
            object(), trade_day=TRADE_DAY, generated_at=GENERATED_AT
        )


def test_lm_studio_base_url_normalizes_root_and_v1():
    from app.services.lm_studio import chat_completions_url

    expected = "http://localhost:1234/v1/chat/completions"
    assert chat_completions_url("http://localhost:1234") == expected
    assert chat_completions_url("http://localhost:1234/v1/") == expected


def test_lm_narrative_valid_json(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        import json
        return FakeResponse(json.dumps(_narrative()))

    result, model = eod_narrative.generate_eod_narrative(_analysis(), request_post=post)
    assert result == _narrative()
    assert model == "google/gemma-4-e4b"
    assert len(calls) == 1
    assert calls[0][1]["json"]["temperature"] == 0.2


def test_lm_narrative_repairs_malformed_json():
    import json
    responses = iter([FakeResponse("not-json"), FakeResponse(json.dumps(_narrative()))])
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        return next(responses)

    result, _ = eod_narrative.generate_eod_narrative(_analysis(), request_post=post)
    assert result == _narrative()
    assert len(calls) == 2
    assert "Repair the response" in calls[1]["json"]["messages"][-1]["content"]


def test_lm_narrative_rejects_invented_levels_after_repair():
    import json
    invented = {**_narrative(), "verdict": "Buy above 12345"}

    def post(url, **kwargs):
        return FakeResponse(json.dumps(invented))

    with pytest.raises(RuntimeError, match="invalid EOD narrative after repair"):
        eod_narrative.generate_eod_narrative(_analysis(), request_post=post)


def test_lm_narrative_timeout():
    def post(url, **kwargs):
        raise requests.Timeout("timed out")

    with pytest.raises(RuntimeError, match="LM Studio request failed"):
        eod_narrative.generate_eod_narrative(_analysis(), request_post=post)


def test_lm_narrative_unavailable_model():
    def post(url, **kwargs):
        return FakeResponse(status_code=404, detail={"error": "model not loaded"})

    with pytest.raises(RuntimeError, match="LM Studio 404"):
        eod_narrative.generate_eod_narrative(_analysis(), request_post=post)


def test_workbook_thread_order_limits_and_chart_attachment(tmp_path):
    tweets = _thread()
    chart = tmp_path / "gex.png"
    chart.write_bytes(b"png")
    result = social_generation.validate_eod_thread(
        tweets, chart_path=str(chart), require_chart=True
    )
    assert len(tweets) == 5
    assert tweets[0].startswith("SPX GEX EOD Report")
    assert tweets[1].startswith("📊 Today's Gamma Regime")
    assert tweets[2].startswith("🎯 Key Strikes to Watch")
    assert tweets[3].startswith("Tomorrow's Playbook")
    assert tweets[4].startswith("Bottom Line")
    assert all("[" not in tweet and "]" not in tweet for tweet in tweets)
    assert max(result["weighted_lengths"]) <= 280
    assert result["media_post_indexes"] == [0]


def test_narrative_field_budgets_keep_realistic_spx_thread_x_safe():
    bounded_analysis = {
        **_analysis(),
        "gamma_flip": 17380.0,
        "call_wall": 17400.0,
        "put_wall": 17300.0,
        "hot_zone": 17350.0,
        "call_wall_gex_b": 12.34,
        "put_wall_gex_b": -12.34,
        "hot_zone_gex_b": -12.34,
    }
    bounded_narrative = {
        key: "x" * limit for key, limit in eod_narrative.FIELD_LIMITS.items()
    }
    tokens = social_generation._eod_tokens(bounded_analysis, bounded_narrative)
    tweets = social_generation._split_tweets(
        social_generation._fill(post_templates._DEFAULTS["eod"], tokens)
    )
    result = social_generation.validate_eod_thread(tweets)
    assert max(result["weighted_lengths"]) <= 280
    assert tweets[4].endswith("#SPX #0DTE #GEX #OptionsFlow #MarketStructure")


def test_eod_thread_requires_chart():
    with pytest.raises(ValueError, match="requires a GEX strike chart"):
        social_generation.validate_eod_thread(_thread(), require_chart=True)


def test_scheduled_eod_generation_is_idempotent(monkeypatch):
    existing_id = "64b64b64b64b64b64b64b64b"
    monkeypatch.setattr(social_generation, "now_ct", lambda: GENERATED_AT)
    monkeypatch.setattr(
        social_generation.social_posts,
        "find_by_schedule_key",
        lambda db, key: {"_id": existing_id, "chart_path": "/existing/chart.png"},
    )
    build = MagicMock()
    monkeypatch.setattr(social_generation, "build_eod_analysis", build)
    assert social_generation._generate_eod_draft(object()) == (existing_id, True)
    build.assert_not_called()


def test_chart_failure_prevents_draft_insert(monkeypatch):
    monkeypatch.setattr(social_generation, "now_ct", lambda: GENERATED_AT)
    monkeypatch.setattr(social_generation.social_posts, "find_by_schedule_key", lambda *args: None)
    monkeypatch.setattr(
        social_generation.post_templates,
        "get_template",
        lambda *args: {"template_text": post_templates._DEFAULTS["eod"]},
    )
    monkeypatch.setattr(
        social_generation,
        "build_eod_analysis",
        lambda *args, **kwargs: ({"_id": "snap"}, _analysis()),
    )
    monkeypatch.setattr(
        social_generation,
        "generate_eod_narrative",
        lambda analysis: (_narrative(), "model"),
    )
    monkeypatch.setattr(
        social_generation,
        "render_eod_thread_media",
        MagicMock(side_effect=RuntimeError("render failed")),
    )
    insert = MagicMock()
    monkeypatch.setattr(social_generation.social_posts, "insert_draft", insert)

    with pytest.raises(RuntimeError, match="render failed"):
        social_generation._generate_eod_draft(object())
    insert.assert_not_called()


def test_insert_failure_cleans_all_rendered_media(monkeypatch, tmp_path):
    monkeypatch.setattr(social_generation, "now_ct", lambda: GENERATED_AT)
    monkeypatch.setattr(social_generation.social_posts, "find_by_schedule_key", lambda *args: None)
    monkeypatch.setattr(
        social_generation.post_templates,
        "get_template",
        lambda *args: {"template_text": post_templates._DEFAULTS["eod"]},
    )
    monkeypatch.setattr(
        social_generation,
        "build_eod_analysis",
        lambda *args, **kwargs: ({"_id": "source-snapshot"}, _analysis()),
    )
    monkeypatch.setattr(
        social_generation,
        "generate_eod_narrative",
        lambda analysis: (_narrative(), "model"),
    )
    assets = _fake_media_assets(tmp_path)
    monkeypatch.setattr(social_generation, "render_eod_thread_media", lambda *args, **kwargs: assets)
    monkeypatch.setattr(
        social_generation.social_posts,
        "insert_draft",
        MagicMock(side_effect=RuntimeError("insert failed")),
    )
    cleanup = MagicMock()
    monkeypatch.setattr(social_generation, "cleanup_eod_media", cleanup)

    with pytest.raises(RuntimeError, match="insert failed"):
        social_generation._generate_eod_draft(object())
    cleanup.assert_called_once_with(assets)


def test_scheduled_draft_persists_review_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(social_generation, "now_ct", lambda: GENERATED_AT)
    monkeypatch.setattr(social_generation.social_posts, "find_by_schedule_key", lambda *args: None)
    monkeypatch.setattr(
        social_generation.post_templates,
        "get_template",
        lambda *args: {"template_text": post_templates._DEFAULTS["eod"]},
    )
    monkeypatch.setattr(
        social_generation,
        "build_eod_analysis",
        lambda *args, **kwargs: ({"_id": "source-snapshot"}, _analysis()),
    )
    monkeypatch.setattr(
        social_generation,
        "generate_eod_narrative",
        lambda analysis: (_narrative(), "google/gemma-test"),
    )

    assets = _fake_media_assets(tmp_path)

    def render(*args, **kwargs):
        return assets

    captured = {}

    def insert(db, doc):
        captured.update(doc)
        return doc["_id"]

    monkeypatch.setattr(social_generation, "render_eod_thread_media", render)
    monkeypatch.setattr(social_generation.social_posts, "insert_draft", insert)
    post_id, chart_ok = social_generation._generate_eod_draft(object())

    assert post_id == str(captured["_id"])
    assert chart_ok is True
    assert captured["status"] == "draft"
    assert captured["review_required"] is True
    assert captured["schedule_key"] == "scheduled:eod:SPX:2026-08-26"
    assert captured["source_trade_date"].date() == TRADE_DAY
    assert captured["structured_analysis"]["gamma_flip"] == 100.0
    assert captured["llm_model"] == "google/gemma-test"
    assert captured["validation_result"]["ok"] is True
    assert captured["media_version"] == 4
    assert captured["theme_snapshot"]["brand_name"] == "GEX Intelligence"
    assert captured["media_assets"] == assets
    assert captured["chart_path"] == assets[0]["path"]
    assert captured["media_post_indexes"] == [0, 1, 2, 3, 4]


def test_eod_scheduler_is_review_only(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(social_job, "_run", run)
    monkeypatch.setattr("scheduler.market_utils.is_trading_day", lambda _: True)
    monkeypatch.setattr("app.utils.time.now_ct", lambda: GENERATED_AT)
    social_job.run_eod_post()
    run.assert_called_once_with("eod", allow_autopublish=False)


def test_eod_scheduler_skips_non_trading_day(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(social_job, "_run", run)
    monkeypatch.setattr("scheduler.market_utils.is_trading_day", lambda _: False)
    monkeypatch.setattr("app.utils.time.now_ct", lambda: GENERATED_AT)
    social_job.run_eod_post()
    run.assert_not_called()


def test_eod_autopublish_is_disabled_at_model_boundary():
    assert platform_settings.is_autopublish_enabled(object(), "eod") is False
    with pytest.raises(ValueError, match="require staff/admin review"):
        platform_settings.set_autopublish(
            object(), "eod", True, "64b64b64b64b64b64b64b64b"
        )


def _configure_x_env(monkeypatch):
    monkeypatch.setenv("TWITTER_API_KEY", "key")
    monkeypatch.setenv("TWITTER_API_SECRET", "secret")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN", "token")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN_SECRET", "token-secret")


class FakeXClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def create_tweet(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_on and len(self.calls) == self.fail_on:
            raise RuntimeError("reply failed")
        return SimpleNamespace(data={"id": str(1000 + len(self.calls))})


class FakeMediaAPI:
    def __init__(self, *, fail_upload_on=None, fail_metadata_on=None):
        self.uploads = []
        self.metadata = []
        self.fail_upload_on = fail_upload_on
        self.fail_metadata_on = fail_metadata_on

    def media_upload(self, **kwargs):
        self.uploads.append(kwargs)
        if self.fail_upload_on == len(self.uploads):
            raise RuntimeError("media failed")
        return SimpleNamespace(media_id=f"media-{len(self.uploads)}")

    def create_media_metadata(self, media_id, alt_text):
        self.metadata.append((media_id, alt_text))
        if self.fail_metadata_on == len(self.metadata):
            raise RuntimeError("alt text failed")


def test_x_media_first_then_full_reply_chain(monkeypatch, tmp_path):
    import tweepy

    _configure_x_env(monkeypatch)
    assets = _fake_media_assets(tmp_path)
    post = {
        "status": "draft", "post_type": "eod", "tweets": _thread(),
        "media_version": 4, "media_assets": assets, "chart_path": assets[0]["path"],
    }
    client = FakeXClient()
    api_v1 = FakeMediaAPI()
    monkeypatch.setattr(tweepy, "Client", lambda **kwargs: client)
    monkeypatch.setattr(social_generation.social_posts, "find_by_id", lambda *args: post)
    monkeypatch.setattr(social_generation, "_build_media_api", lambda: api_v1)
    published = MagicMock()
    monkeypatch.setattr(social_generation.social_posts, "mark_published", published)

    ids = social_generation.publish_post(object(), "64b64b64b64b64b64b64b64b")
    assert ids == ["1001", "1002", "1003", "1004", "1005"]
    assert [call["media_ids"] for call in client.calls] == [
        ["media-1"], ["media-2"], ["media-3"], ["media-4"], ["media-5"]
    ]
    assert len(api_v1.uploads) == 5
    assert len(api_v1.metadata) == 5
    assert all(call["media_category"] == "tweet_image" for call in api_v1.uploads)
    assert client.calls[1]["in_reply_to_tweet_id"] == "1001"
    assert client.calls[4]["in_reply_to_tweet_id"] == "1004"
    published.assert_called_once()


def test_x_media_failure_happens_before_any_post(monkeypatch, tmp_path):
    import tweepy

    _configure_x_env(monkeypatch)
    assets = _fake_media_assets(tmp_path)
    post = {
        "status": "draft", "post_type": "eod", "tweets": _thread(),
        "media_version": 4, "media_assets": assets, "chart_path": assets[0]["path"],
    }
    client = FakeXClient()
    monkeypatch.setattr(tweepy, "Client", lambda **kwargs: client)
    monkeypatch.setattr(social_generation.social_posts, "find_by_id", lambda *args: post)
    monkeypatch.setattr(social_generation, "_build_media_api", lambda: FakeMediaAPI(fail_upload_on=3))
    failed = MagicMock()
    monkeypatch.setattr(social_generation.social_posts, "mark_failed", failed)

    with pytest.raises(RuntimeError, match="media failed"):
        social_generation.publish_post(object(), "64b64b64b64b64b64b64b64b")
    assert client.calls == []
    assert failed.call_args.kwargs["partial_tweet_ids"] == []


def test_x_partial_thread_failure_records_created_ids(monkeypatch, tmp_path):
    import tweepy

    _configure_x_env(monkeypatch)
    assets = _fake_media_assets(tmp_path)
    post = {
        "status": "draft", "post_type": "eod", "tweets": _thread(),
        "media_version": 4, "media_assets": assets, "chart_path": assets[0]["path"],
    }
    client = FakeXClient(fail_on=3)
    monkeypatch.setattr(tweepy, "Client", lambda **kwargs: client)
    monkeypatch.setattr(social_generation.social_posts, "find_by_id", lambda *args: post)
    monkeypatch.setattr(social_generation, "_build_media_api", lambda: FakeMediaAPI())
    failed = MagicMock()
    monkeypatch.setattr(social_generation.social_posts, "mark_failed", failed)

    with pytest.raises(RuntimeError, match="reply failed"):
        social_generation.publish_post(object(), "64b64b64b64b64b64b64b64b")
    assert failed.call_args.kwargs["partial_tweet_ids"] == ["1001", "1002"]


def test_x_alt_text_failure_happens_before_any_post(monkeypatch, tmp_path):
    import tweepy

    _configure_x_env(monkeypatch)
    assets = _fake_media_assets(tmp_path)
    post = {
        "status": "draft", "post_type": "eod", "tweets": _thread(),
        "media_version": 4, "media_assets": assets, "chart_path": assets[0]["path"],
    }
    client = FakeXClient()
    monkeypatch.setattr(tweepy, "Client", lambda **kwargs: client)
    monkeypatch.setattr(social_generation.social_posts, "find_by_id", lambda *args: post)
    monkeypatch.setattr(
        social_generation, "_build_media_api", lambda: FakeMediaAPI(fail_metadata_on=4)
    )
    failed = MagicMock()
    monkeypatch.setattr(social_generation.social_posts, "mark_failed", failed)

    with pytest.raises(RuntimeError, match="alt text failed"):
        social_generation.publish_post(object(), "64b64b64b64b64b64b64b64b")
    assert client.calls == []
    assert failed.call_args.kwargs["partial_tweet_ids"] == []


def test_legacy_eod_draft_is_rejected_before_upload(monkeypatch, tmp_path):
    for key in (
        "TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_TOKEN_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    chart = tmp_path / "legacy.png"
    chart.write_bytes(b"png")
    post = {
        "status": "draft", "post_type": "eod", "tweets": _thread(),
        "chart_path": str(chart), "media_version": 2,
    }
    monkeypatch.setattr(social_generation.social_posts, "find_by_id", lambda *args: post)

    with pytest.raises(RuntimeError, match="legacy EOD draft"):
        social_generation.publish_post(object(), "64b64b64b64b64b64b64b64b")
