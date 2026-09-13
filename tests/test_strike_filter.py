"""Tests for strike-range filtering."""
from datetime import date

import gex_engine
from data_sources.strike_filter import (
    INDEX_STRIKE_BAND,
    STOCK_STRIKE_BAND,
    filter_chain,
    filter_options,
    strike_band,
    strike_bounds,
    trim_gex_result,
)


def _opt(strike: float, opt_type: str = "C") -> dict:
    return {
        "type": opt_type,
        "strike": strike,
        "expiry": date(2026, 6, 20),
        "gamma": 0.01,
        "delta": 0.5,
        "theta": -0.1,
        "vega": 0.2,
        "iv": 0.25,
        "open_interest": 100,
    }


def test_strike_band_by_asset_type():
    assert strike_band("index") == INDEX_STRIKE_BAND
    assert strike_band("stock") == STOCK_STRIKE_BAND


def test_filter_options_index_20_percent():
    spot = 1000.0
    options = [_opt(700), _opt(850), _opt(1000), _opt(1150), _opt(1300)]
    kept = filter_options(options, spot, "index")
    assert [o["strike"] for o in kept] == [850.0, 1000.0, 1150.0]


def test_filter_options_stock_50_percent():
    spot = 200.0
    options = [_opt(110), _opt(150), _opt(250), _opt(310)]
    kept = filter_options(options, spot, "stock")
    assert [o["strike"] for o in kept] == [110.0, 150.0, 250.0]


def test_filter_chain_preserves_spot():
    chain = {"spot_price": 500.0, "options": [_opt(400), _opt(600)]}
    result = filter_chain(chain, "index")
    assert result["spot_price"] == 500.0
    assert len(result["options"]) == 2


def test_gex_compute_uses_pre_filtered_options():
    spot = 100.0
    lo, hi = strike_bounds(spot, "index")
    options = [_opt(lo), _opt(hi), _opt(hi + 50)]
    filtered = filter_options(options, spot, "index")
    result = gex_engine.compute(filtered, spot)
    strikes = [row["strike"] for row in result["gex_by_strike"]]
    assert hi + 50 not in strikes
    assert len(result["options_slice"]) == len(filtered)


def test_trim_gex_result_recomputes_summary():
    spot = 100.0
    gex_result = {
        "net_gex": 999.0,
        "call_wall": 200.0,
        "put_wall": 50.0,
        "gex_by_strike": [
            {"strike": 50.0, "call_gex": 1.0, "put_gex": -2.0,
             "call_greeks": {"open_interest": 10}, "put_greeks": {"open_interest": 5}},
            {"strike": 95.0, "call_gex": 5.0, "put_gex": -1.0,
             "call_greeks": {"open_interest": 20}, "put_greeks": {"open_interest": 8}},
            {"strike": 150.0, "call_gex": 0.5, "put_gex": -0.5,
             "call_greeks": {"open_interest": 1}, "put_greeks": {"open_interest": 1}},
        ],
        "gex_by_expiry_strike": [
            {"expiry": "2026-06-20", "strike": 50.0, "gex": -1.0},
            {"expiry": "2026-06-20", "strike": 95.0, "gex": 4.0},
            {"expiry": "2026-06-20", "strike": 150.0, "gex": 0.0},
        ],
        "options_slice": [],
        "gex_by_expiration": [],
    }
    trimmed = trim_gex_result(gex_result, spot, "index")
    assert [r["strike"] for r in trimmed["gex_by_strike"]] == [95.0]
    assert trimmed["call_wall"] == 95.0
    assert trimmed["net_gex"] == 4.0
