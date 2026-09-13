"""Tests for OHLCV history service."""
from unittest.mock import patch

from app.services.ohlcv_history import fetch_ohlcv_history


def test_fetch_ohlcv_history_routes_nifty_to_zerodha():
    rows = [{
        "date": "2026-09-01", "open": 25000.0, "high": 25100.0,
        "low": 24900.0, "close": 25050.0, "volume": 100,
    }]
    with patch("data_sources.zerodha_client.get_historical_bars",
               return_value=rows) as fetch:
        result = fetch_ohlcv_history("NIFTY", days=1, provider="zerodha")
    assert result == rows
    fetch.assert_called_once()


def test_fetch_ohlcv_history_returns_empty_for_unsupported_provider():
    """Only Zerodha is supported in this fork — anything else is a no-op."""
    with patch("data_sources.zerodha_client.get_historical_bars") as fetch:
        result = fetch_ohlcv_history("SPY", days=5, provider="schwab")
    assert result == []
    fetch.assert_not_called()
