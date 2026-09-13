"""Social draft generation and explicit manual X publishing."""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.models import gex_intraday, gex_weekly, post_templates, social_posts
from app.services.eod_narrative import generate_eod_narrative
from app.services.eod_social_cards import (
    MEDIA_VERSION as EOD_MEDIA_VERSION,
    cleanup_eod_media,
    render_eod_thread_media,
    validate_eod_media_assets,
)
from app.services.eod_social_analysis import build_eod_analysis
from app.services.eod_social_theme import get_eod_theme
from app.services.social_chart import _compute_hot_zone, render_gex_chart
from app.utils.time import now_ct


log = logging.getLogger(__name__)

_UNRESOLVED_TOKEN_RE = re.compile(r"\[[^\]\n]+\]")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def site_url() -> str:
    return os.environ.get("SITE_BASE_URL", "https://yourdomain.com").rstrip("/")


_REGIME_LONG = {
    "POSITIVE": (
        "Dealers are long gamma → they sell into rallies and buy dips, "
        "dampening volatility. Mean-reversion bias; expect tighter intraday ranges."
    ),
    "NEGATIVE": (
        "Dealers are short gamma → they buy into rallies and sell dips, "
        "amplifying moves. Trending/momentum bias; wider ranges, watch for "
        "accelerated moves near key levels."
    ),
}

_EOD_REQUIRED_TOKENS = {
    "DATE",
    "DEALER_POSITION",
    "REGIME",
    "NET_GEX_B",
    "REGIME_EXPLANATION",
    "GAMMA_FLIP",
    "CALL_WALL",
    "CALL_WALL_GEX_B",
    "PUT_WALL",
    "PUT_WALL_GEX_B",
    "HOT_ZONE",
    "HOT_ZONE_GEX_B",
    "HOT_ZONE_EXPLANATION",
    "BULL_TRIGGER",
    "BULL_BEHAVIOR",
    "UPSIDE_TARGET",
    "BEAR_TRIGGER",
    "BEAR_BEHAVIOR",
    "DOWNSIDE_TARGET",
    "WILDCARD_LEVEL",
    "WILDCARD_COMMENTARY",
    "VERDICT",
}

_EOD_REQUIRED_FRAGMENTS = (
    "SPX GEX EOD Report",
    "Today's Gamma Regime",
    "Key Strikes to Watch",
    "Tomorrow's Playbook",
    "Bottom Line",
    "Save this. Check back tomorrow EOD for the update.",
    "Follow for daily GEX maps",
    "#SPX #0DTE #GEX #OptionsFlow #MarketStructure",
)

_POST_COUNTS = {"premarket": 1, "eod": 5, "eow": 6}
_REQUIRED_TEMPLATE_TOKENS = {
    "premarket": {
        "DATE", "REGIME", "SPOT", "NET_GEX", "GAMMA_FLIP",
        "CALL_WALL", "PUT_WALL", "HOT_ZONE", "SITE_URL",
    },
    "eod": _EOD_REQUIRED_TOKENS,
    "eow": {
        "WEEK_OF", "REGIME", "REGIME_LONG_TEXT", "SPOT", "NET_GEX",
        "GAMMA_FLIP", "CALL_WALL", "PUT_WALL", "HOT_ZONE", "SITE_URL",
    },
}
_SUPPORTED_TEMPLATE_TOKENS = _REQUIRED_TEMPLATE_TOKENS
_REQUIRED_TEMPLATE_FRAGMENTS = {
    "premarket": (
        "SPX GEX Levels",
        "Full EOD breakdown",
        "#SPX #GEX #0DTE #OptionsFlow",
    ),
    "eod": _EOD_REQUIRED_FRAGMENTS,
    "eow": (
        "SPX Weekly GEX Wrap",
        "Weekly chart attached",
        "Next EOD report: Monday after close.",
        "Follow to never trade without the map",
        "#SPX #GEX #OptionsFlow #WeeklyOptions #MarketStructure",
    ),
}


# ── Shared template helpers ────────────────────────────────────────────────

def _safe(value, fmt="{:,.0f}", fallback="N/A") -> str:
    if value is None:
        return fallback
    try:
        return fmt.format(value)
    except Exception:
        return str(value)


def _build_tokens(snapshot: dict) -> dict:
    """Legacy token map for pre-market and EOW posts (kept unchanged)."""
    net_gex = snapshot.get("net_gex")
    regime = "POSITIVE" if (net_gex or 0) >= 0 else "NEGATIVE"
    hot_zone = _compute_hot_zone(snapshot.get("gex_by_strike") or [])

    td = snapshot.get("trade_date")
    if isinstance(td, (date, datetime)):
        date_display = td.strftime("%b %d, %Y")
        date_iso = td.strftime("%Y-%m-%d")
    else:
        today = date.today()
        date_display = today.strftime("%b %d, %Y")
        date_iso = today.isoformat()

    wo = snapshot.get("week_of")
    if isinstance(wo, (date, datetime)):
        week_of_iso = wo.strftime("%Y-%m-%d")
    else:
        week_of_iso = date_iso

    return {
        "DATE": date_display,
        "DATE_ISO": date_iso,
        "WEEK_OF": week_of_iso,
        "SPOT": _safe(snapshot.get("spot_price"), "{:,.2f}"),
        "NET_GEX": _safe(net_gex, "{:+,.2f}"),
        "CALL_WALL": _safe(snapshot.get("call_wall")),
        "PUT_WALL": _safe(snapshot.get("put_wall")),
        "HOT_ZONE": _safe(hot_zone) if hot_zone is not None else "N/A",
        "GAMMA_FLIP": snapshot.get("gamma_flip") or "N/A",
        "REGIME": regime,
        "REGIME_LONG_TEXT": _REGIME_LONG[regime],
        "SITE_URL": site_url(),
    }


def _fill(template: str, tokens: dict) -> str:
    for key, value in tokens.items():
        template = template.replace(f"[{key}]", str(value))
    return template


def _split_tweets(template_text: str) -> list[str]:
    return [
        tweet.strip()
        for tweet in template_text.split(post_templates.TWEET_SEP)
        if tweet.strip()
    ]


def _latest_spx_intraday(db) -> dict | None:
    return gex_intraday.find_latest(db, "SPX")


def _latest_spx_weekly(db) -> dict | None:
    results = gex_weekly.find_recent(db, "SPX", n=1)
    return results[0] if results else None


# ── EOD deterministic formatting and validation ──────────────────────────

def format_level(value: float) -> str:
    return f"{float(value):,.0f}"


def format_billions(value: float, *, signed_currency: bool = False) -> str:
    number = float(value)
    if signed_currency:
        sign = "+" if number >= 0 else "-"
        return f"{sign}${abs(number):,.2f}B"
    return f"{abs(number):,.2f}B"


def x_weighted_length(text: str) -> int:
    """Return X's weighted length, including the fixed 23-character URL weight."""
    single_weight_ranges = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
    normalized = unicodedata.normalize("NFC", text)

    def _plain_weight(value: str) -> int:
        total = 0
        for character in value:
            codepoint = ord(character)
            total += (
                1
                if any(start <= codepoint <= end for start, end in single_weight_ranges)
                else 2
            )
        return total

    total = 0
    cursor = 0
    for match in _URL_RE.finditer(normalized):
        total += _plain_weight(normalized[cursor:match.start()])
        total += 23
        cursor = match.end()
    total += _plain_weight(normalized[cursor:])
    return total


def validate_post_template(post_type: str, template_text: str) -> None:
    if post_type not in _POST_COUNTS:
        raise ValueError(f"Unknown post_type: {post_type!r}")

    tweets = _split_tweets(template_text)
    expected_count = _POST_COUNTS[post_type]
    if len(tweets) != expected_count:
        raise ValueError(
            f"{post_type.upper()} template must contain exactly {expected_count} post(s)"
        )

    placeholders = {
        placeholder[1:-1] for placeholder in _UNRESOLVED_TOKEN_RE.findall(template_text)
    }
    unsupported = sorted(placeholders - _SUPPORTED_TEMPLATE_TOKENS[post_type])
    if unsupported:
        raise ValueError(f"Unsupported template tokens: {', '.join(unsupported)}")

    missing_tokens = sorted(
        token
        for token in _REQUIRED_TEMPLATE_TOKENS[post_type]
        if f"[{token}]" not in template_text
    )
    if missing_tokens:
        raise ValueError(f"Template is missing tokens: {', '.join(missing_tokens)}")

    missing_fragments = [
        fragment
        for fragment in _REQUIRED_TEMPLATE_FRAGMENTS[post_type]
        if fragment not in template_text
    ]
    if missing_fragments:
        raise ValueError(
            f"{post_type.upper()} template is missing required structure or CTA text"
        )


def validate_eod_template(template_text: str) -> None:
    """Backward-compatible EOD validator used by existing callers and tests."""
    validate_post_template("eod", template_text)


def validate_post_thread(
    post_type: str,
    tweets: list[str],
    *,
    chart_path: str | None = None,
    require_chart: bool = False,
) -> dict:
    if post_type not in _POST_COUNTS:
        raise ValueError(f"Unknown post_type: {post_type!r}")

    expected_count = _POST_COUNTS[post_type]
    if len(tweets) != expected_count:
        raise ValueError(
            f"{post_type.upper()} thread must contain exactly {expected_count} post(s)"
        )

    weighted_lengths: list[int] = []
    for index, tweet in enumerate(tweets, start=1):
        if not isinstance(tweet, str) or not tweet.strip():
            raise ValueError(f"{post_type.upper()} post {index} is empty")
        unresolved = _UNRESOLVED_TOKEN_RE.findall(tweet)
        if unresolved:
            raise ValueError(
                f"{post_type.upper()} post {index} contains unresolved placeholders: "
                f"{', '.join(unresolved)}"
            )
        weighted = x_weighted_length(tweet)
        if weighted > 280:
            raise ValueError(
                f"{post_type.upper()} post {index} is {weighted} weighted characters "
                "(maximum 280)"
            )
        weighted_lengths.append(weighted)

    if require_chart:
        if not chart_path:
            raise ValueError(f"{post_type.upper()} post 1 requires a GEX strike chart")
        path = Path(chart_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"{post_type.upper()} GEX strike chart is missing or empty")

    return {
        "ok": True,
        "tweet_count": expected_count,
        "weighted_lengths": weighted_lengths,
        "unresolved_placeholders": [],
        "media_post_indexes": [0] if chart_path else [],
    }


def validate_eod_thread(
    tweets: list[str],
    *,
    chart_path: str | None = None,
    require_chart: bool = False,
) -> dict:
    """Backward-compatible EOD thread validator used by existing callers."""
    return validate_post_thread(
        "eod",
        tweets,
        chart_path=chart_path,
        require_chart=require_chart,
    )


def _eod_tokens(analysis: dict, narrative: dict) -> dict:
    gamma_flip = format_level(analysis["gamma_flip"])
    call_wall = format_level(analysis["call_wall"])
    put_wall = format_level(analysis["put_wall"])
    hot_zone = format_level(analysis["hot_zone"])
    trade_date = analysis["source_trade_date"]
    return {
        "DATE": trade_date.strftime("%b %d, %Y"),
        "DEALER_POSITION": analysis["dealer_position"],
        "REGIME": analysis["regime"],
        "NET_GEX_B": format_billions(analysis["net_gex_b"], signed_currency=True),
        "REGIME_EXPLANATION": narrative["regime_explanation"],
        "GAMMA_FLIP": gamma_flip,
        "CALL_WALL": call_wall,
        "CALL_WALL_GEX_B": format_billions(analysis["call_wall_gex_b"]),
        "PUT_WALL": put_wall,
        "PUT_WALL_GEX_B": format_billions(analysis["put_wall_gex_b"]),
        "HOT_ZONE": hot_zone,
        "HOT_ZONE_GEX_B": format_billions(analysis["hot_zone_gex_b"]),
        "HOT_ZONE_EXPLANATION": narrative["hot_zone_explanation"],
        "BULL_TRIGGER": gamma_flip,
        "BULL_BEHAVIOR": narrative["bull_behavior"],
        "UPSIDE_TARGET": call_wall,
        "BEAR_TRIGGER": hot_zone,
        "BEAR_BEHAVIOR": narrative["bear_behavior"],
        "DOWNSIDE_TARGET": put_wall,
        "WILDCARD_LEVEL": hot_zone,
        "WILDCARD_COMMENTARY": narrative["wildcard_commentary"],
        "VERDICT": f"Gamma flip {gamma_flip} controls the setup. {narrative['verdict']}",
    }


def _generate_eod_draft(db, triggered_by=None) -> tuple[str, bool]:
    generated_at = now_ct()
    source_date = generated_at.date()
    schedule_key = (
        f"scheduled:eod:SPX:{source_date.isoformat()}" if triggered_by is None else None
    )
    if schedule_key:
        existing = social_posts.find_by_schedule_key(db, schedule_key)
        if existing is not None:
            return str(existing["_id"]), bool(existing.get("chart_path"))

    template_doc = post_templates.get_template(db, "eod")
    if template_doc is None:
        raise RuntimeError("No template found for post_type='eod'")
    validate_eod_template(template_doc["template_text"])

    chart_snapshot, analysis = build_eod_analysis(
        db, trade_day=source_date, generated_at=generated_at
    )
    narrative, llm_model = generate_eod_narrative(analysis)
    tweets = _split_tweets(_fill(template_doc["template_text"], _eod_tokens(analysis, narrative)))
    validate_eod_thread(tweets)
    theme = get_eod_theme(db)

    post_object_id = ObjectId()
    media_assets = render_eod_thread_media(
        chart_snapshot,
        analysis,
        narrative,
        tweets,
        str(post_object_id),
        generated_at,
        theme=theme,
    )
    media_validation = validate_eod_media_assets(media_assets)
    chart_path = media_assets[0]["path"]
    validation = validate_eod_thread(tweets)
    validation.update(media_validation)

    doc = {
        "_id": post_object_id,
        "post_type": "eod",
        "platform": "twitter",
        "symbol": "SPX",
        "status": "draft",
        "review_required": True,
        "trigger": "manual" if triggered_by else "scheduled",
        "generated_by": ObjectId(str(triggered_by)) if triggered_by else None,
        "source_snapshot_ref": chart_snapshot.get("_id"),
        "source_trade_date": analysis["source_trade_date"],
        "schedule_key": schedule_key,
        "structured_analysis": analysis,
        "llm_model": llm_model,
        "validation_result": validation,
        "tweets": tweets,
        "media_version": EOD_MEDIA_VERSION,
        "theme_snapshot": theme,
        "media_assets": media_assets,
        "chart_path": chart_path,
        "media_post_indexes": [0, 1, 2, 3, 4],
    }
    try:
        post_id = social_posts.insert_draft(db, doc)
    except DuplicateKeyError:
        cleanup_eod_media(media_assets)
        if not schedule_key:
            raise
        existing = social_posts.find_by_schedule_key(db, schedule_key)
        if existing is None:
            raise
        return str(existing["_id"]), bool(existing.get("chart_path"))
    except Exception:
        cleanup_eod_media(media_assets)
        raise

    log.info("Review-required EOD draft created: post_id=%s", post_id)
    return str(post_id), True


def generate_eod_preview_draft(db, preview: dict, triggered_by) -> tuple[str, bool]:
    """Create a normal EOD draft from validated manual data, permanently preview-only."""
    template_doc = post_templates.get_template(db, "eod")
    if template_doc is None:
        raise RuntimeError("No template found for post_type='eod'")
    validate_eod_template(template_doc["template_text"])

    analysis = preview["analysis"]
    chart_snapshot = preview["chart_snapshot"]
    narrative = preview.get("narrative")
    llm_model = None
    if preview.get("narrative_source") == "lm_studio":
        narrative, llm_model = generate_eod_narrative(analysis)
    if narrative is None:
        raise ValueError("validated preview narrative is missing")

    tweets = _split_tweets(
        _fill(template_doc["template_text"], _eod_tokens(analysis, narrative))
    )
    validation = validate_eod_thread(tweets)
    generated_at = now_ct()
    post_object_id = ObjectId()
    theme = get_eod_theme(db)
    media_assets = render_eod_thread_media(
        chart_snapshot,
        analysis,
        narrative,
        tweets,
        str(post_object_id),
        generated_at,
        theme=theme,
    )
    try:
        media_validation = validate_eod_media_assets(media_assets)
        validation.update(media_validation)
        validation.update({
            "preview_only": True,
            "input_mode": preview["mode"],
            "input_format": preview["input_format"],
            "narrative_source": preview["narrative_source"],
        })
        doc = {
            "_id": post_object_id,
            "post_type": "eod",
            "platform": "twitter",
            "symbol": "SPX",
            "status": "draft",
            "review_required": True,
            "trigger": "preview",
            "preview_only": True,
            "generated_by": ObjectId(str(triggered_by)),
            "source_snapshot_ref": None,
            "source_trade_date": analysis["source_trade_date"],
            "schedule_key": None,
            "structured_analysis": analysis,
            "llm_model": llm_model,
            "validation_result": validation,
            "input_mode": preview["mode"],
            "input_format": preview["input_format"],
            "input_filename": preview["input_filename"],
            "builtin_scenario": preview.get("builtin_scenario"),
            "narrative_source": preview["narrative_source"],
            "tweets": tweets,
            "media_version": EOD_MEDIA_VERSION,
            "theme_snapshot": theme,
            "media_assets": media_assets,
            "chart_path": media_assets[0]["path"],
            "media_post_indexes": [0, 1, 2, 3, 4],
        }
        post_id = social_posts.insert_draft(db, doc)
    except Exception:
        cleanup_eod_media(media_assets)
        raise

    log.info("Preview-only EOD draft created: post_id=%s", post_id)
    return str(post_id), True


# ── Public generation API ─────────────────────────────────────────────────

def generate_draft(db, post_type: str, triggered_by=None) -> tuple[str, bool]:
    """Generate a social draft. EOD is always review-required and chart-required."""
    if post_type not in ("premarket", "eod", "eow"):
        raise ValueError(f"Unknown post_type: {post_type!r}")
    if post_type == "eod":
        return _generate_eod_draft(db, triggered_by=triggered_by)

    snapshot = _latest_spx_weekly(db) if post_type == "eow" else _latest_spx_intraday(db)
    if snapshot is None:
        raise ValueError(f"No SPX snapshot available for post_type={post_type!r}")

    template_doc = post_templates.get_template(db, post_type)
    if template_doc is None:
        raise RuntimeError(f"No template found for post_type={post_type!r}")
    validate_post_template(post_type, template_doc["template_text"])
    filled = _fill(template_doc["template_text"], _build_tokens(snapshot))
    tweets = _split_tweets(filled)
    validation = validate_post_thread(post_type, tweets)

    doc = {
        "post_type": post_type,
        "platform": "twitter",
        "symbol": "SPX",
        "status": "draft",
        "trigger": "manual" if triggered_by else "scheduled",
        "generated_by": ObjectId(str(triggered_by)) if triggered_by else None,
        "source_snapshot_ref": snapshot.get("_id"),
        "tweets": tweets,
        "chart_path": None,
        "media_post_indexes": [],
        "validation_result": validation,
    }
    post_id = social_posts.insert_draft(db, doc)

    chart_ok = True
    if post_type == "eow":
        try:
            chart_path = render_gex_chart(snapshot, str(post_id))
            validation = validate_post_thread(
                post_type,
                tweets,
                chart_path=chart_path,
            )
            db["social_posts"].update_one(
                {"_id": post_id},
                {"$set": {
                    "chart_path": chart_path,
                    "media_post_indexes": [0],
                    "validation_result": validation,
                }},
            )
        except Exception:
            log.exception("Chart render failed for legacy post %s", post_id)
            chart_ok = False

    log.info("Draft created: post_id=%s post_type=%s chart_ok=%s", post_id, post_type, chart_ok)
    return str(post_id), chart_ok


# ── Explicit manual publish API ───────────────────────────────────────────

def publish_post(db, post_id: str) -> list[str]:
    """Upload media, then publish the post or reply chain via OAuth 1.0a."""
    post = social_posts.find_by_id(db, post_id)
    if post is None:
        raise ValueError(f"Post {post_id!r} not found")
    if post.get("preview_only"):
        raise RuntimeError("Preview-only drafts cannot be published to X")
    if post["status"] == "published":
        return post.get("tweet_ids", [])

    # Validate the complete immutable thread/media set before credentials or
    # any X client is touched. Version-2 mock-X images are intentionally
    # reviewable but never publishable.
    tweets = post.get("tweets") or []
    chart_path = post.get("chart_path")
    post_type = post.get("post_type")
    eod_assets: list[dict] = []
    if post_type == "eod":
        if post.get("media_version") != EOD_MEDIA_VERSION:
            raise RuntimeError(
                "This legacy EOD draft cannot be published. Generate a current content-only draft."
            )
        eod_assets = post.get("media_assets") or []
        validate_post_thread(post_type, tweets)
        validate_eod_media_assets(eod_assets)
    elif post_type in _POST_COUNTS:
        validate_post_thread(post_type, tweets, chart_path=chart_path)
    if not tweets:
        raise ValueError("Post has no tweet content")

    try:
        import tweepy
    except ImportError as exc:
        raise RuntimeError("tweepy is not installed") from exc

    consumer_key = os.environ.get("TWITTER_API_KEY") or os.environ.get("TWITTER_CONSUMER_KEY")
    consumer_secret = (
        os.environ.get("TWITTER_API_SECRET")
        or os.environ.get("TWITTER_CONSUMER_SECRET")
    )
    access_token = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_secret = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")
    if not all([consumer_key, consumer_secret, access_token, access_secret]):
        raise RuntimeError("Twitter credentials not configured in environment")

    client = tweepy.Client(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )
    tweet_ids: list[str] = []
    reply_to: str | None = None
    try:
        # Media preparation must finish before post 1 is created.
        if eod_assets:
            api_v1 = _build_media_api()
            media_ids = [_upload_media_asset(api_v1, asset) for asset in eod_assets]
        else:
            media_ids = [_upload_media(client, chart_path)] if chart_path else []
        for index, text in enumerate(tweets):
            kwargs: dict = {"text": text}
            if reply_to:
                kwargs["in_reply_to_tweet_id"] = reply_to
            if eod_assets:
                kwargs["media_ids"] = [media_ids[index]]
            elif index == 0 and media_ids:
                kwargs["media_ids"] = [media_ids[0]]

            response = client.create_tweet(**kwargs)
            tweet_id = str(response.data["id"])
            tweet_ids.append(tweet_id)
            reply_to = tweet_id

        social_posts.mark_published(db, post_id, tweet_ids)
        log.info("Published post %s: tweet_ids=%s", post_id, tweet_ids)
        return tweet_ids
    except Exception as exc:
        social_posts.mark_failed(db, post_id, str(exc), partial_tweet_ids=tweet_ids)
        log.exception("Failed to publish post %s", post_id)
        raise


def _upload_media(client, chart_path: str) -> str:
    """Upload a chart through X's OAuth 1.0a v1.1 media endpoint."""
    if not chart_path:
        raise RuntimeError("Chart path is required for media upload")
    path = Path(chart_path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("Chart media is missing or empty")

    api_v1 = _build_media_api()
    media = api_v1.media_upload(filename=str(path))
    return str(media.media_id)


def _build_media_api():
    """Create the OAuth 1.0a API client used by X's media endpoints."""
    import tweepy

    consumer_key = os.environ.get("TWITTER_API_KEY") or os.environ.get("TWITTER_CONSUMER_KEY")
    consumer_secret = (
        os.environ.get("TWITTER_API_SECRET")
        or os.environ.get("TWITTER_CONSUMER_SECRET")
    )
    access_token = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_secret = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")
    auth = tweepy.OAuth1UserHandler(
        consumer_key, consumer_secret, access_token, access_secret
    )
    return tweepy.API(auth)


def _upload_media_asset(api_v1, asset: dict) -> str:
    """Upload one validated EOD image and set its accessible alt text."""
    path = Path(asset["path"])
    media = api_v1.media_upload(filename=str(path), media_category="tweet_image")
    media_id = str(media.media_id)
    api_v1.create_media_metadata(media_id, asset["alt_text"])
    return str(media.media_id)
