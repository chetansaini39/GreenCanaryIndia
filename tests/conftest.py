"""
Shared pytest fixtures.

Tests that exercise auth/authorization logic don't need a real MongoDB —
they hit the session check long before any DB call is made (on 403 paths)
or mock the specific DB function on the 200 path.
"""
import pytest
from unittest.mock import patch

from app import create_app


@pytest.fixture(scope="session")
def app():
    """Application instance in testing mode, with MongoDB index creation skipped."""
    # TESTING flag in create_app already skips ensure_all_indexes, but we also
    # patch the symbols_config DB call that api.py makes on the happy path.
    return create_app("testing")


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


# ── Session helpers ────────────────────────────────────────────────────────

def login_as(client, role: str, subscription_status: str = "none"):
    """Set session state as if the user had just authenticated."""
    with client.session_transaction() as sess:
        sess["user_id"] = "000000000000000000000001"
        sess["role"] = role
        sess["subscription_status"] = subscription_status
