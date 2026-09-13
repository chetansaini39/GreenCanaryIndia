import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import gex_engine
from data_sources.black76 import Black76Calculator, quote_price, time_to_expiry


IST = ZoneInfo("Asia/Kolkata")


def test_black76_matches_reference_fixture():
    calculator = Black76Calculator(0.055)
    futures = 25_000.0
    strike = 25_100.0
    years = 3.5 / 365
    volatility = 0.18

    call_price = calculator.price(futures, strike, years, volatility, "C")
    put_price = calculator.price(futures, strike, years, volatility, "P")
    call = calculator.greeks(futures, strike, years, volatility, "C")
    put = calculator.greeks(futures, strike, years, volatility, "P")

    assert call_price == pytest.approx(130.57517165364618, rel=1e-12)
    assert put_price == pytest.approx(230.5224458325659, rel=1e-12)
    assert call["delta"] == pytest.approx(0.4136255479859932, rel=1e-12)
    assert call["gamma"] == pytest.approx(0.000883675030207801, rel=1e-12)
    assert call["theta"] == pytest.approx(-35.4763017714803, rel=1e-12)
    assert call["vega"] == pytest.approx(9.532795702584156, rel=1e-12)
    assert call["rho"] == pytest.approx(-0.012520906870897582, rel=1e-12)
    assert put["delta"] == pytest.approx(-0.5858471938032221, rel=1e-12)
    assert calculator.implied_volatility(
        call_price, futures, strike, years, "C"
    ) == pytest.approx(volatility, abs=1e-8)


def test_time_to_expiry_uses_1530_ist_and_one_minute_floor():
    expiry = date(2026, 9, 3)
    snapshot = datetime(2026, 9, 3, 15, 29, 30, tzinfo=IST)
    assert time_to_expiry(expiry, snapshot) == pytest.approx(1 / (365 * 24 * 60))
    assert math.isnan(
        time_to_expiry(expiry, datetime(2026, 9, 3, 15, 31, tzinfo=IST))
    )


def test_quote_price_selection_order():
    weighted, source = quote_price({
        "depth": {
            "buy": [{"price": 90, "quantity": 300}],
            "sell": [{"price": 110, "quantity": 100}],
        },
        "last_price": 101,
    })
    assert source == "weighted_mid"
    assert weighted == pytest.approx(105.0)

    midpoint, source = quote_price({
        "depth": {
            "buy": [{"price": 99, "quantity": 1}],
            "sell": [{"price": 101, "quantity": 1}],
        }
    })
    assert (midpoint, source) == (100.0, "mid")
    assert quote_price({"last_price": 98.5}) == (98.5, "ltp")


def test_nifty_gex_uses_futures_and_actual_lot_size():
    expiry = date(2026, 9, 3)
    options = [
        {
            "type": "C", "strike": 25_000, "expiry": expiry,
            "gamma": 0.001, "open_interest": 10,
            "reference_price": 25_000, "contract_multiplier": 65,
        },
        {
            "type": "P", "strike": 24_900, "expiry": expiry,
            "gamma": 0.002, "open_interest": 5,
            "reference_price": 25_000, "contract_multiplier": 65,
        },
    ]
    result = gex_engine.compute(options, 24_950)
    assert result["gex_by_strike"][0]["put_gex"] == pytest.approx(-4_062_500)
    assert result["gex_by_strike"][1]["call_gex"] == pytest.approx(4_062_500)
    assert result["net_gex"] == pytest.approx(0)


def test_us_gex_defaults_are_unchanged_and_unknown_gamma_is_not_zeroed():
    option = {"type": "C", "strike": 100, "gamma": 0.01, "open_interest": 100}
    assert gex_engine.compute([option], 100)["net_gex"] == pytest.approx(10_000)

    with pytest.raises(ValueError, match="Missing gamma"):
        gex_engine.compute([{"type": "P", "strike": 100, "gamma": None,
                             "open_interest": 1}], 100)
    zero = gex_engine.compute([{"type": "P", "strike": 100, "gamma": None,
                                "open_interest": 0}], 100)
    assert zero["net_gex"] == 0


def test_us_options_slice_keeps_legacy_output_shape():
    option = {
        "type": "C",
        "strike": 100,
        "expiry": "2026-09-18",
        "gamma": 0.01,
        "delta": 0.5,
        "theta": -0.2,
        "vega": 0.1,
        "iv": 0.25,
        "open_interest": 100,
        "volume": 25,
    }
    row = gex_engine.compute([option], 100)["options_slice"][0]
    assert set(row) == {
        "type", "strike", "expiry", "gex", "gamma", "delta", "theta",
        "vega", "iv", "open_interest", "volume",
    }
