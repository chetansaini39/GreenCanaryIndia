import os
from flask import Flask
from .extensions import mongo, sess, limiter, oauth


def create_app(config_name=None):
    app = Flask(__name__)

    from .config import config
    env = config_name or os.environ.get("FLASK_ENV", "development")
    app.config.from_object(config[env])

    # Validate isolation without connecting to the secondary database. A
    # Zerodha outage must not prevent the US application from starting.
    from .models.db import validate_distinct_market_databases
    validate_distinct_market_databases(
        app.config["MONGO_URI"], app.config["ZERODHA_MONGO_URI"]
    )

    # MongoDB must initialise before Flask-Session so we can hand over the
    # MongoClient (mongo.cx) as the session store. tz_aware kwargs make all
    # stored datetimes come back as Central-Time-aware, not naive UTC.
    from .utils.time import mongo_client_kwargs
    mongo.init_app(app, **mongo_client_kwargs())
    if app.config.get("SESSION_TYPE") == "mongodb":
        app.config["SESSION_MONGODB"] = mongo.cx

    sess.init_app(app)
    limiter.init_app(app)

    # flask-limiter's MongoDB storage backend (the `limits` library) does Mongo
    # I/O inside __del__. When a worker exits, that runs during interpreter
    # teardown — after sys.meta_path is gone — and prints a harmless but noisy
    # "Exception ignored in __del__ ... sys.meta_path is None" traceback. Close
    # the client ourselves at exit, while the interpreter is still alive, and
    # leave a falsy sentinel so __del__'s `if self.storage:` guard short-circuits.
    if not app.config.get("TESTING"):
        import atexit
        _rl_storage = getattr(limiter, "storage", None)
        if _rl_storage is not None and hasattr(_rl_storage, "_storage"):
            @atexit.register
            def _dispose_ratelimit_storage():
                client = _rl_storage._storage
                if client not in (None, False):
                    try:
                        client.close()
                    except Exception:
                        pass
                _rl_storage._storage = False

    # Authlib — register Google as an OAuth 2.0 / OIDC provider
    oauth.init_app(app)
    oauth.register(
        name="google",
        client_id=app.config.get("GOOGLE_OAUTH_CLIENT_ID"),
        client_secret=app.config.get("GOOGLE_OAUTH_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

    # Blueprints
    from .routes.main import main_bp
    from .routes.auth import auth_bp
    from .routes.api import api_bp
    from .routes.billing import billing_bp
    from .routes.admin_panel import admin_bp
    from .routes.public import public_bp
    from .routes.social_studio import social_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(social_bp)

    # Register custom error pages
    from .routes.errors import register_error_handlers
    register_error_handlers(app)

    # Ensure MongoDB indexes exist (idempotent — safe to run on every startup).
    # Skipped in TESTING mode so unit tests don't need a live MongoDB instance.
    if not app.config.get("TESTING"):
        with app.app_context():
            from .models import ensure_all_indexes
            ensure_all_indexes(mongo.db)

    # Heavy deps (numpy/plotly) must load once on the main thread before
    # parallel API handlers run — see app/bootstrap.py.
    if not app.config.get("TESTING"):
        from .bootstrap import eager_import_data_stack
        eager_import_data_stack()

    return app
