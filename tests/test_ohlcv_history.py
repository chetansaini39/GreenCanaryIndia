"""Tests for OHLCV history service."""
from unittest.mock import MagicMock, patch

import pandas as pd

from app.services.ohlcv_history import fetch_ohlcv_history


def _sample_df():
    idx = pd.to_datetime(["2025-06-10", "2025-06-11", "2025-06-12"])
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [105.0, 106.0, 107.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [104.0, 105.0, 106.0],
            "Volume": [1_000_000, 1_100_000, 1_200_000],
        },
        index=idx,
    )


@patch("yfinance.Ticker")
def test_fetch_ohlcv_history_returns_last_n_bars(mock_ticker_cls):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _sample_df()
    mock_ticker_cls.return_value = mock_ticker

    bars = fetch_ohlcv_history("NVDA", days=2)

    assert len(bars) == 2
    assert bars[0]["date"] == "2025-06-11"
    assert bars[1]["date"] == "2025-06-12"
    assert bars[1]["close"] == 106.0
    assert bars[1]["volume"] == 1_200_000


@patch("yfinance.Ticker")
def test_fetch_ohlcv_history_maps_spx_symbol(mock_ticker_cls):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _sample_df()
    mock_ticker_cls.return_value = mock_ticker

    fetch_ohlcv_history("SPX", days=3)

    mock_ticker_cls.assert_called_once_with("^GSPC")


def test_fetch_ohlcv_history_routes_nifty_to_zerodha():
    rows = [{
        "date": "2026-09-01", "open": 25000.0, "high": 25100.0,
        "low": 24900.0, "close": 25050.0, "volume": 100,
    }]
    with patch("data_sources.zerodha_client.get_historical_bars",
               return_value=rows) as fetch, \
         patch("yfinance.Ticker") as yfinance:
        result = fetch_ohlcv_history("NIFTY", days=1, provider="zerodha")
    assert result == rows
    fetch.assert_called_once()
    yfinance.assert_not_called()
