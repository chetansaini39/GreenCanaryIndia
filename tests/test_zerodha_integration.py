from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models import platform_settings, symbols_config
from app.models.db import market_db_for_symbol, validate_distinct_market_databases
from app.services import eod_charts
from data_sources import zerodha_client
from scheduler.jobs import gex_collection


def test_dual_database_configuration_must_be_distinct():
    with pytest.raises(ValueError, match="distinct"):
        validate_distinct_market_databases(
            "mongodb://127.0.0.1:27017/retailgex",
            "mongodb://127.0.0.1:27017/retailgex",
        )
    with pytest.raises(ValueError, match="distinct"):
        validate_distinct_market_databases(
            "mongodb://localhost:27017/retailgex",
            "mongodb://127.0.0.1:27017/retailgex",
        )
    validate_distinct_market_databases(
        "mongodb://127.0.0.1:27017/retailgex",
        "mongodb://127.0.0.1:27017/retailgex_zerodha",
    )


def test_provider_database_routing_preserves_schwab_default():
    primary = object()
    secondary = object()
    assert market_db_for_symbol(primary, {}) is primary
    assert market_db_for_symbol(primary, {"provider": "schwab"}) is primary
    with patch("app.models.db.zerodha_market_db", return_value=secondary) as resolve:
        assert market_db_for_symbol(
            primary, {"provider": "zerodha"}, zerodha_uri="mongodb://x/nifty"
        ) is secondary
    resolve.assert_called_once_with("mongodb://x/nifty")


def test_term_structure_keeps_legacy_us_shape_and_scopes_expiry_index():
    expiry = date(2026, 9, 18)
    options = [{
        "type": "C", "strike": 4_500, "expiry": expiry,
        "gamma": 0.001, "open_interest": 10,
    }]

    us_panel = gex_collection._build_term_structure(
        options, 4_500, [expiry]
    )[0]
    assert "expiry_index" not in us_panel
    assert us_panel["expiry_date"].tzinfo == gex_collection.CT

    nifty_options = [{
        **options[0],
        "strike": 25_000,
        "reference_price": 25_020,
        "contract_multiplier": 65,
    }]
    nifty_panel = gex_collection._build_term_structure(
        nifty_options,
        25_000,
        [expiry],
        timezone_name="Asia/Kolkata",
        include_expiry_index=True,
    )[0]
    assert nifty_panel["expiry_index"] == 1
    assert nifty_panel["expiry_date"].tzinfo == ZoneInfo("Asia/Kolkata")


def test_nifty_symbol_metadata_is_paid_and_black76():
    assert symbols_config.NIFTY_CONFIG["tier"] == "paid"
    assert symbols_config.NIFTY_CONFIG["provider"] == "zerodha"
    assert symbols_config.NIFTY_CONFIG["market_timezone"] == "Asia/Kolkata"
    assert symbols_config.NIFTY_CONFIG["pricing_model"] == "black76"
    assert symbols_config.NIFTY_CONFIG["currency"] == "INR"
    assert symbols_config.NIFTY_CONFIG["display_unit"] == "crore"


def test_nifty_eod_chart_uses_rupee_crore_without_changing_raw_values():
    chart = eod_charts.gex_by_strike_chart(
        "NIFTY",
        25_000,
        [{
            "strike": 25_000,
            "call_gex": 20_000_000,
            "put_gex": -10_000_000,
            "call_greeks": {"open_interest": 10},
            "put_greeks": {"open_interest": 5},
        }],
        currency="INR",
        display_unit="crore",
    )
    assert chart["data"][0]["y"][0] == pytest.approx(1.0)
    assert chart["layout"]["yaxis"]["title"]["text"] == "Gamma Exposure (₹ Cr)"


def test_zerodha_automation_defaults_are_all_off():
    db = MagicMock()
    db.__getitem__.return_value.find_one.return_value = None
    settings = platform_settings.get_zerodha_settings(db)
    assert settings["zerodha_automation_enabled"] is False
    assert settings["zerodha_ws_enabled"] is False
    assert settings["zerodha_intraday_enabled"] is False
    assert settings["zerodha_eod_enabled"] is False
    assert settings["zerodha_monthly_enabled"] is False
    assert settings["zerodha_eod_times"] == ["09:30", "15:10"]


def test_actual_expiry_and_monthly_expiry_are_instrument_driven():
    today = date.today()
    expiries = [today + timedelta(days=n) for n in (2, 9, 16, 35, 42)]
    rows = [
        {"exchange": "NFO", "name": "NIFTY", "instrument_type": "CE",
         "expiry": expiry}
        for expiry in expiries
    ]
    with patch.object(zerodha_client, "_nifty_derivatives", return_value=rows):
        assert zerodha_client.available_option_expiries(from_date=today) == expiries
        monthly = zerodha_client.monthly_option_expiries(from_date=today)
    expected = {}
    for expiry in expiries:
        expected[(expiry.year, expiry.month)] = expiry
    assert monthly == sorted(expected.values())


def test_option_chain_rolls_past_expired_same_day_contracts():
    before_close = datetime(2026, 9, 8, 15, 29, tzinfo=ZoneInfo("Asia/Kolkata"))
    at_close = datetime(2026, 9, 8, 15, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert zerodha_client._active_expiry_start(before_close) == date(2026, 9, 8)
    assert zerodha_client._active_expiry_start(at_close) == date(2026, 9, 9)


def test_option_chain_uses_spot_future_quotes_and_actual_lot_size():
    today = date.today()
    expiry = today + timedelta(days=7)
    future_expiry = today + timedelta(days=28)
    spot = {
        "exchange": "NSE", "tradingsymbol": "NIFTY 50",
        "instrument_token": 1,
    }
    future = {
        "exchange": "NFO", "name": "NIFTY", "instrument_type": "FUT",
        "tradingsymbol": "NIFTY-FUT", "instrument_token": 2,
        "expiry": future_expiry, "strike": 0, "lot_size": 65,
    }
    call = {
        "exchange": "NFO", "name": "NIFTY", "instrument_type": "CE",
        "tradingsymbol": "NIFTY-CE", "instrument_token": 3,
        "expiry": expiry, "strike": 25_000, "lot_size": 65,
    }
    put = {
        "exchange": "NFO", "name": "NIFTY", "instrument_type": "PE",
        "tradingsymbol": "NIFTY-PE", "instrument_token": 4,
        "expiry": expiry, "strike": 25_000, "lot_size": 65,
    }
    all_rows = [spot, future, call, put]
    quotes = {
        "NSE:NIFTY 50": {"last_price": 25_000},
        "NFO:NIFTY-FUT": {"last_price": 25_020},
        "NFO:NIFTY-CE": {"last_price": 210, "oi": 100, "volume": 25},
        "NFO:NIFTY-PE": {"last_price": 190, "oi": 120, "volume": 30},
    }

    def selected_quotes(rows):
        return {
            f"{row['exchange']}:{row['tradingsymbol']}":
                quotes[f"{row['exchange']}:{row['tradingsymbol']}"]
            for row in rows
        }

    with patch.object(zerodha_client, "get_instruments", return_value=all_rows), \
         patch.object(zerodha_client, "_nifty_derivatives",
                      return_value=[future, call, put]), \
         patch.object(zerodha_client, "available_option_expiries",
                      return_value=[expiry]), \
         patch.object(zerodha_client, "get_instrument_quotes",
                      side_effect=selected_quotes):
        chain = zerodha_client.get_option_chain("NIFTY", expiry_date=expiry)

    assert chain["spot_price"] == 25_000
    assert chain["futures_price"] == 25_020
    assert len(chain["options"]) == 2
    assert all(row["contract_multiplier"] == 65 for row in chain["options"])
    assert all(row["reference_price"] == 25_020 for row in chain["options"])
    assert all(row["gamma"] is not None for row in chain["options"])
    assert {row["tradingsymbol"] for row in chain["options"]} == {
        "NIFTY-CE", "NIFTY-PE"
    }
    assert chain["data_quality"]["status"] == "complete"


def test_unknown_gamma_excludes_whole_strike_pair_and_records_quality():
    expiry = date(2026, 9, 8)
    rows = [
        {"type": "C", "strike": 26_400.0, "expiry": expiry,
         "gamma": None, "open_interest": 100},
        {"type": "P", "strike": 26_400.0, "expiry": expiry,
         "gamma": 0.001, "open_interest": 20},
        {"type": "C", "strike": 23_800.0, "expiry": expiry,
         "gamma": 0.002, "open_interest": 50},
        {"type": "P", "strike": 23_800.0, "expiry": expiry,
         "gamma": 0.002, "open_interest": 60},
    ]

    kept, quality = zerodha_client._validated_gex_contracts(rows)

    assert {row["strike"] for row in kept} == {23_800.0}
    assert quality == {
        "status": "partial",
        "total_contracts": 4,
        "included_contracts": 2,
        "unknown_gamma_contracts": 1,
        "excluded_contracts": 2,
        "excluded_strikes": 1,
        "excluded_open_interest": 120,
        "reason": "canonical IV unavailable or rejected by the wing-quality filter",
        "by_expiry": {
            "2026-09-08": {
                "status": "partial",
                "total_contracts": 4,
                "included_contracts": 2,
                "unknown_gamma_contracts": 1,
                "excluded_contracts": 2,
                "excluded_strikes": 1,
                "excluded_open_interest": 120,
            }
        },
    }


def test_unknown_gamma_still_fails_when_no_validated_open_interest_remains():
    expiry = date(2026, 9, 8)
    with pytest.raises(RuntimeError, match="No validated non-zero-OI"):
        zerodha_client._validated_gex_contracts([
            {"type": "C", "strike": 26_400.0, "expiry": expiry,
             "gamma": None, "open_interest": 100},
            {"type": "P", "strike": 26_400.0, "expiry": expiry,
             "gamma": 0.0, "open_interest": 0},
        ])


def test_quote_lookup_uses_fresh_websocket_then_rest_for_missing_tokens():
    rows = [
        {"exchange": "NFO", "tradingsymbol": "A", "instrument_token": 1},
        {"exchange": "NFO", "tradingsymbol": "B", "instrument_token": 2},
    ]
    with patch("data_sources.zerodha_stream.cached_quote",
               side_effect=[{"last_price": 100}, None]) as cached, \
         patch.object(zerodha_client, "get_quotes",
                      return_value={"NFO:B": {"last_price": 200}}) as rest:
        quotes = zerodha_client.get_instrument_quotes(rows)
    assert quotes == {
        "NFO:A": {"last_price": 100},
        "NFO:B": {"last_price": 200},
    }
    assert cached.call_count == 2
    rest.assert_called_once_with(["NFO:B"])


def test_margin_adapter_exposes_estimates_only():
    client = MagicMock()
    client.order_margins.return_value = {"total": 123}
    with patch.object(zerodha_client, "_kite_client", return_value=client):
        result = zerodha_client.estimate_margins([{"exchange": "NFO"}])
    assert result == {"total": 123}
    client.order_margins.assert_called_once()
    assert not hasattr(zerodha_client, "place_order")
    assert not hasattr(zerodha_client, "modify_order")
    assert not hasattr(zerodha_client, "cancel_order")


def test_nifty_job_writes_market_data_secondary_and_health_primary():
    primary = object()
    secondary = object()
    expiry = date(2026, 9, 3)
    now = datetime(2026, 9, 1, 15, 10, tzinfo=ZoneInfo("Asia/Kolkata"))
    options = [{
        "type": "C", "strike": 25_000, "expiry": expiry,
        "gamma": 0.001, "open_interest": 10,
        "reference_price": 25_020, "contract_multiplier": 65,
    }]
    chain = {
        "options": options,
        "spot_price": 25_000,
        "futures_price": 25_020,
        "futures_by_expiry": {expiry.isoformat(): 25_020},
    }
    symbol_doc = dict(symbols_config.NIFTY_CONFIG)

    with patch.object(gex_collection, "_get_db", return_value=primary), \
         patch.object(gex_collection, "_zerodha_job_enabled",
                      return_value=(True, {"zerodha_weekly_forward_expiries": 1})), \
         patch("scheduler.india_market_utils.now_ist", return_value=now), \
         patch("scheduler.india_market_utils.is_nse_trading_day", return_value=True), \
         patch.object(gex_collection, "_nifty_symbol", return_value=symbol_doc), \
         patch.object(gex_collection, "_fetch_chain", return_value=(chain, "zerodha")), \
         patch.object(gex_collection, "market_db_for_symbol", return_value=secondary) as route, \
         patch.object(gex_collection.db_writer, "write_rolling_21d") as rolling, \
         patch.object(gex_collection.db_writer, "write_weekly") as weekly, \
         patch.object(gex_collection, "_log_health") as health:
        gex_collection.run_zerodha_eod(manual=True)

    route.assert_called_once_with(primary, symbol_doc)
    assert rolling.call_args.args[0] is secondary
    assert weekly.call_args.args[0] is secondary
    assert health.call_args.args[0] is primary
    assert health.call_args.args[2] == "success"
