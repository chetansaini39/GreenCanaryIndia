"""
Twitter snapshot posting job.

Picks one of the free-tier symbols, pulls the latest weekly GEX snapshot,
and posts a summary tweet with a link to /snapshot/<symbol>/<date>.

Required env vars:
  TWITTER_CONSUMER_KEY
  TWITTER_CONSUMER_SECRET
  TWITTER_ACCESS_TOKEN
  TWITTER_ACCESS_TOKEN_SECRET
  SITE_BASE_URL  (e.g. https://yourdomain.com)
"""
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

_CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))

log = logging.getLogger(__name__)

FREE_SYMBOLS = ["SPY", "QQQ", "TSLA", "NVDA"]


def _pick_symbol(db) -> str | None:
    """Return the first free-tier symbol that has data for today's week."""
    from app.models import gex_weekly
    today = date.today()
    # week_of is stored as CT midnight (trade_date_ct(week_of_monday(...)))
    weekday = today.weekday()
    from datetime import timedelta
    monday = today - timedelta(days=weekday)
    monday_utc = datetime(monday.year, monday.month, monday.day, tzinfo=_CT)

    for sym in FREE_SYMBOLS:
        doc = gex_weekly.find_latest_for_week(db, sym, monday_utc)
        if doc:
            return sym, doc, monday
    return None, None, monday


def _format_tweet(symbol: str, doc: dict, as_of: date, base_url: str) -> str:
    net_gex = doc.get("net_gex")
    call_wall = doc.get("call_wall")
    put_wall = doc.get("put_wall")

    lines = [f"${symbol} GEX Snapshot — {as_of.strftime('%b %d, %Y')}"]
    if net_gex is not None:
        sign = "+" if net_gex >= 0 else ""
        lines.append(f"Net GEX: {sign}{net_gex:,.0f}")
    if call_wall is not None:
        lines.append(f"Call Wall: {call_wall:,.0f}")
    if put_wall is not None:
        lines.append(f"Put Wall: {put_wall:,.0f}")
    lines.append("")
    lines.append(f"{base_url}/snapshot/{symbol}/{as_of.isoformat()}")
    return "\n".join(lines)


def run_twitter_post() -> None:
    """Post a GEX snapshot tweet. Called by the scheduler at 8:00, 9:30, 15:30 CT."""
    try:
        import tweepy
    except ImportError:
        log.error("tweepy is not installed — skipping Twitter post")
        return

    consumer_key    = os.environ.get("TWITTER_CONSUMER_KEY")
    consumer_secret = os.environ.get("TWITTER_CONSUMER_SECRET")
    access_token    = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_secret   = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")
    base_url        = os.environ.get("SITE_BASE_URL", "https://yourdomain.com").rstrip("/")

    if not all([consumer_key, consumer_secret, access_token, access_secret]):
        log.warning("Twitter credentials not configured — skipping post")
        return

    # We need a Flask app context to access MongoDB via flask-pymongo
    from app import create_app
    from app.extensions import mongo as _mongo

    app = create_app()
    with app.app_context():
        db = _mongo.db
        symbol, doc, monday = _pick_symbol(db)

    if doc is None:
        log.warning("No GEX snapshot available to tweet")
        return

    tweet_text = _format_tweet(symbol, doc, monday, base_url)

    try:
        client = tweepy.Client(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        response = client.create_tweet(text=tweet_text)
        log.info("Tweet posted: id=%s symbol=%s", response.data["id"], symbol)
    except Exception:
        log.exception("Failed to post tweet")
