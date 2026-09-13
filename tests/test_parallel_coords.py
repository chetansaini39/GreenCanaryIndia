"""Tests for EOD parallel-coordinates chart modules."""
from datetime import date

from app.charts.contracts import (
    contracts_for_multi_expiry_docs,
    contracts_for_snapshot,
    expiry_detail_for_snapshot,
    filter_near_spot,
    from_gex_by_strike,
    from_options_slice,
    gex_by_expiration_from_contracts,
    normalize_contract,
)
from app.charts.parallel_coords import build_parallel_coords_figure, parallel_coords_chart_json


def _sample_options_slice():
    spot = 400.0
    rows = []
    for strike in range(360, 441, 5):
        for opt_type in ("C", "P"):
            rows.append({
                "type": opt_type,
                "strike": float(strike),
                "expiry": "2026-06-20",
                "gex": 1e8 if opt_type == "C" else -8e7,
                "gamma": 0.02,
                "delta": 0.5 if opt_type == "C" else -0.5,
                "iv": 0.35,
                "open_interest": 1000 + strike,
                "volume": 50,
            })
    return spot, rows


def test_normalize_contract_maps_fields():
    row = normalize_contract({
        "type": "C",
        "strike": 400,
        "expiry": "2026-06-20",
        "gex": 1.5e8,
        "iv": 0.3,
        "open_interest": 500,
    })
    assert row["type"] == "C"
    assert row["strike"] == 400.0
    assert row["GEX"] == 1.5e8
    assert row["open_interest"] == 500


def test_filter_near_spot():
    contracts = [{"strike": 300}, {"strike": 400}, {"strike": 500}]
    filtered = filter_near_spot(contracts, spot=400.0, band=0.20)
    assert len(filtered) == 1
    assert filtered[0]["strike"] == 400


def test_from_gex_by_strike_fallback():
    rows = [{
        "strike": 400.0,
        "call_gex": 2e8,
        "put_gex": -1e8,
        "call_greeks": {"open_interest": 100, "gamma": 0.01, "delta": 0.5, "iv": 0.25},
        "put_greeks": {"open_interest": 80, "gamma": 0.01, "delta": -0.5, "iv": 0.28},
    }]
    contracts = from_gex_by_strike(rows)
    assert len(contracts) == 2
    assert {c["type"] for c in contracts} == {"C", "P"}


def test_parallel_coords_chart_from_options_slice():
    spot, rows = _sample_options_slice()
    contracts = from_options_slice(rows)
    chart = parallel_coords_chart_json(spot, contracts)
    assert chart is not None
    assert "data" in chart
    assert chart["data"][0]["type"] == "parcoords"


def test_parallel_coords_nifty_uses_crore_units():
    spot, rows = _sample_options_slice()
    contracts = from_options_slice(rows)
    chart = parallel_coords_chart_json(
        spot, contracts, currency="INR", display_unit="crore"
    )
    labels = [dimension["label"] for dimension in chart["data"][0]["dimensions"]]
    assert "GEX (₹ Cr)" in labels


def test_parallel_coords_from_snapshot_without_options_slice():
    spot, rows = _sample_options_slice()
    contracts = from_options_slice(rows)
    gex_by_strike = []
    strike_buckets = {}
    for c in contracts:
        key = c["strike"]
        if key not in strike_buckets:
            strike_buckets[key] = {
                "strike": key,
                "call_gex": 0.0,
                "put_gex": 0.0,
                "call_greeks": {"open_interest": 0, "gamma": 0.01, "delta": 0.5, "iv": 0.25},
                "put_greeks": {"open_interest": 0, "gamma": 0.01, "delta": -0.5, "iv": 0.28},
            }
        if c["type"] == "C":
            strike_buckets[key]["call_gex"] += c["GEX"]
            strike_buckets[key]["call_greeks"]["open_interest"] += c["open_interest"]
        else:
            strike_buckets[key]["put_gex"] += c["GEX"]
            strike_buckets[key]["put_greeks"]["open_interest"] += c["open_interest"]
    gex_by_strike = list(strike_buckets.values())

    snap = {"spot_price": spot, "gex_by_strike": gex_by_strike}
    rebuilt = contracts_for_snapshot(snap)
    fig = build_parallel_coords_figure(spot, rebuilt)
    assert fig is not None


def test_gex_by_expiration_from_options_slice():
    spot, rows = _sample_options_slice()
    contracts = from_options_slice(rows)
    by_exp = gex_by_expiration_from_contracts(contracts)
    assert len(by_exp) == 1
    assert by_exp[0]["expiry"] == "2026-06-20"
    assert by_exp[0]["net_gex"] != 0


def test_expiry_detail_weekly_fallback():
    snap = {
        "expiry_date": "2026-06-20",
        "gex_by_strike": [
            {"strike": 400.0, "call_gex": 2e8, "put_gex": -1e8},
            {"strike": 410.0, "call_gex": 1e8, "put_gex": -5e7},
        ],
    }
    by_exp, by_es = expiry_detail_for_snapshot(snap)
    assert len(by_exp) == 1
    assert by_exp[0]["expiry"] == "2026-06-20"
    assert by_exp[0]["net_gex"] == 1.5e8
    assert len(by_es) == 2


# ── Multi-expiry (source=weekly / source=monthly) fix — spec matrix rows 27/28 ──


def _make_weekly_doc(expiry: str, strikes=(400.0, 410.0)):
    """Simulate one gex_weekly document for a single expiry week."""
    return {
        "symbol": "TSLA",
        "expiry_date": expiry,
        "spot_price": 400.0,
        "gex_by_strike": [
            {"strike": s, "call_gex": 1e8, "put_gex": -5e7,
             "call_greeks": {"open_interest": 100}, "put_greeks": {"open_interest": 80}}
            for s in strikes
        ],
    }


def test_contracts_for_multi_expiry_docs_multiple_expiries():
    """spec row 27: source=weekly on a stock must yield multiple distinct expiry_date values."""
    expiries = ["2026-06-27", "2026-07-04", "2026-07-11"]
    docs = [_make_weekly_doc(exp) for exp in expiries]

    contracts = contracts_for_multi_expiry_docs(docs)

    found_expiries = {c["expiry"] for c in contracts if c.get("expiry")}
    assert len(found_expiries) > 1, (
        "multi-expiry source=weekly must return contracts from multiple distinct expiry dates; "
        f"got: {found_expiries}"
    )
    assert found_expiries == set(expiries)


def test_contracts_for_multi_expiry_docs_with_options_slice():
    """source=weekly with options_slice in docs (expiry already embedded per contract)."""
    docs = [
        {
            "symbol": "TSLA",
            "expiry_date": "2026-06-27",
            "spot_price": 400.0,
            "options_slice": [
                {"type": "C", "strike": 400.0, "expiry": "2026-06-27",
                 "gex": 1e8, "open_interest": 100, "volume": 50},
            ],
        },
        {
            "symbol": "TSLA",
            "expiry_date": "2026-07-04",
            "spot_price": 400.0,
            "options_slice": [
                {"type": "P", "strike": 395.0, "expiry": "2026-07-04",
                 "gex": -8e7, "open_interest": 80, "volume": 30},
            ],
        },
    ]
    contracts = contracts_for_multi_expiry_docs(docs)
    found_expiries = {c["expiry"] for c in contracts if c.get("expiry")}
    assert found_expiries == {"2026-06-27", "2026-07-04"}


def test_contracts_for_multi_expiry_docs_monthly_cycles():
    """spec row 28: source=monthly on a stock must yield up to 3 distinct expiry_date values."""
    expiries = ["2026-06-20", "2026-07-18", "2026-08-15"]
    docs = [_make_weekly_doc(exp) for exp in expiries]

    contracts = contracts_for_multi_expiry_docs(docs)
    found_expiries = {c["expiry"] for c in contracts if c.get("expiry")}
    assert len(found_expiries) == 3
    assert found_expiries == set(expiries)
