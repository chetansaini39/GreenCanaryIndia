"""Tests for manual pipeline rerun dispatch logic."""
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from app.services import pipeline_rerun as pr
from scheduler.job_types import (
    EOD_ROLLING_5D_WEEKLY,
    INTRADAY_0DTE_5MIN,
    MONTHLY_OPEX_3RD_FRIDAY,
    ZERODHA_NIFTY_EOD,
    resolve_job_id,
)


class PipelineRerunDispatchTests(unittest.TestCase):
    def test_legacy_aliases_resolve(self):
        self.assertEqual(resolve_job_id("0dte_intraday"), INTRADAY_0DTE_5MIN)
        self.assertEqual(resolve_job_id("eod"), EOD_ROLLING_5D_WEEKLY)
        self.assertEqual(resolve_job_id("monthly_opex"), MONTHLY_OPEX_3RD_FRIDAY)

    def test_rerun_job_dispatches_single_symbol(self):
        with patch.object(pr, "_rerun_single_symbol", return_value={"ok": True}) as single:
            pr.rerun_job(INTRADAY_0DTE_5MIN, symbol="SPY", trade_date=date(2025, 6, 20))
            single.assert_called_once_with(INTRADAY_0DTE_5MIN, "SPY", date(2025, 6, 20))

    def test_rerun_job_dispatches_all_symbols_when_date_only(self):
        with patch.object(pr, "_rerun_all_symbols", return_value={"ok": True}) as all_syms:
            pr.rerun_job(EOD_ROLLING_5D_WEEKLY, symbol=None, trade_date=date(2025, 6, 18))
            all_syms.assert_called_once_with(EOD_ROLLING_5D_WEEKLY, date(2025, 6, 18))

    def test_rerun_job_dispatches_full_job_when_no_filters(self):
        with patch.object(
            pr,
            "_registry",
            return_value={INTRADAY_0DTE_5MIN: (INTRADAY_0DTE_5MIN, lambda: None)},
        ):
            result = pr.rerun_job(INTRADAY_0DTE_5MIN)
        self.assertEqual(
            result,
            {"ok": True, "job": INTRADAY_0DTE_5MIN, "detail": "full job triggered"},
        )

    def test_rerun_all_symbols_rejects_non_trading_day(self):
        with patch("scheduler.market_utils.is_trading_day", return_value=False):
            result = pr._rerun_all_symbols(INTRADAY_0DTE_5MIN, date(2025, 7, 4))
        self.assertFalse(result["ok"])
        self.assertIn("not a trading day", result["error"])

    def test_symbols_for_job_filters_0dte_stocks_to_fridays(self):
        symbols = [
            {"symbol": "SPY", "asset_type": "index"},
            {"symbol": "AAPL", "asset_type": "stock"},
        ]
        db = MagicMock()
        with patch("app.models.symbols_config.find_all_active", return_value=symbols):
            wed = pr._symbols_for_job(db, INTRADAY_0DTE_5MIN, date(2025, 6, 18))
            fri = pr._symbols_for_job(db, INTRADAY_0DTE_5MIN, date(2025, 6, 20))
        self.assertEqual([s["symbol"] for s in wed], ["SPY"])
        self.assertEqual(sorted(s["symbol"] for s in fri), ["AAPL", "SPY"])

    def test_legacy_eod_manual_request_for_nifty_routes_to_provider_job(self):
        client = MagicMock()
        client.get_default_database.return_value = MagicMock()
        symbol_doc = {
            "symbol": "NIFTY", "asset_type": "index", "provider": "zerodha"
        }
        expected = {"ok": True, "job": ZERODHA_NIFTY_EOD}
        with patch("pymongo.MongoClient", return_value=client), \
             patch("app.models.symbols_config.find_by_symbol", return_value=symbol_doc), \
             patch.object(pr, "rerun_job", return_value=expected) as dispatch:
            result = pr._rerun_single_symbol(EOD_ROLLING_5D_WEEKLY, "NIFTY", None)
        self.assertEqual(result, expected)
        dispatch.assert_called_once_with(
            ZERODHA_NIFTY_EOD, symbol="NIFTY", trade_date=None
        )
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
