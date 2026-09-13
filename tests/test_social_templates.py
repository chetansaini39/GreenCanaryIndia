from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.models import post_templates
from app.services import social_generation


CT = ZoneInfo("America/Chicago")


def _legacy_tokens():
    return {
        "DATE": "Aug 27, 2026",
        "WEEK_OF": "2026-08-24",
        "SPOT": "6,500.25",
        "NET_GEX": "+12.34",
        "CALL_WALL": "6,600",
        "PUT_WALL": "6,400",
        "HOT_ZONE": "6,425",
        "GAMMA_FLIP": "6,475",
        "REGIME": "POSITIVE",
        "REGIME_LONG_TEXT": (
            "Dealers are long gamma → they sell into rallies and buy dips, "
            "dampening volatility. Mean-reversion bias; expect tighter intraday ranges."
        ),
        "SITE_URL": "https://retailgex.csaini.org",
    }


def _filled(post_type):
    text = social_generation._fill(post_templates._DEFAULTS[post_type], _legacy_tokens())
    return social_generation._split_tweets(text)


def test_v2_default_template_shapes_and_workbook_inspired_copy():
    assert post_templates.TEMPLATE_VERSIONS == {"premarket": 2, "eod": 2, "eow": 2}
    assert len(social_generation._split_tweets(post_templates._DEFAULTS["premarket"])) == 1
    assert len(social_generation._split_tweets(post_templates._DEFAULTS["eod"])) == 5
    assert len(social_generation._split_tweets(post_templates._DEFAULTS["eow"])) == 6

    assert "SPX GEX Levels" in post_templates._DEFAULTS["premarket"]
    assert "Full EOD breakdown" in post_templates._DEFAULTS["premarket"]
    assert "Weekly chart attached" in post_templates._DEFAULTS["eow"]
    assert "Next EOD report: Monday after close." in post_templates._DEFAULTS["eow"]


@pytest.mark.parametrize("post_type,expected_count", [("premarket", 1), ("eow", 6)])
def test_defaults_fill_without_placeholders_and_fit_x(post_type, expected_count):
    social_generation.validate_post_template(
        post_type, post_templates._DEFAULTS[post_type]
    )
    tweets = _filled(post_type)
    validation = social_generation.validate_post_thread(post_type, tweets)

    assert validation["tweet_count"] == expected_count
    assert validation["unresolved_placeholders"] == []
    assert max(validation["weighted_lengths"]) <= 280
    assert all("[" not in tweet and "]" not in tweet for tweet in tweets)


def test_x_weighted_length_uses_fixed_url_weight():
    prefix = "Full data: "
    short = f"{prefix}https://x.co/a"
    long = f"{prefix}https://example.com/a/very/long/path/that/keeps/going"
    expected = social_generation.x_weighted_length(prefix) + 23
    assert social_generation.x_weighted_length(short) == expected
    assert social_generation.x_weighted_length(long) == expected


def test_template_validation_rejects_unknown_missing_and_wrong_count():
    premarket = post_templates._DEFAULTS["premarket"]
    with pytest.raises(ValueError, match="Unsupported template tokens"):
        social_generation.validate_post_template(
            "premarket", f"{premarket}\n[INVENTED_LEVEL]"
        )
    with pytest.raises(ValueError, match="missing tokens"):
        social_generation.validate_post_template(
            "premarket", premarket.replace("[SPOT]", "spot")
        )
    with pytest.raises(ValueError, match="exactly 1"):
        social_generation.validate_post_template(
            "premarket", f"{premarket}{post_templates.TWEET_SEP}extra"
        )

    with pytest.raises(ValueError, match="required structure or CTA"):
        social_generation.validate_post_template(
            "eow",
            post_templates._DEFAULTS["eow"].replace(
                "Follow to never trade without the map", "Follow for updates"
            ),
        )


def test_filled_thread_validation_rejects_unresolved_empty_and_oversized_posts():
    tweets = _filled("eow")
    unresolved = list(tweets)
    unresolved[0] += " [UNKNOWN]"
    with pytest.raises(ValueError, match="unresolved placeholders"):
        social_generation.validate_post_thread("eow", unresolved)

    empty = list(tweets)
    empty[2] = ""
    with pytest.raises(ValueError, match="post 3 is empty"):
        social_generation.validate_post_thread("eow", empty)

    oversized = list(tweets)
    oversized[4] = "x" * 281
    with pytest.raises(ValueError, match="maximum 280"):
        social_generation.validate_post_thread("eow", oversized)


class _FakeCollection:
    def __init__(self, documents):
        self.documents = {
            (doc["post_type"], doc.get("platform", "twitter")): dict(doc)
            for doc in documents
        }

    def create_index(self, *args, **kwargs):
        return None

    def update_one(self, query, update, upsert=False):
        key = (query["post_type"], query["platform"])
        document = self.documents.get(key)
        if "$or" in query:
            if document is None:
                return None
            target_version = update["$set"]["template_version"]
            current_version = document.get("template_version")
            if current_version is not None and current_version >= target_version:
                return None
            document.update(update["$set"])
            return None
        if document is None and upsert:
            self.documents[key] = dict(update["$setOnInsert"])
        return None


class _FakeDb:
    def __init__(self, documents):
        self.collection = _FakeCollection(documents)

    def __getitem__(self, name):
        assert name == post_templates.COLLECTION
        return self.collection


def test_template_migration_updates_v1_and_preserves_v2_edits():
    db = _FakeDb([
        {
            "post_type": "premarket",
            "platform": "twitter",
            "template_text": "old premarket",
            "template_version": 1,
        },
        {
            "post_type": "eow",
            "platform": "twitter",
            "template_text": "custom v2 eow",
            "template_version": 2,
        },
        {
            "post_type": "eod",
            "platform": "twitter",
            "template_text": "legacy without version",
        },
    ])

    post_templates.ensure_indexes(db)

    docs = db.collection.documents
    assert docs[("premarket", "twitter")]["template_text"] == post_templates._DEFAULTS["premarket"]
    assert docs[("premarket", "twitter")]["template_version"] == 2
    assert docs[("eow", "twitter")]["template_text"] == "custom v2 eow"
    assert docs[("eod", "twitter")]["template_text"] == post_templates._DEFAULTS["eod"]


class _FakeXClient:
    def __init__(self):
        self.calls = []

    def create_tweet(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(data={"id": str(2000 + len(self.calls))})


def _configure_x(monkeypatch):
    monkeypatch.setenv("TWITTER_API_KEY", "key")
    monkeypatch.setenv("TWITTER_API_SECRET", "secret")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN", "token")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN_SECRET", "token-secret")


@pytest.mark.parametrize(
    "post_type,chart_expected,expected_count",
    [("premarket", False, 1), ("eow", True, 6)],
)
def test_publish_keeps_premarket_text_only_and_eow_media_on_post_one(
    monkeypatch, tmp_path, post_type, chart_expected, expected_count
):
    import tweepy

    _configure_x(monkeypatch)
    chart = tmp_path / "weekly.png"
    chart.write_bytes(b"png")
    post = {
        "status": "draft",
        "post_type": post_type,
        "tweets": _filled(post_type),
        "chart_path": str(chart) if chart_expected else None,
    }
    client = _FakeXClient()
    upload = MagicMock(return_value="media-1")
    monkeypatch.setattr(tweepy, "Client", lambda **kwargs: client)
    monkeypatch.setattr(social_generation.social_posts, "find_by_id", lambda *args: post)
    monkeypatch.setattr(social_generation.social_posts, "mark_published", MagicMock())
    monkeypatch.setattr(social_generation, "_upload_media", upload)

    ids = social_generation.publish_post(object(), "64b64b64b64b64b64b64b64b")

    assert len(ids) == expected_count
    assert len(client.calls) == expected_count
    if chart_expected:
        upload.assert_called_once()
        assert client.calls[0]["media_ids"] == ["media-1"]
        assert all("media_ids" not in call for call in client.calls[1:])
    else:
        upload.assert_not_called()
        assert "media_ids" not in client.calls[0]
