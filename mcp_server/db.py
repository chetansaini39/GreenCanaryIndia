"""Shared MongoDB connection for the MCP server process.

Separate process from Flask (see docs/spec/11-mcp-server.md) — connects on
import and reuses the same client across every tool call, same pattern as
app.services.pipeline_rerun's manual-rerun MongoClient.
"""
import os

from pymongo import MongoClient

from app.utils.time import mongo_client_kwargs

_client: MongoClient | None = None


def get_db():
    global _client
    if _client is None:
        _client = MongoClient(os.environ["MONGO_URI"], **mongo_client_kwargs())
    return _client.get_default_database()
