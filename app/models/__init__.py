from . import (
    users,
    password_reset_tokens,
    gex_intraday,
    gex_weekly,
    gex_monthly_opex,
    gex_rolling_21d,
    symbols_config,
    subscriptions_log,
    pipeline_health,
    newsletter_subscribers,
    contact_submissions,
    admin_audit_log,
    platform_settings,
    social_posts,
    post_templates,
    scheduler_heartbeat,
    api_keys,
    data_refresh_requests,
)

# Backward-compatible name retained for older callers/tests. The collection was
# renamed to describe the current 21-day retention window; both names point to
# the same module and therefore the same MongoDB collection.
gex_rolling_5d = gex_rolling_21d


def ensure_all_indexes(db):
    """Call once at app startup (or via CLI) to create all collection indexes."""
    users.ensure_indexes(db)
    password_reset_tokens.ensure_indexes(db)
    gex_intraday.ensure_indexes(db)
    gex_weekly.ensure_indexes(db)
    gex_monthly_opex.ensure_indexes(db)
    gex_rolling_21d.ensure_indexes(db)
    symbols_config.ensure_indexes(db)
    subscriptions_log.ensure_indexes(db)
    pipeline_health.ensure_indexes(db)
    newsletter_subscribers.ensure_indexes(db)
    contact_submissions.ensure_indexes(db)
    admin_audit_log.ensure_indexes(db)
    platform_settings.ensure_indexes(db)
    social_posts.ensure_indexes(db)
    post_templates.ensure_indexes(db)
    scheduler_heartbeat.ensure_indexes(db)
    api_keys.ensure_indexes(db)
    data_refresh_requests.ensure_indexes(db)


def ensure_zerodha_market_indexes(db):
    """Create only collections permitted in the isolated Zerodha database."""
    gex_intraday.ensure_indexes(db)
    gex_weekly.ensure_indexes(db)
    gex_monthly_opex.ensure_indexes(db)
    gex_rolling_21d.ensure_indexes(db)
