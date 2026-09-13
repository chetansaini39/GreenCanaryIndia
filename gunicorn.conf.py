import os

bind = f"{os.environ.get('FLASK_HOST', '127.0.0.1')}:{os.environ.get('FLASK_PORT', '5001')}"
workers = 2
worker_class = "sync"
timeout = 60
keepalive = 5

# Logging — mirrors LOG_LEVEL / LOG_FILE from .env
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
_log_file = os.environ.get("LOG_FILE", "")
accesslog = _log_file or "-"
errorlog = _log_file or "-"

# Reload on code change — only in dev; production sets FLASK_ENV=production
reload = os.environ.get("FLASK_ENV", "production") == "development"
