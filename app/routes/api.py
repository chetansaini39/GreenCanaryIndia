"""
API endpoints consumed by the dashboard frontend (Module 04).

Authorization is enforced server-side on every request — the frontend
dropdown/selector is purely cosmetic; a crafted request still gets blocked.
"""
import logging
import re
from datetime import date, datetime, time, timedelta
import os
from zoneinfo import ZoneInfo

from bson import ObjectId
from flask import Blueprint, current_app, has_app_context, jsonify, request, session

from app.extensions import mongo, limiter
from app.models import gex_intraday, gex_weekly, gex_monthly_opex, gex_rolling_21d, symbols_config, platform_settings
from app.models.db import market_db_for_symbol
from app.utils.time import market_midnight

log = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__, url_prefix="/api")

CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))

# ── Tier / symbol constants ────────────────────────────────────────────────

FREE_SYMBOLS = {"SPY", "QQQ", "TSLA", "NVDA"}

# Symbols that get index-style granularity options (0DTE / monthly etc.)
INDEX_SYMBOLS = {"SPY", "QQQ", "SPX", "NDX", "RUT", "VIX", "IWM", "NIFTY"}

# Allowed granularity types per tier/asset
FREE_INDEX_TYPES  = {"0dte", "intraday", "weekly"}  # monthly is paid-only entirely
FREE_STOCK_TYPES  = {"weekly", "intraday", "0dte"}
ALL_TYPES         = {"0dte", "weekly", "monthly", "intraday"}

# Hardcoded fallback catalog — used when symbols_config collection is empty
# US symbols were removed with the Schwab/CBOE/yfinance pipeline in this
# India-only fork. Keep in sync with scripts/seed_symbols.py DEFAULT_SYMBOLS.
_DEFAULT_CATALOG = [
    symbols_config.NIFTY_CONFIG,
]


# ── Auth helpers ───────────────────────────────────────────────────────────

def _authed() -> bool:
    return "user_id" in session


def _is_paid() -> bool:
    return session.get("role") == "paid"


def _has_full_data_access(role: str | None = None) -> bool:
    """Paid users plus staff/admin (support troubleshooting).

    Accepts an explicit `role` so non-Flask-session callers (the MCP server,
    Module 11) can reuse this same check for their API-key-linked user's role
    instead of duplicating the tier logic. Defaults to the current session.
    """
    if role is None:
        role = session.get("role")
    return role in ("paid", "staff", "admin")


def _require_auth():
    """Return a 401 JSON response if the request has no valid session."""
    if not _authed():
        return jsonify({"error": "authentication required"}), 401
    return None


# ── Symbol / tier authorization ────────────────────────────────────────────

def _load_catalog(db=None) -> list[dict]:
    """Load symbol list from DB; fall back to defaults if the collection is empty."""
    if db is None:
        db = mongo.db
    try:
        rows = symbols_config.find_all_active(db)
        if rows:
            return [dict(r) for r in rows]
    except Exception:
        log.warning("Could not load symbols_config from DB; using defaults")
    return _DEFAULT_CATALOG


def _symbol_doc(symbol: str, db=None) -> dict:
    primary_db = db if db is not None else mongo.db
    try:
        row = symbols_config.find_by_symbol(primary_db, symbol)
        if row:
            return row
    except Exception:
        pass
    return next(
        (row for row in _DEFAULT_CATALOG if row["symbol"] == symbol),
        {"symbol": symbol, "asset_type": "index" if symbol in INDEX_SYMBOLS else "stock"},
    )


def _data_db(symbol: str, db=None):
    primary_db = db if db is not None else mongo.db
    zerodha_uri = _zerodha_uri()
    return market_db_for_symbol(
        primary_db, _symbol_doc(symbol, primary_db), zerodha_uri=zerodha_uri
    )


def _zerodha_uri() -> str | None:
    return (
        current_app.config.get("ZERODHA_MONGO_URI")
        if has_app_context() else os.environ.get("ZERODHA_MONGO_URI")
    )


def _asset_type_of(symbol: str) -> str:
    return _symbol_doc(symbol).get("asset_type", "stock")


def _market_timezone_of(symbol: str) -> str:
    return _symbol_doc(symbol).get("market_timezone", str(CT))


def _today_for_symbol(symbol: str) -> date:
    return datetime.now(ZoneInfo(_market_timezone_of(symbol))).date()


def _response_market_metadata(symbol: str) -> dict:
    row = _symbol_doc(symbol)
    return {
        "provider": row.get("provider", "schwab"),
        "market": row.get("market", "us"),
        "market_timezone": row.get("market_timezone", str(CT)),
        "currency": row.get("currency", "USD"),
        "display_unit": row.get("display_unit", "billion"),
        "pricing_model": row.get("pricing_model", "source_greeks"),
    }


def _optional_market_metadata(symbol: str) -> dict:
    """Extend only provider-native responses; keep legacy US schemas exact."""
    if str(_symbol_doc(symbol).get("provider") or "schwab").lower() != "zerodha":
        return {}
    return {"market_metadata": _response_market_metadata(symbol)}


def _check_authorization(symbol: str, snap_type: str, role: str | None = None) -> tuple[bool, str]:
    """
    Enforce server-side tier + granularity rules.
    Returns (allowed, error_reason).

    `role` lets non-session callers (the MCP server) pass the caller's role
    explicitly instead of duplicating this logic — see _has_full_data_access.
    """
    if snap_type not in ALL_TYPES:
        return False, f"unknown snapshot type '{snap_type}'"

    if not _has_full_data_access(role):
        if symbol not in FREE_SYMBOLS:
            return False, "symbol not available on your plan — upgrade to unlock"

        asset_type = _asset_type_of(symbol)
        allowed_types = FREE_INDEX_TYPES if asset_type == "index" else FREE_STOCK_TYPES
        if snap_type not in allowed_types:
            return False, "granularity not available on your plan — upgrade to unlock"

    return True, ""


def _check_symbol_authorization(symbol: str) -> tuple[bool, str]:
    """Symbol-tier check only — no week-distance or granularity check.

    Used exclusively by /api/gex/weekly-comparison and /api/gex/monthly-comparison,
    which are a deliberate carve-out from the next-week-paid-only rule while still
    enforcing the existing free-tier symbol list.  Do NOT reuse this function for
    the original weekly/monthly endpoints.
    """
    if not _has_full_data_access() and symbol not in FREE_SYMBOLS:
        return False, "symbol not available on your plan — upgrade to unlock"
    return True, ""


# ── Free-tier weekly date window ──────────────────────────────────────────

def _free_stock_allowed_week_mondays() -> set[date]:
    """The week_of Monday a free user may access for weekly snapshots (current week only).

    Applies to both stock and index symbols — next week is paid-only regardless.
    Window rolls forward after Friday 3 PM CT.
    """
    now_ct = datetime.now(CT)
    today  = now_ct.date()
    weekday = today.weekday()  # Mon=0 … Sun=6

    # "Rolled" means the current week's expiry has passed
    rolled = (weekday == 4 and now_ct.time() >= time(15, 0)) or weekday >= 5

    if not rolled:
        current_monday = today - timedelta(days=weekday)
    else:
        days_forward = (7 - weekday) % 7 or 7
        current_monday = today + timedelta(days=days_forward)

    return {current_monday}


def _week_monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _nearest_expiry_friday(d: date) -> date:
    """Nearest Friday on/after d, holiday-adjusted (falls back to Thursday)."""
    from scheduler.market_utils import is_trading_day as _is_trading_day
    days_to_friday = (4 - d.weekday()) % 7
    friday = d + timedelta(days=days_to_friday)
    if not _is_trading_day(friday):
        friday -= timedelta(days=1)
    return friday


def _stock_weekly_tabs() -> list[dict]:
    """Ordered 4-tab definitions for the stock weekly card — Past Week, This
    Week, Next Week, Week+2 — each with its resolved (holiday-adjusted) Friday
    expiry date. Computed server-side from Central Time; rolls forward after
    Friday 3 PM CT so a session spanning the boundary sees the shifted window.
    """
    from scheduler.market_utils import next_n_upcoming_fridays, is_trading_day as _is_trading_day
    now_ct = datetime.now(CT)
    today = now_ct.date()
    from_date = today
    if today.weekday() == 4 and now_ct.time() >= time(15, 0):
        from_date = today + timedelta(days=1)

    this_week, next_week, week_plus_2 = next_n_upcoming_fridays(3, from_date=from_date)

    # Past week: the Friday one week before this week's *nominal* Friday, then
    # holiday-adjusted independently. Deriving it from this_week directly would
    # be off by a day whenever this_week itself was holiday-adjusted back to a
    # Thursday (e.g. a July-4-on-Friday week).
    nominal_this_friday = from_date + timedelta(days=(4 - from_date.weekday()) % 7)
    past_week = nominal_this_friday - timedelta(days=7)
    if not _is_trading_day(past_week):
        past_week -= timedelta(days=1)

    return [
        {"key": "past",  "label": "Past Week", "expiry_date": past_week},
        {"key": "this",  "label": "This Week", "expiry_date": this_week},
        {"key": "next",  "label": "Next Week", "expiry_date": next_week},
        {"key": "week2", "label": "Week+2",    "expiry_date": week_plus_2},
    ]


def _free_stock_tab_expiries() -> set[date]:
    """The set of expiry Fridays a free user may access on stock symbols —
    the 4-tab window. Used by the /api/gex authorization check."""
    return {t["expiry_date"] for t in _stock_weekly_tabs()}


# ── Serialization ──────────────────────────────────────────────────────────

def _serialize(obj, timezone_name: str | None = None):
    """Recursively strip ObjectIds and convert datetimes to ISO strings."""
    if isinstance(obj, dict):
        return {k: _serialize(v, timezone_name) for k, v in obj.items() if k != "_id"}
    if isinstance(obj, list):
        return [_serialize(i, timezone_name) for i in obj]
    if isinstance(obj, datetime):
        value = obj.astimezone(ZoneInfo(timezone_name)) if timezone_name else obj
        return value.isoformat()
    if isinstance(obj, ObjectId):
        return str(obj)
    return obj


# ── Data fetchers ──────────────────────────────────────────────────────────

def _fetch_snapshot(symbol: str, snap_type: str, req_date: date, expiry_date: date | None = None,
                    trade_date_given: bool = False, timestamp_str: str | None = None,
                    db=None) -> dict | None:
    """`db` defaults to the Flask-managed mongo.db; the MCP server (Module 11,
    a separate process with its own MongoClient) passes its own db instead."""
    primary_db = db if db is not None else mongo.db
    symbol_doc = _symbol_doc(symbol, primary_db)
    db = market_db_for_symbol(
        primary_db,
        symbol_doc,
        zerodha_uri=_zerodha_uri(),
    )
    timezone_name = symbol_doc.get("market_timezone", str(CT))
    midnight_ct = market_midnight(req_date, timezone_name)

    if snap_type == "weekly":
        # expiry_date selects which tracked week (the 4-tab toggle); date is the
        # trade_date — which day's snapshot within that week to show. When no
        # explicit date is sent (trade_date_given=False), return that week's
        # latest available trade_date. When both are sent, fetch that exact day
        # and fall back to latest if it hasn't been captured yet.
        #
        # Legacy path (no expiry_date): date doubles as both the week selector
        # (nearest Friday from it) and the exact trade_date to fetch.
        if symbol_doc.get("provider") == "zerodha":
            if expiry_date is not None:
                expiry_dt = market_midnight(expiry_date, timezone_name)
                if not trade_date_given:
                    return gex_weekly.find_latest_for_expiry(db, symbol, expiry_dt)
                doc = gex_weekly.find_by_symbol_expiry_trade_date(
                    db, symbol, expiry_dt, midnight_ct
                )
                return doc or gex_weekly.find_latest_for_expiry(db, symbol, expiry_dt)
            if trade_date_given:
                return gex_weekly.find_by_symbol_trade_date(db, symbol, midnight_ct)
            return gex_weekly.find_latest_for_symbol(db, symbol)

        nearest_friday = _nearest_expiry_friday(expiry_date or req_date)
        expiry_dt = datetime(nearest_friday.year, nearest_friday.month, nearest_friday.day, tzinfo=CT)

        if expiry_date is not None and not trade_date_given:
            return gex_weekly.find_latest_for_expiry(db, symbol, expiry_dt)

        trade_date_dt = datetime(req_date.year, req_date.month, req_date.day, tzinfo=CT)
        doc = gex_weekly.find_by_symbol_expiry_trade_date(db, symbol, expiry_dt, trade_date_dt)
        if doc is None:
            # Fallback: latest trade_date for the nearest expiry (e.g. EOD hasn't run yet)
            doc = gex_weekly.find_latest_for_expiry(db, symbol, expiry_dt)
        return doc

    if snap_type == "monthly":
        # date param = trade_date (the Monday or Friday the snapshot was taken).
        # Rollover-aware: on a 3rd-Friday expiry day the scheduler writes a doc
        # with trade_date=today and a rolled-forward expiry_date, so querying by
        # trade_date naturally returns the new cycle's first point.
        doc = gex_monthly_opex.find_by_symbol_trade_date(db, symbol, midnight_ct)
        if doc is None:
            # Fallback: return the most recent available snapshot
            doc = gex_monthly_opex.find_latest_for_symbol(db, symbol)
        return doc

    # 0dte / intraday → gex_intraday collection
    effective_type = "0dte" if snap_type == "intraday" else snap_type
    docs = gex_intraday.find_by_symbol_date_range(
        db, symbol, midnight_ct, midnight_ct, snapshot_type=effective_type
    )
    if not docs:
        return None

    if timestamp_str:
        # timestamp_str is a CT "HH:MM" matching one of the values returned by
        # /api/gex/intraday-snapshots — find the exact snapshot the user clicked.
        for doc in docs:
            ts = doc.get("timestamp")
            if ts and ts.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M") == timestamp_str:
                return doc
        return None

    return docs[-1]


def _last_known_update(symbol: str) -> str | None:
    """Best-effort: return the created_at of the most recent snapshot for this symbol."""
    try:
        latest = gex_intraday.find_latest(_data_db(symbol), symbol)
        if latest:
            return _serialize(latest.get("created_at"), _market_timezone_of(symbol))
    except Exception:
        pass
    return None


# ── GET /api/prices ────────────────────────────────────────────────────────
# Public, no-auth ticker strip for the homepage. Returns the most recent spot
# price per symbol from gex_intraday — never hardcoded. Rate-limited since it's
# an unauthenticated endpoint.

TICKER_SYMBOLS = ["SPY", "QQQ", "SPX", "NDX", "VIX"]


def _intraday_change_pct(db, doc) -> float | None:
    """Percent change of the latest spot vs the day's first captured snapshot.

    Derived only from data we already store (gex_intraday has no prev-close
    field) — so this is an intraday move, not a since-yesterday-close move.
    Returns None when there's nothing to compare against.
    """
    spot = doc.get("spot_price")
    trade_date = doc.get("trade_date")
    if spot in (None, 0) or trade_date is None:
        return None
    day = gex_intraday.find_by_symbol_date(db, doc["symbol"], trade_date)
    if not day:
        return None
    first = day[0].get("spot_price")
    if not first:
        return None
    return round((spot - first) / first * 100, 2)


@api_bp.route("/prices")
@limiter.limit("120 per hour")
def prices():
    from app.routes.main import _is_market_hours_now

    prices_out = []
    latest_ts = None
    for sym in TICKER_SYMBOLS:
        doc = gex_intraday.find_latest(mongo.db, sym)
        spot = doc.get("spot_price") if doc else None
        updated = (doc.get("created_at") if doc else None)
        if updated and (latest_ts is None or updated > latest_ts):
            latest_ts = updated
        prices_out.append({
            "symbol": sym,
            "spot_price": spot,
            "change_pct": _intraday_change_pct(mongo.db, doc) if doc else None,
        })

    return jsonify({
        "prices": prices_out,
        "market_open": _is_market_hours_now(),
        "last_updated": _serialize(latest_ts),
    })


# ── GET /api/pricing ───────────────────────────────────────────────────────
# Public. Pro tier display price, sourced from config (PRO_PRICE env var) so
# the homepage template never hardcodes it.

@api_bp.route("/pricing")
def pricing():
    return jsonify({"pro_price": current_app.config.get("PRO_PRICE")})


# ── GET /api/symbols ───────────────────────────────────────────────────────

@api_bp.route("/symbols")
def symbols():
    if err := _require_auth():
        return err

    is_paid = _has_full_data_access()
    catalog = _load_catalog()

    result = [
        {
            "symbol": entry["symbol"],
            "asset_type": entry.get("asset_type", "stock"),
            "tier": entry["tier"],
            "locked": not is_paid and entry["tier"] == "paid",
            **({
                "provider": "zerodha",
                "market": entry.get("market", "nse"),
                "market_timezone": entry.get("market_timezone", "Asia/Kolkata"),
                "currency": entry.get("currency", "INR"),
                "display_unit": entry.get("display_unit", "crore"),
                "pricing_model": entry.get("pricing_model", "black76"),
            } if str(entry.get("provider") or "schwab").lower() == "zerodha" else {}),
        }
        for entry in catalog
    ]
    return jsonify({"symbols": result})


# ── GET /api/gex ───────────────────────────────────────────────────────────

@api_bp.route("/gex")
def gex():
    if err := _require_auth():
        return err

    symbol      = (request.args.get("symbol")      or "").upper().strip()
    snap_type   = (request.args.get("type")        or "").lower().strip()
    date_str    = (request.args.get("date")        or "").strip()
    expiry_str  = (request.args.get("expiry_date") or "").strip()
    timestamp_str = (request.args.get("timestamp")   or "").strip()

    if not symbol or not snap_type:
        return jsonify({"error": "symbol and type parameters are required"}), 400

    if timestamp_str and not re.fullmatch(r"[0-2]\d:[0-5]\d", timestamp_str):
        message = (
            "invalid timestamp — expected HH:MM in the symbol's market timezone"
            if str(_symbol_doc(symbol).get("provider") or "schwab").lower() == "zerodha"
            else "invalid timestamp — expected HH:MM (24-hour, Central Time)"
        )
        return jsonify({"error": message}), 400

    # ── Server-side authorization ──────────────────────────────────────────
    allowed, reason = _check_authorization(symbol, snap_type)
    if not allowed:
        return jsonify({"error": reason}), 403

    # ── Parse date / expiry_date ────────────────────────────────────────────
    try:
        req_date = date.fromisoformat(date_str) if date_str else _today_for_symbol(symbol)
    except ValueError:
        return jsonify({"error": "invalid date — expected YYYY-MM-DD"}), 400

    expiry_date = None
    if expiry_str:
        try:
            expiry_date = date.fromisoformat(expiry_str)
        except ValueError:
            return jsonify({"error": "invalid expiry_date — expected YYYY-MM-DD"}), 400

    # ── Free weekly: window + latest-day-only checks ──────────────────────
    if not _has_full_data_access() and snap_type == "weekly":
        if _asset_type_of(symbol) == "stock":
            # 4-tab toggle: Past Week / This Week / Next Week / Week+2 — a
            # confirmed override of the old current-week-only rule, stocks only.
            target_expiry = _nearest_expiry_friday(expiry_date or req_date)
            if target_expiry not in _free_stock_tab_expiries():
                return jsonify({"error": "date out of range for your plan"}), 403
        else:
            # Only gate on the week window when a date is *explicitly* requested.
            # With no date, the fetch resolves the nearest upcoming Friday (the
            # current week) from the server's own clock — this is what the free
            # UI sends, and avoids a spurious 403 across the Friday/weekend
            # rollover when the client's "today" maps to the just-passed week.
            if date_str and _week_monday_of(req_date) not in _free_stock_allowed_week_mondays():
                return jsonify({"error": "date out of range for your plan"}), 403
        # Free users may only see the latest available day's snapshot —
        # stepping back to an earlier trade_date within the current week is paid.
        if date_str and req_date < _today_for_symbol(symbol):
            return jsonify({"error": "earlier snapshots within the current week require a paid plan"}), 403

    # ── Intraday: stocks only available on Fridays ─────────────────────────
    if snap_type == "intraday" and _asset_type_of(symbol) == "stock":
        if req_date.weekday() != 4:  # 4 = Friday
            return jsonify({
                "symbol": symbol,
                "type": snap_type,
                "date": req_date.isoformat(),
                "data": None,
                "message": f"Intraday view is only available on Fridays for {symbol}",
            })

    # ── Fetch ──────────────────────────────────────────────────────────────
    doc = _fetch_snapshot(
        symbol, snap_type, req_date, expiry_date,
        trade_date_given=bool(date_str), timestamp_str=timestamp_str or None,
    )

    if doc is None:
        return jsonify({
            "symbol": symbol,
            "type": snap_type,
            "date": req_date.isoformat(),
            "data": None,
            "message": "data not available yet",
            "last_updated": _last_known_update(symbol),
            **_optional_market_metadata(symbol),
        })

    return jsonify({
        "symbol": symbol,
        "type": snap_type,
        "date": req_date.isoformat(),
        "data": _serialize(doc, _market_timezone_of(symbol)),
        "last_updated": _serialize(doc.get("created_at"), _market_timezone_of(symbol)),
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/intraday-snapshots ────────────────────────────────────────
# Lightweight metadata (no gex_by_strike) for the last N intraday snapshots —
# powers the time-box picker row above the 0DTE chart. Same tier/symbol
# authorization as the main intraday endpoint.

MAX_INTRADAY_SNAPSHOTS = 10


@api_bp.route("/gex/intraday-snapshots")
def gex_intraday_snapshots():
    if err := _require_auth():
        return err

    symbol   = (request.args.get("symbol") or "").upper().strip()
    date_str = (request.args.get("date")   or "").strip()
    n_raw    = (request.args.get("n")      or str(MAX_INTRADAY_SNAPSHOTS)).strip()

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    allowed, reason = _check_authorization(symbol, "intraday")
    if not allowed:
        return jsonify({"error": reason}), 403

    try:
        req_date = date.fromisoformat(date_str) if date_str else _today_for_symbol(symbol)
    except ValueError:
        return jsonify({"error": "invalid date — expected YYYY-MM-DD"}), 400

    if not date_str:
        # No explicit date requested (the dashboard's default "give me current"
        # call) — resolve to the most recent trade_date that actually has 0DTE
        # data, so outside market hours / on a weekend the picker still shows
        # the last trading day's boxes instead of an empty "today".
        data_db = _data_db(symbol)
        latest = gex_intraday.find_latest(data_db, symbol, snapshot_type="0dte")
        if latest and latest.get("trade_date"):
            req_date = latest["trade_date"].astimezone(
                ZoneInfo(_market_timezone_of(symbol))
            ).date()

    try:
        n = int(n_raw)
    except ValueError:
        return jsonify({"error": "invalid n — expected an integer"}), 400
    n = max(1, min(n, MAX_INTRADAY_SNAPSHOTS))

    # Stocks only have a same-day expiry on Fridays — no intraday data otherwise.
    if _asset_type_of(symbol) == "stock" and req_date.weekday() != 4:
        return jsonify({
            "symbol": symbol,
            "date": req_date.isoformat(),
            "snapshots": [],
            "message": f"Intraday view is only available on Fridays for {symbol}",
        })

    timezone = ZoneInfo(_market_timezone_of(symbol))
    midnight_ct = market_midnight(req_date, str(timezone))
    data_db = _data_db(symbol)
    docs = gex_intraday.find_last_n_by_symbol_date(data_db, symbol, midnight_ct, "0dte", n)

    snapshots = [
        {
            "timestamp": d["timestamp"].astimezone(timezone).strftime("%H:%M"),
            "net_gex": d.get("net_gex"),
            "call_wall": d.get("call_wall"),
            "put_wall": d.get("put_wall"),
            "spot_price": d.get("spot_price"),
        }
        for d in docs
        if d.get("timestamp") is not None
    ]

    return jsonify({
        "symbol": symbol,
        "date": req_date.isoformat(),
        "snapshots": snapshots,
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/weekly-tabs ──────────────────────────────────────────────
# Tab metadata for the stock weekly 4-tab toggle. Returns each tab's semantic
# label, its server-resolved expiry_date, and whether a gex_weekly snapshot
# exists for it yet — so the frontend renders real dates + disabled state
# without doing any expiry-date math in JavaScript. No per-strike data here.

@api_bp.route("/gex/weekly-tabs")
def gex_weekly_tabs():
    if err := _require_auth():
        return err

    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    # Same free-tier symbol restriction as /api/gex.
    if not _has_full_data_access() and symbol not in FREE_SYMBOLS:
        return jsonify({"error": "symbol not available on your plan — upgrade to unlock"}), 403

    db = _data_db(symbol)
    tabs = []
    for tab in _stock_weekly_tabs():
        exp = tab["expiry_date"]
        expiry_dt = datetime(exp.year, exp.month, exp.day, tzinfo=CT)
        latest = gex_weekly.find_latest_for_expiry(db, symbol, expiry_dt)
        # A doc may exist but hold an empty snapshot (a tracked expiry that had
        # no contracts in the chain yet) — treat that as "no data" so the tab
        # reflects real per-strike availability, not mere row presence.
        has_data = bool(latest and (latest.get("gex_by_strike") or []))
        tabs.append({
            "key": tab["key"],
            "label": tab["label"],
            "expiry_date": exp.isoformat(),
            "has_data": has_data,
            "last_updated": _serialize(latest.get("created_at")) if latest else None,
        })

    return jsonify({"symbol": symbol, "tabs": tabs})


# ── GET /api/gex/rolling ──────────────────────────────────────────────────

@api_bp.route("/gex/rolling")
def gex_rolling():
    if err := _require_auth():
        return err

    if not _has_full_data_access():
        return jsonify({"error": "rolling 21-day series requires a paid plan"}), 403

    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    rows = gex_rolling_21d.find_last_n(_data_db(symbol), symbol, n=21)
    last_updated = _serialize(rows[-1].get("created_at")) if rows else None
    return jsonify({
        "symbol": symbol,
        "series": [_serialize(r, _market_timezone_of(symbol)) for r in rows],
        "last_updated": _serialize(rows[-1].get("created_at"), _market_timezone_of(symbol)) if rows else None,
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/eod ─────────────────────────────────────────────────────

@api_bp.route("/gex/eod")
def gex_eod():
    """EOD GEX Analysis card — per-day strike/expiry charts (mirrors ticker-eod page)."""
    if err := _require_auth():
        return err

    symbol = (request.args.get("symbol") or "").upper().strip()
    date_str = (request.args.get("date") or "").strip()
    days_raw = (request.args.get("days") or "1").strip()

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    if not _has_full_data_access() and symbol not in FREE_SYMBOLS:
        return jsonify({"error": "symbol not available on your plan — upgrade to unlock"}), 403

    try:
        req_date = date.fromisoformat(date_str) if date_str else _today_for_symbol(symbol)
    except ValueError:
        return jsonify({"error": "invalid date — expected YYYY-MM-DD"}), 400

    try:
        num_days = max(1, min(int(days_raw), 13))
    except ValueError:
        num_days = 1

    if not _has_full_data_access():
        num_days = 1
        if _week_monday_of(req_date) not in _free_stock_allowed_week_mondays():
            return jsonify({"error": "date out of range for your plan"}), 403

    from app.services.eod_analysis import build_eod_analysis

    try:
        payload = build_eod_analysis(
            _data_db(symbol),
            symbol,
            req_date,
            num_days=num_days,
            asset_type=_asset_type_of(symbol),
            market_timezone=_market_timezone_of(symbol),
            provider=_symbol_doc(symbol).get("provider", "schwab"),
        )
        payload.update(_optional_market_metadata(symbol))
        return jsonify(_serialize(payload, _market_timezone_of(symbol)))
    except Exception as exc:
        log.exception("gex_eod failed for %s: %s", symbol, exc)
        return jsonify({"error": "failed to build EOD analysis", "days": [], "message": str(exc)}), 500


# ── GET /api/ohlcv ───────────────────────────────────────────────────────

@api_bp.route("/ohlcv")
def ohlcv():
    """Last N daily OHLCV bars for the selected symbol (Zerodha, dashboard card)."""
    if err := _require_auth():
        return err

    symbol = (request.args.get("symbol") or "").upper().strip()
    days_raw = (request.args.get("days") or "12").strip()

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    if not _has_full_data_access() and symbol not in FREE_SYMBOLS:
        return jsonify({"error": "symbol not available on your plan — upgrade to unlock"}), 403

    try:
        num_days = max(1, min(int(days_raw), 45))
    except ValueError:
        num_days = 12

    from app.services.ohlcv_history import fetch_ohlcv_history

    symbol_doc = _symbol_doc(symbol)
    bars = fetch_ohlcv_history(symbol, num_days, provider=symbol_doc.get("provider", "schwab"))
    return jsonify({
        "symbol": symbol,
        "days": num_days,
        "bars": bars,
        "source": "zerodha",
        **_optional_market_metadata(symbol),
        "message": None if bars else "price history not available for this symbol",
    })


# ── GET /api/gex/public ───────────────────────────────────────────────────

@api_bp.route("/gex/public")
@limiter.limit("20 per hour")
def gex_public():
    """No-auth, rate-limited snapshot for Twitter/social sharing links (Module 08)."""
    symbol   = (request.args.get("symbol") or "").upper().strip()
    date_str = (request.args.get("date")   or "").strip()

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    if symbol not in FREE_SYMBOLS:
        return jsonify({"error": "symbol not available on the public endpoint"}), 403

    try:
        req_date = date.fromisoformat(date_str) if date_str else _today_for_symbol(symbol)
    except ValueError:
        return jsonify({"error": "invalid date — expected YYYY-MM-DD"}), 400

    doc = _fetch_snapshot(symbol, "weekly", req_date)

    if doc is None:
        return jsonify({
            "symbol": symbol,
            "date": req_date.isoformat(),
            "data": None,
            "message": "data not available yet",
            "last_updated": None,
        })

    return jsonify({
        "symbol": symbol,
        "date": req_date.isoformat(),
        "data": _serialize(doc),
        "last_updated": _serialize(doc.get("created_at")),
    })


# ── GET /api/gex/greeks ───────────────────────────────────────────────────

@api_bp.route("/gex/greeks")
def gex_greeks():
    """Per-strike greeks for the parallel coordinates view — paid-only."""
    if err := _require_auth():
        return err

    if not _has_full_data_access():
        return jsonify({"error": "per-strike greeks require a paid plan"}), 403

    symbol    = (request.args.get("symbol") or "").upper().strip()
    snap_type = (request.args.get("type")   or "weekly").lower().strip()
    date_str  = (request.args.get("date")   or "").strip()

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    if snap_type not in ALL_TYPES:
        return jsonify({"error": f"unknown snapshot type '{snap_type}'"}), 400

    try:
        req_date = date.fromisoformat(date_str) if date_str else _today_for_symbol(symbol)
    except ValueError:
        return jsonify({"error": "invalid date — expected YYYY-MM-DD"}), 400

    doc = _fetch_snapshot(symbol, snap_type, req_date)
    if doc is None:
        return jsonify({"symbol": symbol, "type": snap_type, "date": req_date.isoformat(),
                        "strikes": [], "message": "data not available"})

    strikes = []
    for row in doc.get("gex_by_strike", []):
        entry = {
            "strike": row.get("strike"),
            "call_gex": row.get("call_gex"),
            "put_gex": row.get("put_gex"),
        }
        for side in ("call_greeks", "put_greeks"):
            g = row.get(side) or {}
            entry[side] = {
                "delta": g.get("delta"),
                "gamma": g.get("gamma"),
                "theta": g.get("theta"),
                "vega": g.get("vega"),
                "open_interest": g.get("open_interest"),
            }
        strikes.append(entry)

    return jsonify({
        "symbol": symbol,
        "type": snap_type,
        "date": req_date.isoformat(),
        "last_updated": _serialize(doc.get("created_at"), _market_timezone_of(symbol)),
        "strikes": strikes,
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/parallel-chart ──────────────────────────────────────────
#
# Returns Plotly JSON for the parallel coordinates chart with optional
# server-side option_type filtering. Called by the dashboard filter toggle.

@api_bp.route("/gex/parallel-chart")
def gex_parallel_chart():
    """Parallel coordinates Plotly JSON with optional Call/Put filter — paid-only.

    Query params:
      symbol      required
      source      chain (default for index) | weekly | monthly (stocks only)
      type        snapshot type: weekly (default) | monthly | 0dte | intraday
      date        YYYY-MM-DD (defaults to today)
      option_type C | P (omit for all)
    """
    if err := _require_auth():
        return err

    if not _has_full_data_access():
        return jsonify({"error": "parallel coordinates require a paid plan"}), 403

    symbol      = (request.args.get("symbol")      or "").upper().strip()
    source      = (request.args.get("source")       or "").lower().strip()
    snap_type   = (request.args.get("type")         or "weekly").lower().strip()
    date_str    = (request.args.get("date")         or "").strip()
    expiry_str  = (request.args.get("expiry_date")  or "").strip()
    option_type = (request.args.get("option_type")  or "").upper().strip() or None

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    if snap_type not in ALL_TYPES:
        return jsonify({"error": f"unknown snapshot type '{snap_type}'"}), 400

    if option_type and option_type not in ("C", "P"):
        return jsonify({"error": "option_type must be C or P"}), 400

    try:
        req_date = date.fromisoformat(date_str) if date_str else _today_for_symbol(symbol)
    except ValueError:
        return jsonify({"error": "invalid date — expected YYYY-MM-DD"}), 400

    expiry_date = None
    if expiry_str:
        try:
            expiry_date = date.fromisoformat(expiry_str)
        except ValueError:
            return jsonify({"error": "invalid expiry_date — expected YYYY-MM-DD"}), 400

    asset_type = _asset_type_of(symbol)

    if asset_type == "index" and source in ("weekly", "monthly"):
        return jsonify({"error": "source=weekly/monthly does not apply to index symbols; use source=chain or omit"}), 400

    from app.charts.contracts import contracts_for_snapshot, contracts_for_multi_expiry_docs
    from app.charts.parallel_coords import parallel_coords_chart_json

    if asset_type == "stock" and source in ("weekly", "monthly"):
        db = _data_db(symbol)
        if source == "weekly":
            docs = gex_weekly.find_recent(db, symbol, n=12)
        else:
            docs = gex_monthly_opex.find_recent_cycles(db, symbol, n=3)
        if not docs:
            return jsonify({"chart": None, "message": "data not available"})
        spot = float(docs[0].get("spot_price") or 0)
        contracts = contracts_for_multi_expiry_docs(docs)
    else:
        # expiry_date selects which tracked week (the stock weekly 4-tab toggle);
        # without it, fall back to the nearest-Friday week for req_date.
        doc = _fetch_snapshot(symbol, snap_type, req_date, expiry_date,
                              trade_date_given=bool(date_str))
        if doc is None:
            return jsonify({"chart": None, "message": "data not available"})
        spot = float(doc.get("spot_price") or 0)
        contracts = contracts_for_snapshot(doc)

    if option_type:
        contracts = [c for c in contracts if c.get("type") == option_type]

    metadata = _response_market_metadata(symbol)
    chart = parallel_coords_chart_json(
        spot,
        contracts,
        option_type=option_type,
        currency=metadata["currency"],
        display_unit=metadata["display_unit"],
    )
    return jsonify({
        "chart": chart,
        "message": None if chart else "not enough contract data to build chart",
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/contracts ───────────────────────────────────────────────
#
# Richer per-contract rows (options_slice) for the parallel coordinates chart.
# Returns one row per raw option contract, preserving per-expiry detail that
# the aggregated /api/gex/greeks endpoint collapses away.

@api_bp.route("/gex/contracts")
def gex_contracts():
    """Per-contract option rows for the parallel coordinates view — paid-only.

    Query params:
      symbol      required
      source      chain (default for index) | weekly | monthly (stocks only)
      type        snapshot type: weekly (default) | monthly | 0dte | intraday
      date        YYYY-MM-DD (defaults to today)
      option_type C | P (omit for all)
    """
    if err := _require_auth():
        return err

    if not _has_full_data_access():
        return jsonify({"error": "per-contract data requires a paid plan"}), 403

    symbol      = (request.args.get("symbol")      or "").upper().strip()
    source      = (request.args.get("source")       or "").lower().strip()
    snap_type   = (request.args.get("type")         or "weekly").lower().strip()
    date_str    = (request.args.get("date")         or "").strip()
    option_type = (request.args.get("option_type")  or "").upper().strip()

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    if snap_type not in ALL_TYPES:
        return jsonify({"error": f"unknown snapshot type '{snap_type}'"}), 400

    if option_type and option_type not in ("C", "P"):
        return jsonify({"error": "option_type must be C or P"}), 400

    try:
        req_date = date.fromisoformat(date_str) if date_str else _today_for_symbol(symbol)
    except ValueError:
        return jsonify({"error": "invalid date — expected YYYY-MM-DD"}), 400

    asset_type = _asset_type_of(symbol)

    if asset_type == "index" and source in ("weekly", "monthly"):
        return jsonify({"error": "source=weekly/monthly does not apply to index symbols; use source=chain or omit"}), 400

    from app.charts.contracts import contracts_for_snapshot, contracts_for_multi_expiry_docs

    if asset_type == "stock" and source in ("weekly", "monthly"):
        db = _data_db(symbol)
        if source == "weekly":
            docs = gex_weekly.find_recent(db, symbol, n=12)
        else:
            docs = gex_monthly_opex.find_recent_cycles(db, symbol, n=3)
        if not docs:
            return jsonify({"symbol": symbol, "source": source, "contracts": [],
                            "message": "data not available"})
        spot = float(docs[0].get("spot_price") or 0)
        last_updated = _serialize(docs[0].get("created_at"))
        contracts = contracts_for_multi_expiry_docs(docs)
        if option_type:
            contracts = [c for c in contracts if c.get("type") == option_type]
        expiry_dates = sorted({c["expiry"] for c in contracts if c.get("expiry")})
        return jsonify({
            "symbol": symbol,
            "source": source,
            "spot_price": spot,
            "last_updated": last_updated,
            "option_type_filter": option_type or None,
            "expiry_dates": expiry_dates,
            "contracts": contracts,
            **_optional_market_metadata(symbol),
        })

    doc = _fetch_snapshot(symbol, snap_type, req_date)
    if doc is None:
        return jsonify({"symbol": symbol, "type": snap_type, "date": req_date.isoformat(),
                        "contracts": [], "message": "data not available"})

    spot = float(doc.get("spot_price") or 0)
    contracts = contracts_for_snapshot(doc)

    if option_type:
        contracts = [c for c in contracts if c.get("type") == option_type]

    return jsonify({
        "symbol": symbol,
        "source": "chain",
        "type": snap_type,
        "date": req_date.isoformat(),
        "spot_price": spot,
        "last_updated": _serialize(doc.get("created_at"), _market_timezone_of(symbol)),
        "option_type_filter": option_type or None,
        "contracts": contracts,
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/term-structure ───────────────────────────────────────────

# Index symbols that support term structure (feature is index-only)
_TERM_STRUCTURE_SYMBOLS = {"SPX", "NDX", "SPY", "QQQ", "IWM", "NIFTY"}


def _latest_term_structure(symbol: str, primary_db=None) -> tuple[dict | None, list[dict]]:
    """Return the source snapshot and normalized next-three term panels."""
    primary_db = primary_db if primary_db is not None else mongo.db
    symbol_doc = _symbol_doc(symbol, primary_db)
    data_db = market_db_for_symbol(
        primary_db, symbol_doc, zerodha_uri=_zerodha_uri()
    )
    if symbol_doc.get("provider") == "zerodha":
        timezone_name = symbol_doc.get("market_timezone", "Asia/Kolkata")
        timezone = ZoneInfo(timezone_name)
        now = datetime.now(timezone)
        active_expiry = (
            now.date() + timedelta(days=1)
            if now.time() >= time(15, 30)
            else now.date()
        )
        docs = gex_weekly.find_latest_upcoming(
            data_db, symbol, market_midnight(active_expiry, timezone_name), n=3
        )
        latest_intraday = gex_intraday.find_latest(
            data_db, symbol, snapshot_type="0dte"
        )

        def recency(doc):
            value = (doc or {}).get("created_at") or (doc or {}).get("timestamp")
            if not isinstance(value, datetime):
                return datetime.min.replace(tzinfo=ZoneInfo("UTC"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo("UTC"))
            return value.astimezone(ZoneInfo("UTC"))

        candidates = {}
        for doc in docs:
            expiry_value = doc.get("expiry_date")
            if not expiry_value:
                continue
            expiry = (
                expiry_value.astimezone(timezone).date()
                if isinstance(expiry_value, datetime)
                else date.fromisoformat(str(expiry_value)[:10])
            )
            candidates[expiry] = ({
                "expiry_date": doc.get("expiry_date"),
                "net_gex": doc.get("net_gex"),
                "call_wall": doc.get("call_wall"),
                "put_wall": doc.get("put_wall"),
                "gex_by_strike": doc.get("gex_by_strike") or [],
                "futures_price": doc.get("futures_price"),
            }, doc)

        for panel in ((latest_intraday or {}).get("term_structure") or []):
            expiry_value = panel.get("expiry_date")
            if not expiry_value:
                continue
            expiry = (
                expiry_value.astimezone(timezone).date()
                if isinstance(expiry_value, datetime)
                else date.fromisoformat(str(expiry_value)[:10])
            )
            if expiry < active_expiry:
                continue
            existing = candidates.get(expiry)
            if existing is None or recency(latest_intraday) >= recency(existing[1]):
                candidates[expiry] = (dict(panel), latest_intraday)

        selected = [candidates[expiry] for expiry in sorted(candidates)[:3]]
        panels = []
        for index, (panel, _) in enumerate(selected, start=1):
            panel["expiry_index"] = index
            panel["trading_days_out"] = index
            panels.append(panel)
        source = max((doc for _, doc in selected), key=recency, default=None)
        return source, panels

    latest = gex_intraday.find_latest(data_db, symbol, snapshot_type="0dte")
    return latest, (latest.get("term_structure") or [] if latest else [])


@api_bp.route("/gex/term-structure")
def gex_term_structure():
    """Next-3-trading-days GEX term structure — paid-only, index-only.

    Returns the term_structure array from the most recent gex_intraday document
    for the given symbol, so the card always shows the latest available data
    whether or not the market is currently open.
    """
    if err := _require_auth():
        return err

    if not _has_full_data_access():
        return jsonify({"error": "term structure requires a paid plan"}), 403

    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    if symbol not in _TERM_STRUCTURE_SYMBOLS:
        return jsonify({
            "error": f"term structure is only available for index symbols ({', '.join(sorted(_TERM_STRUCTURE_SYMBOLS))})"
        }), 400

    latest, ts = _latest_term_structure(symbol)
    if latest is None:
        return jsonify({
            "symbol": symbol,
            "term_structure": [],
            "message": "no term-structure data available yet",
            "last_updated": None,
            **_optional_market_metadata(symbol),
        })

    # Strike range filter — only strikes within ±N% of spot are returned to the
    # client. Filtering happens server-side (Module 03/04): the full per-strike
    # array must never be sent over the wire when this filter is active.
    range_pct = platform_settings.get_term_structure_strike_range_pct(mongo.db)
    spot = latest.get("spot_price")
    ts = _filter_term_structure_strikes(ts, spot, range_pct)

    return jsonify({
        "symbol": symbol,
        "term_structure": _serialize(ts, _market_timezone_of(symbol)),
        "strike_range_pct": range_pct,
        "last_updated": _serialize(latest.get("created_at"), _market_timezone_of(symbol)),
        **_optional_market_metadata(symbol),
    })


def _filter_term_structure_strikes(term_structure, spot_price, range_pct):
    """Return a copy of the term_structure array with each panel's
    gex_by_strike narrowed to strikes within ±range_pct% of spot_price.

    If spot_price is missing or range_pct is non-positive, the data is
    returned unfiltered rather than dropping every strike.
    """
    if not spot_price or not range_pct or range_pct <= 0:
        return term_structure

    band = spot_price * (range_pct / 100.0)
    low, high = spot_price - band, spot_price + band

    filtered = []
    for panel in term_structure:
        panel_copy = dict(panel)
        strikes = panel.get("gex_by_strike") or []
        panel_copy["gex_by_strike"] = [
            s for s in strikes
            if s.get("strike") is not None and low <= s["strike"] <= high
        ]
        filtered.append(panel_copy)
    return filtered


# ── GET /api/gex/weekly-trend ─────────────────────────────────────────────
# Free + paid. Returns net_gex/call_wall/put_wall per tracked expiry week —
# never gex_by_strike. Query is server-side narrow (projection) so no
# per-strike data ever reaches the response body.

@api_bp.route("/gex/weekly-trend")
def gex_weekly_trend():
    if err := _require_auth():
        return err

    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    # Symbol access check — still enforced even though the endpoint is tier-open
    if not _has_full_data_access() and symbol not in FREE_SYMBOLS:
        return jsonify({"error": "symbol not available on your plan — upgrade to unlock"}), 403

    db = _data_db(symbol)
    timezone = ZoneInfo(_market_timezone_of(symbol))
    today = datetime.now(timezone).date()
    trade_date_dt = market_midnight(today, str(timezone))

    rows = gex_weekly.find_trend_for_trade_date(db, symbol, trade_date_dt)

    # If nothing for today yet, return the latest available trade_date's summary
    if not rows:
        latest_doc = gex_weekly.find_by_symbol_trade_date(db, symbol, trade_date_dt)
        if latest_doc is None:
            # Fall back to any available data
            recent = gex_weekly.find_recent(db, symbol, n=1)
            if recent:
                td = recent[0].get("trade_date")
                if td:
                    rows = gex_weekly.find_trend_for_trade_date(db, symbol, td)

    last_updated = _serialize(rows[-1].get("created_at")) if rows else None
    return jsonify({
        "symbol": symbol,
        "weeks": [
            {
                "expiry_date": _serialize(r.get("expiry_date"), _market_timezone_of(symbol)),
                "net_gex": r.get("net_gex"),
                "call_wall": r.get("call_wall"),
                "put_wall": r.get("put_wall"),
            }
            for r in rows
        ],
        "last_updated": last_updated,
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/monthly-trend ────────────────────────────────────────────
# Free + paid. Returns net_gex/call_wall/put_wall per tracked OPEX cycle —
# never gex_by_strike. Same narrow-query principle as weekly-trend.

@api_bp.route("/gex/monthly-trend")
def gex_monthly_trend():
    if err := _require_auth():
        return err

    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    if not _has_full_data_access() and symbol not in FREE_SYMBOLS:
        return jsonify({"error": "symbol not available on your plan — upgrade to unlock"}), 403

    db = _data_db(symbol)
    timezone = ZoneInfo(_market_timezone_of(symbol))
    today = datetime.now(timezone).date()
    trade_date_dt = market_midnight(today, str(timezone))

    rows = gex_monthly_opex.find_trend_for_trade_date(db, symbol, trade_date_dt)

    # Fall back to latest available Monday/Friday capture if today has no data
    if not rows:
        latest_doc = gex_monthly_opex.find_latest_for_symbol(db, symbol)
        if latest_doc:
            td = latest_doc.get("trade_date")
            if td:
                rows = gex_monthly_opex.find_trend_for_trade_date(db, symbol, td)

    last_updated = _serialize(rows[-1].get("created_at")) if rows else None
    return jsonify({
        "symbol": symbol,
        "cycles": [
            {
                "expiry_date": _serialize(r.get("expiry_date"), _market_timezone_of(symbol)),
                "net_gex": r.get("net_gex"),
                "call_wall": r.get("call_wall"),
                "put_wall": r.get("put_wall"),
            }
            for r in rows
        ],
        "last_updated": last_updated,
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/weekly-comparison ───────────────────────────────────────────
# Free + paid, symbol-restricted.  Returns full per-strike detail (gex_by_strike
# included) for each of the N tracked upcoming expiry weeks — one snapshot per
# week (latest available trade_date per expiry).
#
# Authorization: symbol-tier check ONLY via _check_symbol_authorization().
# The week-distance check from _check_authorization() is deliberately bypassed
# here — this is a confirmed spec carve-out, not an oversight.  Do NOT reuse
# or call _check_authorization() from this handler.

@api_bp.route("/gex/weekly-comparison")
def gex_weekly_comparison():
    if err := _require_auth():
        return err

    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    allowed, reason = _check_symbol_authorization(symbol)
    if not allowed:
        return jsonify({"error": reason}), 403

    docs = gex_weekly.find_recent(_data_db(symbol), symbol, n=12)
    last_updated = _serialize(docs[0].get("created_at")) if docs else None
    return jsonify({
        "symbol": symbol,
        "weeks": [_serialize(d, _market_timezone_of(symbol)) for d in docs],
        "last_updated": last_updated,
        **_optional_market_metadata(symbol),
    })


# ── GET /api/gex/monthly-comparison ──────────────────────────────────────────
# Same pattern as weekly-comparison for the M tracked monthly OPEX cycles.
# Authorization: symbol-tier check ONLY via _check_symbol_authorization().

@api_bp.route("/gex/monthly-comparison")
def gex_monthly_comparison():
    if err := _require_auth():
        return err

    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    allowed, reason = _check_symbol_authorization(symbol)
    if not allowed:
        return jsonify({"error": reason}), 403

    docs = gex_monthly_opex.find_recent_cycles(_data_db(symbol), symbol, n=3)
    last_updated = _serialize(docs[0].get("created_at")) if docs else None
    return jsonify({
        "symbol": symbol,
        "cycles": [_serialize(d, _market_timezone_of(symbol)) for d in docs],
        "last_updated": last_updated,
        **_optional_market_metadata(symbol),
    })
