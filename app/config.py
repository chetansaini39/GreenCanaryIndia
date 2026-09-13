import os
import tempfile
from dotenv import load_dotenv

load_dotenv()


class BaseConfig:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/retailgex")
    ZERODHA_MONGO_URI = os.environ.get(
        "ZERODHA_MONGO_URI", "mongodb://localhost:27017/retailgex_zerodha"
    )
    TIMEZONE = os.environ.get("TIMEZONE", "America/Chicago")

    # Flask-Session — MongoDB backend.
    # SESSION_MONGODB (the MongoClient instance) is injected in create_app()
    # after mongo.init_app() runs, so it cannot live here as a class attribute.
    SESSION_TYPE = "mongodb"
    SESSION_MONGODB_DB = "retailgex"
    SESSION_MONGODB_COLLECT = "flask_sessions"
    SESSION_PERMANENT = False
    SESSION_USE_SIGNER = True       # signs the session cookie to prevent tampering

    # Google OAuth (Authlib)
    GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")

    # Rate limiting
    # flask-limiter reads RATELIMIT_STORAGE_URI. Default to the shared MongoDB
    # so counters are consistent across gunicorn workers (memory:// would give
    # each worker its own counter, effectively multiplying every limit).
    RATELIMIT_DEFAULT = "200 per day;50 per hour"
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", MONGO_URI)

    # Max upload size — two chart PNGs, generous headroom
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB

    # LM Studio vision (Module 10 weekly chart analysis)
    LM_STUDIO_BASE_URL     = os.environ.get("LM_STUDIO_BASE_URL",     "http://localhost:1234")
    LM_STUDIO_VISION_MODEL = os.environ.get("LM_STUDIO_VISION_MODEL", "google/gemma-4-e4b")
    LM_STUDIO_TEXT_MODEL   = os.environ.get("LM_STUDIO_TEXT_MODEL",   "google/gemma-4-e4b")
    LM_STUDIO_TIMEOUT      = os.environ.get("LM_STUDIO_TIMEOUT",      "240")

    # Social Post Studio (Module 10) — chart branding
    # TODO: Replace placeholder defaults with real values before public launch.
    # All of these can be overridden in .env without touching code.
    SOCIAL_BRAND_NAME     = os.environ.get("SOCIAL_BRAND_NAME",     "GEX Intelligence")
    SOCIAL_TWITTER_HANDLE = os.environ.get("SOCIAL_TWITTER_HANDLE", "@gexintelligence")
    SOCIAL_CHART_COLOR_POS  = os.environ.get("SOCIAL_CHART_COLOR_POS",  "#2196F3")
    SOCIAL_CHART_COLOR_NEG  = os.environ.get("SOCIAL_CHART_COLOR_NEG",  "#EF5350")
    SOCIAL_CHART_COLOR_SPOT = os.environ.get("SOCIAL_CHART_COLOR_SPOT", "#FFD700")
    SOCIAL_CHART_BG         = os.environ.get("SOCIAL_CHART_BG",         "#0D1117")
    SOCIAL_CHART_TEXT       = os.environ.get("SOCIAL_CHART_TEXT",       "#E0E0E0")
    SOCIAL_CHART_DIR        = os.environ.get("SOCIAL_CHART_DIR",        "output/social_charts")
    # EOD v3 content-image theme. Separate names keep the legacy EOW chart
    # palette and units fully backward compatible.
    SOCIAL_EOD_IMAGE_COLOR_POS = os.environ.get("SOCIAL_EOD_IMAGE_COLOR_POS", "#22C55E")
    SOCIAL_EOD_IMAGE_COLOR_NEG = os.environ.get("SOCIAL_EOD_IMAGE_COLOR_NEG", "#EF4444")
    SOCIAL_EOD_IMAGE_COLOR_AMBER = os.environ.get("SOCIAL_EOD_IMAGE_COLOR_AMBER", "#D29922")
    SOCIAL_EOD_IMAGE_COLOR_ACCENT = os.environ.get("SOCIAL_EOD_IMAGE_COLOR_ACCENT", "#1D9BF0")
    SOCIAL_EOD_IMAGE_SURFACE = os.environ.get("SOCIAL_EOD_IMAGE_SURFACE", "#151A21")
    SOCIAL_EOD_IMAGE_MUTED = os.environ.get("SOCIAL_EOD_IMAGE_MUTED", "#8B949E")
    SOCIAL_EOD_MAX_SNAPSHOT_AGE_MINUTES = os.environ.get(
        "SOCIAL_EOD_MAX_SNAPSHOT_AGE_MINUTES", "120"
    )

    # Transactional email (Module 01 — verification & password reset).
    # Uses SendGrid's Web API — see docs/spec/07-deployment-infra.md.
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "noreply@retailgex.csaini.org")
    SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")

    # Inbox that receives contact-form notifications (Module 01 /contact backend).
    ADMIN_CONTACT_EMAIL = os.environ.get("ADMIN_CONTACT_EMAIL", "hello@retailgex.csaini.org")

    # Stripe
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
    STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")

    # Public MCP server URL (Module 11) — shown to users on the "API key
    # created" page so they can copy it straight into their MCP client config.
    MCP_PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "https://retailgex.csaini.org/mcp")

    # Pro tier display price for the homepage pricing card (Module 01 homepage).
    # Sourced here rather than hardcoded in the template so changing the price
    # doesn't require a template edit. Surfaced via /api/pricing too.
    PRO_PRICE = os.environ.get("PRO_PRICE", "$29")


class DevelopmentConfig(BaseConfig):
    DEBUG = True


class ProductionConfig(BaseConfig):
    DEBUG = False


class TestingConfig(BaseConfig):
    TESTING = True
    MONGO_URI = "mongodb://localhost:27017/retailgex_test"
    ZERODHA_MONGO_URI = "mongodb://localhost:27017/retailgex_zerodha_test"
    # Filesystem session avoids needing a live Mongo connection in unit tests
    SESSION_TYPE = "filesystem"
    SESSION_FILE_DIR = tempfile.mkdtemp()
    WTF_CSRF_ENABLED = False
    # Disable rate limiting in tests
    RATELIMIT_ENABLED = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
