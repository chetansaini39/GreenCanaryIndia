from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.models import symbols_config
from tests.conftest import login_as


def test_nifty_is_paid_and_exposes_market_metadata(client):
    login_as(client, "free")
    with patch("app.models.symbols_config.find_all_active", return_value=[]):
        symbols = client.get("/api/symbols").get_json()["symbols"]
    nifty = next(row for row in symbols if row["symbol"] == "NIFTY")
    assert nifty["locked"] is True
    assert nifty["provider"] == "zerodha"
    assert nifty["market_timezone"] == "Asia/Kolkata"
    assert nifty["currency"] == "INR"
    assert nifty["display_unit"] == "crore"
    assert client.get("/api/gex?symbol=NIFTY&type=0dte").status_code == 403


def test_existing_us_symbol_and_snapshot_response_shapes_remain_legacy(client):
    login_as(client, "paid")
    spy = {"symbol": "SPY", "tier": "free", "asset_type": "index"}
    with patch("app.models.symbols_config.find_all_active",
               return_value=[spy, symbols_config.NIFTY_CONFIG]):
        catalog = client.get("/api/symbols").get_json()["symbols"]

    spy_response = next(row for row in catalog if row["symbol"] == "SPY")
    assert set(spy_response) == {"symbol", "asset_type", "tier", "locked"}

    with patch("app.models.symbols_config.find_by_symbol", return_value=spy), \
         patch("app.routes.api._fetch_snapshot", return_value=None), \
         patch("app.routes.api._last_known_update", return_value=None):
        response = client.get(
            "/api/gex?symbol=SPY&type=weekly&date=2026-09-04"
        )

    assert response.status_code == 200
    assert "market_metadata" not in response.get_json()


def test_paid_nifty_snapshot_reads_provider_database_and_serializes_ist(client):
    login_as(client, "paid")
    secondary = object()
    created_utc = datetime(2026, 9, 1, 9, 40, tzinfo=ZoneInfo("UTC"))
    doc = {
        "symbol": "NIFTY",
        "timestamp": created_utc,
        "created_at": created_utc,
        "net_gex": 2_500_000_000,
        "spot_price": 25_000,
        "gex_by_strike": [],
    }
    with patch("app.models.symbols_config.find_by_symbol",
               return_value={**symbols_config.NIFTY_CONFIG, "active": True}), \
         patch("app.routes.api.market_db_for_symbol", return_value=secondary) as route, \
         patch("app.models.gex_intraday.find_by_symbol_date_range",
               return_value=[doc]):
        response = client.get("/api/gex?symbol=NIFTY&type=0dte&date=2026-09-01")

    assert response.status_code == 200
    payload = response.get_json()
    route.assert_called()
    assert all(call.args[0] is not secondary for call in route.call_args_list)
    assert payload["market_metadata"]["provider"] == "zerodha"
    assert payload["market_metadata"]["currency"] == "INR"
    assert payload["data"]["timestamp"].startswith("2026-09-01T15:10:00")


def test_nifty_term_structure_uses_next_actual_weekly_expiries(client):
    login_as(client, "paid")
    secondary = object()
    expiries = [
        datetime(2026, 9, day, tzinfo=ZoneInfo("Asia/Kolkata"))
        for day in (3, 10, 17)
    ]
    docs = [
        {
            "expiry_date": expiry,
            "spot_price": 25_000,
            "created_at": datetime(2026, 9, 1, 15, 10,
                                   tzinfo=ZoneInfo("Asia/Kolkata")),
            "net_gex": index * 10_000_000,
            "call_wall": 25_200,
            "put_wall": 24_800,
            "gex_by_strike": [{"strike": 25_000, "call_gex": 1, "put_gex": -1}],
            "futures_price": 25_020 + index,
        }
        for index, expiry in enumerate(expiries, start=1)
    ]
    with patch("app.models.symbols_config.find_by_symbol",
               return_value={**symbols_config.NIFTY_CONFIG, "active": True}), \
         patch("app.routes.api.market_db_for_symbol", return_value=secondary), \
         patch("app.models.gex_intraday.find_latest", return_value=None), \
         patch("app.models.gex_weekly.find_latest_upcoming", return_value=docs) as find, \
         patch("app.models.platform_settings.get_term_structure_strike_range_pct",
               return_value=7):
        response = client.get("/api/gex/term-structure?symbol=NIFTY")

    assert response.status_code == 200
    payload = response.get_json()
    find.assert_called_once()
    assert [row["expiry_index"] for row in payload["term_structure"]] == [1, 2, 3]
    assert [row["expiry_date"][:10] for row in payload["term_structure"]] == [
        "2026-09-03", "2026-09-10", "2026-09-17"
    ]


def test_nifty_term_structure_prefers_fresher_intraday_panels(client):
    login_as(client, "paid")
    secondary = object()
    old = datetime(2026, 9, 3, 15, 10, tzinfo=ZoneInfo("Asia/Kolkata"))
    fresh = datetime.now(ZoneInfo("Asia/Kolkata"))
    future_expiries = [fresh.date() + timedelta(days=offset) for offset in (1, 8, 15)]
    weekly_docs = [
        {
            "expiry_date": datetime.combine(
                expiry, datetime.min.time(), tzinfo=ZoneInfo("Asia/Kolkata")
            ),
            "spot_price": 25_000,
            "created_at": old,
            "net_gex": 1,
            "gex_by_strike": [{"strike": 25_000}],
        }
        for expiry in future_expiries
    ]
    intraday = {
        "spot_price": 25_100,
        "created_at": fresh,
        "term_structure": [
            {
                "expiry_date": weekly_docs[index]["expiry_date"],
                "net_gex": 100 + index,
                "gex_by_strike": [{"strike": 25_100}],
            }
            for index in range(2)
        ],
    }
    with patch("app.models.symbols_config.find_by_symbol",
               return_value={**symbols_config.NIFTY_CONFIG, "active": True}), \
         patch("app.routes.api.market_db_for_symbol", return_value=secondary), \
         patch("app.models.gex_intraday.find_latest", return_value=intraday), \
         patch("app.models.gex_weekly.find_latest_upcoming", return_value=weekly_docs), \
         patch("app.models.platform_settings.get_term_structure_strike_range_pct",
               return_value=7):
        response = client.get("/api/gex/term-structure?symbol=NIFTY")

    payload = response.get_json()
    assert response.status_code == 200
    assert len(payload["term_structure"]) == 3
    assert [row["net_gex"] for row in payload["term_structure"]] == [100, 101, 1]
    assert payload["last_updated"].startswith(fresh.date().isoformat())


def test_admin_dashboard_gets_paid_data_panels_without_upgrade_gate(client):
    login_as(client, "admin")
    with patch("app.models.users.find_by_id",
               return_value={"email_verified": True}), \
         patch("app.models.platform_settings.get_0dte_interval_minutes",
               return_value=15):
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'id="ts-panels"' in response.data
    assert b'id="trend-chart"' in response.data
    assert b'id="greeks-chart"' in response.data
    assert b">Paid feature<" not in response.data
    assert b"state.provider === 'zerodha' && !state.dateExplicit" in response.data
    assert response.data.count(b"shouldUseLatestWeeklySnapshot()") == 3


def test_nifty_parallel_chart_without_explicit_date_uses_latest_weekly_capture(client):
    login_as(client, "admin")
    secondary = object()
    doc = {
        "symbol": "NIFTY",
        "trade_date": datetime(2026, 9, 3, tzinfo=ZoneInfo("Asia/Kolkata")),
        "created_at": datetime(2026, 9, 3, 15, 10,
                               tzinfo=ZoneInfo("Asia/Kolkata")),
        "spot_price": 25_000,
        "options_slice": [
            {"type": "C", "strike": 25_000, "gamma": 0.001,
             "open_interest": 10, "gex": 1_000_000},
            {"type": "P", "strike": 25_000, "gamma": 0.001,
             "open_interest": 10, "gex": -1_000_000},
        ],
    }
    with patch("app.models.symbols_config.find_by_symbol",
               return_value={**symbols_config.NIFTY_CONFIG, "active": True}), \
         patch("app.routes.api.market_db_for_symbol", return_value=secondary), \
         patch("app.models.gex_weekly.find_latest_for_symbol",
               return_value=doc) as latest:
        response = client.get(
            "/api/gex/parallel-chart?symbol=NIFTY&type=weekly"
        )

    assert response.status_code == 200
    assert response.get_json()["chart"] is not None
    latest.assert_called_once_with(secondary, "NIFTY")


def test_admin_nifty_weekly_without_date_reads_latest_snapshot(client):
    login_as(client, "admin")
    secondary = object()
    doc = {
        "symbol": "NIFTY",
        "trade_date": datetime(2026, 9, 2, tzinfo=ZoneInfo("Asia/Kolkata")),
        "expiry_date": datetime(2026, 9, 3, tzinfo=ZoneInfo("Asia/Kolkata")),
        "created_at": datetime(2026, 9, 2, 11, 43,
                               tzinfo=ZoneInfo("Asia/Kolkata")),
        "spot_price": 23_813.85,
        "net_gex": 1_000_000_000,
        "gex_by_strike": [{"strike": 23_800, "call_gex": 2, "put_gex": -1}],
    }
    with patch("app.models.symbols_config.find_by_symbol",
               return_value={**symbols_config.NIFTY_CONFIG, "active": True}), \
         patch("app.routes.api.market_db_for_symbol", return_value=secondary), \
         patch("app.models.gex_weekly.find_latest_for_symbol",
               return_value=doc) as latest:
        response = client.get("/api/gex?symbol=NIFTY&type=weekly")

    assert response.status_code == 200
    assert response.get_json()["data"]["spot_price"] == 23_813.85
    latest.assert_called_once_with(secondary, "NIFTY")


def test_margin_diagnostic_is_admin_only_and_estimate_only(client):
    order_json = (
        '[{"exchange":"NFO","tradingsymbol":"NIFTY26SEP25000CE",'
        '"transaction_type":"SELL","quantity":65}]'
    )
    login_as(client, "staff")
    assert client.post(
        "/admin/pipeline/zerodha/margins", data={"orders_json": order_json}
    ).status_code == 403

    login_as(client, "admin")
    with patch("data_sources.zerodha_client.estimate_margins",
               return_value={"total": 100}) as estimate:
        response = client.post(
            "/admin/pipeline/zerodha/margins", data={"orders_json": order_json}
        )
    assert response.status_code == 302
    estimate.assert_called_once()
