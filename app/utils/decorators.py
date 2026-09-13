from functools import wraps
from flask import session, redirect, url_for, flash, request, abort
from app.extensions import mongo
from app.models import users as users_model


def login_required(f):
    """Require an authenticated session. On missing/expired session, redirect to
    /login with a flash message and a `next` param to return the user after login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Your session has expired. Please log in again.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        # Guard against deactivated accounts whose session is still technically live
        user = users_model.find_by_id(mongo.db, session["user_id"])
        if not user or not user.get("is_active"):
            session.clear()
            flash("Your account is inactive. Please contact support.", "danger")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """Require login AND one of the given roles. Returns 403 if role doesn't match."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                flash("Your session has expired. Please log in again.", "warning")
                return redirect(url_for("auth.login", next=request.path))
            if session.get("role") not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator
