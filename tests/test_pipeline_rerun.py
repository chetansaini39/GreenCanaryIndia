"""Tests for manual pipeline rerun dispatch logic."""
import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.services import pipeline_rerun as pr
from scheduler.job_types import (
    ZERODHA_NIFTY_EOD,
    ZERODHA_NIFTY_INTRADAY,
    ZERODHA_NIFTY_MONTHLY,
    resolve_job_id,
)


class PipelineRerunDispatchTests(unittest.TestCase):
    def test_zerodha_job_ids_resolve_to_themselves(self):
        self.assertEqual(resolve_job_id(ZERODHA_NIFTY_INTRADAY), ZERODHA_NIFTY_INTRADAY)
        self.assertEqual(resolve_job_id(ZERODHA_NIFTY_EOD), ZERODHA_NIFTY_EOD)
        self.assertEqual(resolve_job_id(ZERODHA_NIFTY_MONTHLY), ZERODHA_NIFTY_MONTHLY)

    def test_legacy_us_job_aliases_no_longer_resolve(self):
        """The Schwab/CBOE/yfinance US jobs (and their old aliases) were removed."""
        self.assertIsNone(resolve_job_id("0dte_intraday"))
        self.assertIsNone(resolve_job_id("eod"))
        self.assertIsNone(resolve_job_id("monthly_opex"))
        self.assertIsNone(resolve_job_id("eod_rolling_5d_weekly"))

    def test_rerun_job_rejects_unknown_job(self):
        result = pr.rerun_job("eod_rolling_5d_weekly")
        self.assertFalse(result["ok"])
        self.assertIn("unknown job", result["error"])

    def test_rerun_job_runs_zerodha_job_for_nifty(self):
        with patch("scheduler.jobs.gex_collection.run_zerodha_eod") as run_eod:
            result = pr.rerun_job(ZERODHA_NIFTY_EOD, symbol="NIFTY")
        run_eod.assert_called_once_with(manual=True)
        self.assertEqual(
            result, {"ok": True, "job": ZERODHA_NIFTY_EOD, "symbol": "NIFTY", "detail": "live manual run"}
        )

    def test_rerun_job_rejects_non_nifty_symbol(self):
        result = pr.rerun_job(ZERODHA_NIFTY_EOD, symbol="SPY")
        self.assertFalse(result["ok"])
        self.assertIn("NIFTY only", result["error"])

    def test_rerun_job_rejects_non_today_trade_date(self):
        stale_date = date(2000, 1, 1)
        result = pr.rerun_job(ZERODHA_NIFTY_EOD, trade_date=stale_date)
        self.assertFalse(result["ok"])
        self.assertIn("today's IST date", result["error"])

    def test_rerun_job_accepts_todays_ist_trade_date(self):
        today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        with patch("scheduler.jobs.gex_collection.run_zerodha_intraday") as run_intraday:
            result = pr.rerun_job(ZERODHA_NIFTY_INTRADAY, trade_date=today_ist)
        run_intraday.assert_called_once_with(manual=True)
        self.assertTrue(result["ok"])

    def test_backfill_forward_window_runs_nifty_eod_and_monthly(self):
        with patch("scheduler.jobs.gex_collection.run_zerodha_eod") as run_eod, \
             patch("scheduler.jobs.gex_collection.run_zerodha_monthly") as run_monthly:
            result = pr.backfill_forward_window()
        run_eod.assert_called_once_with(manual=True)
        run_monthly.assert_called_once_with(manual=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["symbols_processed"], 1)

    def test_backfill_forward_window_rejects_non_nifty_symbol(self):
        result = pr.backfill_forward_window(symbol="AAPL")
        self.assertFalse(result["ok"])
        self.assertIn("NIFTY only", result["error"])


if __name__ == "__main__":
    unittest.main()
