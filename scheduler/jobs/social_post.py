"""
Scheduled social post jobs — Module 10 Twitter Post Generation Studio.

Three job functions, each called by the APScheduler at their configured time:
  run_premarket_post()  — Pre-Market brief  (7:45 AM CT, confirmed)
  run_eod_post()        — review-only EOD thread (3:15 PM CT / 4:15 PM ET)
  run_eow_post()        — EOW wrap thread   (see TODO in runner.py for exact time)

EOD is intentionally different: it is guarded by the NYSE trading calendar,
uses a per-trade-date idempotency key, and always stops after draft creation.
"""
import logging

log = logging.getLogger(__name__)


def _run(post_type: str, *, allow_autopublish: bool = True) -> None:
    from app import create_app
    from app.extensions import mongo as _mongo
    from app.models import platform_settings
    from app.services.social_generation import generate_draft, publish_post

    app = create_app()
    with app.app_context():
        db = _mongo.db
        try:
            post_id, _ = generate_draft(db, post_type, triggered_by=None)
        except Exception:
            log.exception("Failed to generate %s draft", post_type)
            return

        if allow_autopublish and platform_settings.is_autopublish_enabled(db, post_type):
            try:
                publish_post(db, post_id)
            except Exception:
                log.exception("Auto-publish failed for %s post %s", post_type, post_id)
        else:
            log.info(
                "Auto-publish is OFF for %s — draft %s ready for manual review",
                post_type, post_id,
            )


def run_premarket_post() -> None:
    """Pre-Market brief — 7:45 AM CT (confirmed)."""
    _run("premarket")


def run_eod_post() -> None:
    """Create one unpublished EOD draft at 3:15 PM CT on trading days."""
    from app.utils.time import now_ct
    from scheduler.market_utils import is_trading_day

    trade_day = now_ct().date()
    if not is_trading_day(trade_day):
        log.info("Skipping EOD social draft for non-trading day %s", trade_day)
        return
    _run("eod", allow_autopublish=False)


def run_eow_post() -> None:
    """EOW wrap thread (Fridays). Exact schedule time: see TODO in scheduler/runner.py."""
    _run("eow")
