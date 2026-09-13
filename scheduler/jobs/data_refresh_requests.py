"""Polls the `data_refresh_requests` collection for pending manual refresh
requests queued by the MCP server's trigger_data_refresh tool (Module 11)
and runs them through the same EOD job the admin panel's manual "rerun"
button uses.

Kept out of the MCP server process on purpose — Zerodha token access stays
isolated to this scheduler process (confirmed open item, Module 11).
"""
import logging
import os

from pymongo import MongoClient

from app.models import data_refresh_requests
from app.utils.time import mongo_client_kwargs
from scheduler.job_types import EOD_ROLLING_5D_WEEKLY, ZERODHA_NIFTY_EOD

log = logging.getLogger(__name__)


def run_pending_data_refresh_requests() -> None:
    from app.services.pipeline_rerun import rerun_job

    client = MongoClient(os.environ["MONGO_URI"], **mongo_client_kwargs())
    db = client.get_default_database()

    try:
        pending = data_refresh_requests.find_pending(db)
        for req in pending:
            symbol = req["symbol"]
            trade_date = req.get("trade_date")
            run_date = trade_date.date() if trade_date else None
            try:
                job_name = (
                    ZERODHA_NIFTY_EOD
                    if symbol.upper() == "NIFTY" else EOD_ROLLING_5D_WEEKLY
                )
                result = rerun_job(job_name, symbol=symbol, trade_date=run_date)
                if result.get("ok"):
                    data_refresh_requests.mark_done(db, req["_id"], result.get("detail", ""))
                else:
                    data_refresh_requests.mark_failed(db, req["_id"], result.get("error", "unknown error"))
            except Exception as exc:
                log.exception("data refresh request failed for %s", symbol)
                data_refresh_requests.mark_failed(db, req["_id"], str(exc))
    finally:
        client.close()
