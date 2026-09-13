"""Unit tests for Schwab client parameter building and normalisation."""
from datetime import date, datetime, timedelta

import pytest
from schwab.client import Client

from data_sources import schwab_client


def test_schwab_symbol_maps_cash_settled_indices():
    assert schwab_client._schwab_symbol("SPX") == "$SPX"
    assert schwab_client._schwab_symbol("NDX") == "$NDX"
    assert schwab_client._schwab_symbol("SPY") == "SPY"


def test_build_option_chain_kwargs_spx_uses_single_strategy():
    kwargs = schwab_client._build_option_chain_kwargs("SPX", asset_type="index")
    assert kwargs["strategy"] == Client.Options.Strategy.SINGLE
    assert kwargs["strike_count"] == 25
    assert kwargs["interval"] == 5.0
    assert kwargs["strike_range"] == Client.Options.StrikeRange.NEAR_THE_MONEY
    assert kwargs["entitlement"] == Client.Options.Entitlement.PAYING_PRO
    assert kwargs["contract_type"] == Client.Options.ContractType.ALL
    assert kwargs["to_date"] - kwargs["from_date"] == timedelta(days=21)


def test_build_option_chain_kwargs_ndx_uses_single_strategy():
    kwargs = schwab_client._build_option_chain_kwargs("NDX", asset_type="index")
    assert kwargs["strategy"] == Client.Options.Strategy.SINGLE


def test_build_option_chain_kwargs_etf_index_uses_shorter_window():
    kwargs = schwab_client._build_option_chain_kwargs("SPY", asset_type="index")
    assert kwargs["strategy"] == Client.Options.Strategy.ANALYTICAL
    assert kwargs["strike_count"] == 25
    assert kwargs["to_date"] - kwargs["from_date"] == timedelta(days=14)


def test_build_option_chain_kwargs_stock_uses_analytical_strategy():
    kwargs = schwab_client._build_option_chain_kwargs("AAPL", asset_type="stock")
    assert kwargs["strategy"] == Client.Options.Strategy.ANALYTICAL
    assert kwargs["strike_count"] == 35
    # Widened to cover the full 12-week weekly window (12th Friday ≤ 83 days out).
    assert kwargs["to_date"] - kwargs["from_date"] == timedelta(days=90)


def test_build_option_chain_kwargs_explicit_expiry():
    expiry = date(2026, 6, 20)
    kwargs = schwab_client._build_option_chain_kwargs("SPX", expiry)
    assert kwargs["from_date"] == datetime.combine(expiry, datetime.min.time())
    assert kwargs["to_date"] == kwargs["from_date"] + timedelta(days=1)


def test_normalise_chain_parses_spx_style_response():
    raw = {
        "underlyingPrice": 5432.1,
        "callExpDateMap": {
            "2026-06-20:1": {
                "5400.0": [{
                    "gamma": 0.0023,
                    "delta": 0.49,
                    "theta": -1.2,
                    "vega": 0.8,
                    "volatility": 14.52,
                    "openInterest": 1200,
                }],
            },
        },
        "putExpDateMap": {
            "2026-06-20:1": {
                "5400.0": [{
                    "gamma": 0.0021,
                    "delta": -0.48,
                    "theta": -1.1,
                    "vega": 0.75,
                    "volatility": 15.1,
                    "openInterest": 900,
                }],
            },
        },
    }

    result = schwab_client._normalise_chain(raw, "SPX")
    assert result["spot_price"] == 5432.1
    assert len(result["options"]) == 2
    call = next(o for o in result["options"] if o["type"] == "C")
    put = next(o for o in result["options"] if o["type"] == "P")
    assert call["strike"] == 5400.0
    assert call["expiry"] == date(2026, 6, 20)
    assert call["gamma"] == pytest.approx(0.0023)
    assert call["iv"] == pytest.approx(0.1452)
    assert put["open_interest"] == 900


def test_normalise_chain_raises_on_api_errors():
    with pytest.raises(ValueError, match="errors"):
        schwab_client._normalise_chain({"errors": ["bad request"]}, "SPX")


def test_normalise_chain_raises_on_empty_chain():
    with pytest.raises(ValueError, match="no option contracts"):
        schwab_client._normalise_chain(
            {"underlyingPrice": 0.0, "callExpDateMap": {}, "putExpDateMap": {}},
            "SPX",
        )
