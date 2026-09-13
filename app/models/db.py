"""
Shared database accessors.

Each model module accepts a `db` argument (a PyMongo Database instance)
rather than importing `mongo` directly, so helpers work outside Flask's
app context (tests, scheduler, CLI scripts).

Usage inside a Flask route / view:
    from app.extensions import mongo
    from app.models import users
    user = users.find_by_email(mongo.db, "alice@example.com")

Usage in the scheduler or a script:
    from pymongo import MongoClient
    from app.utils.time import mongo_client_kwargs
    db = MongoClient(
        os.environ["MONGO_URI"], **mongo_client_kwargs()
    ).get_default_database()
    from app.models import users
    user = users.find_by_email(db, "alice@example.com")

Zerodha market data is intentionally stored in a second database.  The
primary database remains authoritative for users, settings, symbol metadata,
pipeline health, and all Schwab data.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pymongo import MongoClient
from pymongo.uri_parser import parse_uri

from app.utils.time import mongo_client_kwargs


def _database_identity(uri: str) -> tuple[tuple[tuple[str, int], ...], str]:
    """Return a comparable (hosts, database) identity for a Mongo URI."""
    parsed = parse_uri(uri)
    database = parsed.get("database")
    if not database:
        raise ValueError("MongoDB URI must include an explicit database name")
    hosts = tuple(sorted((str(host), int(port)) for host, port in parsed["nodelist"]))
    return hosts, str(database)


def validate_distinct_market_databases(primary_uri: str, zerodha_uri: str) -> None:
    """Refuse configurations that route both stores to the same database."""
    if database_name(primary_uri) == database_name(zerodha_uri):
        raise ValueError(
            "ZERODHA_MONGO_URI must point to a database distinct from MONGO_URI"
        )


def database_name(uri: str) -> str:
    """Return the explicit database name without exposing the rest of the URI."""
    return _database_identity(uri)[1]


@lru_cache(maxsize=4)
def _client_for_uri(uri: str) -> MongoClient:
    return MongoClient(uri, serverSelectionTimeoutMS=3000, **mongo_client_kwargs())


def zerodha_market_db(uri: str | None = None):
    """Return this process's cached Zerodha market-data Database object."""
    resolved = uri or os.environ.get("ZERODHA_MONGO_URI")
    if not resolved:
        raise RuntimeError("ZERODHA_MONGO_URI is not configured")
    primary = os.environ.get("MONGO_URI")
    if primary:
        validate_distinct_market_databases(primary, resolved)
    return _client_for_uri(resolved).get_default_database()


def market_db_for_symbol(primary_db, symbol_doc: dict | None, *, zerodha_uri: str | None = None):
    """Route one symbol's market-data read/write to its provider database."""
    provider = str((symbol_doc or {}).get("provider") or "schwab").lower()
    if provider == "zerodha":
        return zerodha_market_db(zerodha_uri)
    return primary_db
