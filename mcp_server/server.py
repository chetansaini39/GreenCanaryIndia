"""RetailGex MCP server — external agent access to GEX data (Module 11).

Separate process from the Flask web app and the scheduler. Streamable HTTP
transport on port 5010, mounted at /mcp.

Run directly:
    python mcp_server/server.py
or:
    python -m mcp_server.server
"""
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mcp_server")

CT = ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago"))

mcp = FastMCP("RetailGex")


def _db():
    from mcp_server.db import get_db
    return get_db()


def _log_call(tool_name: str, status: str, detail: str) -> None:
    """Mirror scheduler/admin job logging so MCP usage shows up in the admin
    pipeline-health view (Module 11 implementation notes)."""
    try:
        from app.models import pipeline_health
        pipeline_health.insert_one(
            _db(), {"job_name": f"mcp_tool_{tool_name}", "status": status, "detail": detail}
        )
    except Exception:
        log.exception("failed to log mcp tool call %s", tool_name)


def _parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    return date.fromisoformat(value)


def _midnight_ct(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=CT)


def _market_context(symbol: str):
    """Resolve metadata in primary Mongo and market data in the provider store."""
    from app.routes.api import _data_db, _symbol_doc

    primary_db = _db()
    symbol_doc = _symbol_doc(symbol, primary_db)
    market_db = _data_db(symbol, primary_db)
    timezone_name = symbol_doc.get("market_timezone", str(CT))
    return primary_db, market_db, symbol_doc, timezone_name


def _market_midnight(d: date, timezone_name: str) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(timezone_name))


def _parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


# ── Data tools (read-only) ──────────────────────────────────────────────────

@mcp.tool()
def list_symbols(tier: str | None = None) -> dict:
    """Return available symbols. If tier is omitted, returns symbols for the
    caller's own tier; pass "free" or "paid" to see a specific tier's list."""
    from mcp_server.auth import authorize
    from app.routes.api import _load_catalog, _has_full_data_access

    user = authorize()
    db = _db()
    catalog = _load_catalog(db)

    if tier not in (None, "free", "paid"):
        from mcp_server.auth import _raise, ERR_FORBIDDEN
        _raise(ERR_FORBIDDEN, "tier must be 'free' or 'paid'")

    effective_tier = tier
    if effective_tier is None:
        effective_tier = "paid" if _has_full_data_access(user.get("role")) else "free"

    # "paid" tier means full catalog access (free + paid symbols), matching
    # the dashboard's own free-vs-paid symbol list rule.
    symbols = catalog if effective_tier == "paid" else [c for c in catalog if c["tier"] == "free"]
    _log_call("list_symbols", "success", f"tier={effective_tier}")
    return {"tier": effective_tier, "symbols": symbols}


@mcp.tool()
def get_gex_snapshot(symbol: str, type: str, date: str | None = None, expiry_date: str | None = None) -> dict:
    """Latest (or specified) GEX snapshot for a symbol — gex_by_strike, net_gex,
    call_wall, put_wall, spot_price, timestamp. type: intraday | weekly | monthly | rolling."""
    from mcp_server.auth import authorize, _raise, ERR_FORBIDDEN, ERR_NOT_FOUND
    from app.routes.api import _check_authorization, _fetch_snapshot, _serialize, _symbol_doc

    user = authorize()
    symbol = symbol.upper().strip()
    snap_type = type.lower().strip()
    db = _db()

    if snap_type == "rolling":
        # get_rolling_trend is the dedicated tool for the rolling series;
        # gex_by_strike doesn't exist on rolling docs, so treat as unsupported here.
        _raise(ERR_FORBIDDEN, "use get_rolling_trend for type='rolling'")

    allowed, reason = _check_authorization(symbol, snap_type, user.get("role"))
    if not allowed:
        _raise(ERR_FORBIDDEN, reason)

    symbol_doc = _symbol_doc(symbol, db)
    timezone_name = symbol_doc.get("market_timezone", str(CT))
    req_date = (
        _parse_iso_date(date)
        if date else datetime.now(ZoneInfo(timezone_name)).date()
    )
    expiry = _parse_iso_date(expiry_date) if expiry_date else None

    doc = _fetch_snapshot(symbol, snap_type, req_date, expiry, trade_date_given=bool(date), db=db)
    if doc is None:
        _raise(ERR_NOT_FOUND, f"no {snap_type} data for {symbol} on {req_date.isoformat()}")

    _log_call("get_gex_snapshot", "success", f"{symbol} {snap_type}")
    return {
        "symbol": symbol,
        "type": snap_type,
        "date": req_date.isoformat(),
        "market_timezone": timezone_name,
        "currency": symbol_doc.get("currency", "USD"),
        "display_unit": symbol_doc.get("display_unit", "billion"),
        "data": _serialize(doc, timezone_name),
    }


@mcp.tool()
def get_intraday_snapshots(symbol: str, date: str | None = None, n: int = 5) -> dict:
    """Metadata (timestamp, net_gex, spot_price, call_wall, put_wall) for the
    last N intraday snapshots for a symbol/date. No gex_by_strike."""
    from mcp_server.auth import authorize, _raise, ERR_FORBIDDEN
    from app.routes.api import _check_authorization, _serialize
    from app.models import gex_intraday

    user = authorize()
    symbol = symbol.upper().strip()
    n = max(1, min(int(n), 10))
    _primary_db, db, symbol_doc, timezone_name = _market_context(symbol)

    allowed, reason = _check_authorization(symbol, "intraday", user.get("role"))
    if not allowed:
        _raise(ERR_FORBIDDEN, reason)

    req_date = (
        _parse_iso_date(date)
        if date else datetime.now(ZoneInfo(timezone_name)).date()
    )
    docs = gex_intraday.find_last_n_by_symbol_date(
        db, symbol, _market_midnight(req_date, timezone_name), "0dte", n
    )

    snapshots = [
        {
            "timestamp": d["timestamp"].astimezone(ZoneInfo(timezone_name)).strftime("%H:%M"),
            "net_gex": d.get("net_gex"),
            "call_wall": d.get("call_wall"),
            "put_wall": d.get("put_wall"),
            "spot_price": d.get("spot_price"),
        }
        for d in docs if d.get("timestamp") is not None
    ]
    _log_call("get_intraday_snapshots", "success", f"{symbol} n={n}")
    return {
        "symbol": symbol,
        "date": req_date.isoformat(),
        "market_timezone": timezone_name,
        "currency": symbol_doc.get("currency", "USD"),
        "display_unit": symbol_doc.get("display_unit", "billion"),
        "snapshots": snapshots,
    }


@mcp.tool()
def get_weekly_evolution(symbol: str, expiry_date: str) -> dict:
    """Every trade_date snapshot for the specified upcoming Friday expiry —
    the day-by-day GEX evolution for that week. Paid key required."""
    from mcp_server.auth import authorize, _raise, ERR_NOT_FOUND
    from app.routes.api import _serialize
    import db_reader

    authorize(required_tier="paid")
    symbol = symbol.upper().strip()
    expiry = date.fromisoformat(expiry_date)
    _primary_db, db, _symbol_doc, timezone_name = _market_context(symbol)

    docs = db_reader.get_weekly_evolution(
        db, symbol, _market_midnight(expiry, timezone_name)
    )
    if not docs:
        _raise(ERR_NOT_FOUND, f"no weekly data for {symbol} expiry {expiry_date}")

    _log_call("get_weekly_evolution", "success", f"{symbol} {expiry_date}")
    return {
        "symbol": symbol,
        "expiry_date": expiry_date,
        "market_timezone": timezone_name,
        "days": [_serialize(d, timezone_name) for d in docs],
    }


def _forward_trend_rows(db, model, symbol: str, timezone_name: str) -> list[dict]:
    """Shared logic behind get_forward_weeks/get_forward_months — today's
    trend rows, falling back to the latest available trade_date."""
    today = datetime.now(ZoneInfo(timezone_name)).date()
    rows = model.find_trend_for_trade_date(
        db, symbol, _market_midnight(today, timezone_name)
    )
    if not rows:
        latest = model.find_recent(db, symbol, n=1) if hasattr(model, "find_recent") else \
                 model.find_recent_cycles(db, symbol, n=1)
        if latest:
            td = latest[0].get("trade_date")
            if td:
                rows = model.find_trend_for_trade_date(db, symbol, td)
    return rows


@mcp.tool()
def get_forward_weeks(symbol: str, n: int | None = None) -> dict:
    """net_gex/call_wall/put_wall summary across the next N tracked upcoming
    weeks. Free keys get max 3; paid keys get up to 12."""
    from mcp_server.auth import authorize, _raise, ERR_FORBIDDEN
    from app.routes.api import _has_full_data_access, FREE_SYMBOLS, _serialize
    from app.models import gex_weekly

    user = authorize()
    symbol = symbol.upper().strip()
    is_paid = _has_full_data_access(user.get("role"))
    cap = 12 if is_paid else 3

    if not is_paid and symbol not in FREE_SYMBOLS:
        _raise(ERR_FORBIDDEN, "symbol not available on your plan — upgrade to unlock")

    n = cap if n is None else max(1, min(int(n), cap))
    _primary_db, db, symbol_doc, timezone_name = _market_context(symbol)
    rows = _forward_trend_rows(db, gex_weekly, symbol, timezone_name)[:n]

    _log_call("get_forward_weeks", "success", f"{symbol} n={n}")
    return {
        "symbol": symbol,
        "market_timezone": timezone_name,
        "currency": symbol_doc.get("currency", "USD"),
        "display_unit": symbol_doc.get("display_unit", "billion"),
        "weeks": [
            {"expiry_date": _serialize(r.get("expiry_date")), "net_gex": r.get("net_gex"),
             "call_wall": r.get("call_wall"), "put_wall": r.get("put_wall")}
            for r in rows
        ],
    }


@mcp.tool()
def get_forward_months(symbol: str, m: int | None = None) -> dict:
    """net_gex/call_wall/put_wall summary across the next M tracked monthly
    OPEX cycles (max 3)."""
    from mcp_server.auth import authorize, _raise, ERR_FORBIDDEN
    from app.routes.api import _has_full_data_access, FREE_SYMBOLS, _serialize
    from app.models import gex_monthly_opex

    user = authorize()
    symbol = symbol.upper().strip()
    is_paid = _has_full_data_access(user.get("role"))

    if not is_paid and symbol not in FREE_SYMBOLS:
        _raise(ERR_FORBIDDEN, "symbol not available on your plan — upgrade to unlock")

    m = 3 if m is None else max(1, min(int(m), 3))
    _primary_db, db, symbol_doc, timezone_name = _market_context(symbol)
    rows = _forward_trend_rows(db, gex_monthly_opex, symbol, timezone_name)[:m]

    _log_call("get_forward_months", "success", f"{symbol} m={m}")
    return {
        "symbol": symbol,
        "market_timezone": timezone_name,
        "currency": symbol_doc.get("currency", "USD"),
        "display_unit": symbol_doc.get("display_unit", "billion"),
        "cycles": [
            {"expiry_date": _serialize(r.get("expiry_date")), "net_gex": r.get("net_gex"),
             "call_wall": r.get("call_wall"), "put_wall": r.get("put_wall")}
            for r in rows
        ],
    }


@mcp.tool()
def get_term_structure(symbol: str) -> dict:
    """Next 3 upcoming trading days' full GEX data, filtered to
    ±gex_term_structure_strike_range_pct% of spot. Index symbols only. Paid key required."""
    from mcp_server.auth import authorize, _raise, ERR_FORBIDDEN
    from app.routes.api import (
        _TERM_STRUCTURE_SYMBOLS,
        _filter_term_structure_strikes,
        _latest_term_structure,
        _serialize,
    )
    from app.models import platform_settings

    authorize(required_tier="paid")
    symbol = symbol.upper().strip()
    if symbol not in _TERM_STRUCTURE_SYMBOLS:
        _raise(ERR_FORBIDDEN, f"term structure is only available for index symbols ({', '.join(sorted(_TERM_STRUCTURE_SYMBOLS))})")

    primary_db, _db, symbol_doc, timezone_name = _market_context(symbol)
    latest, ts = _latest_term_structure(symbol, primary_db)
    range_pct = platform_settings.get_term_structure_strike_range_pct(primary_db)
    spot = latest.get("spot_price") if latest else None
    ts = _filter_term_structure_strikes(ts, spot, range_pct)

    _log_call("get_term_structure", "success", symbol)
    return {
        "symbol": symbol,
        "market_timezone": timezone_name,
        "currency": symbol_doc.get("currency", "USD"),
        "display_unit": symbol_doc.get("display_unit", "billion"),
        "term_structure": _serialize(ts, timezone_name),
        "strike_range_pct": range_pct,
    }


@mcp.tool()
def get_rolling_trend(symbol: str) -> dict:
    """Rolling 21-day EOD net_gex trend series. Paid key required."""
    from mcp_server.auth import authorize
    from app.routes.api import _serialize
    import db_reader

    authorize(required_tier="paid")
    symbol = symbol.upper().strip()
    _primary_db, db, symbol_doc, timezone_name = _market_context(symbol)
    rows = db_reader.get_rolling_21d(db, symbol, n=21)

    _log_call("get_rolling_trend", "success", symbol)
    return {
        "symbol": symbol,
        "market_timezone": timezone_name,
        "currency": symbol_doc.get("currency", "USD"),
        "display_unit": symbol_doc.get("display_unit", "billion"),
        "series": [_serialize(r, timezone_name) for r in rows],
    }


@mcp.tool()
def get_platform_status() -> dict:
    """Scheduler heartbeat, last data refresh per job, and current market hours status."""
    from mcp_server.auth import authorize
    from app.routes.api import _serialize
    from app.routes.main import _is_market_hours_now
    from app.models import scheduler_heartbeat, pipeline_health

    authorize()
    db = _db()
    heartbeat = scheduler_heartbeat.get_heartbeat(db)

    last_per_job: dict[str, dict] = {}
    for row in pipeline_health.find_recent(db, limit=100):
        job = row.get("job_name")
        if job not in last_per_job:
            last_per_job[job] = {
                "status": row.get("status"),
                "run_at": _serialize(row.get("run_at")),
                "detail": row.get("detail"),
            }

    _log_call("get_platform_status", "success", "")
    return {
        "scheduler_heartbeat": _serialize(heartbeat.get("last_heartbeat_at")) if heartbeat else None,
        "last_refresh_per_job": last_per_job,
        "market_open": _is_market_hours_now(),
    }


# ── Action tools (write/trigger) ────────────────────────────────────────────

@mcp.tool()
def trigger_data_refresh(symbol: str, date: str | None = None) -> dict:
    """Request a manual EOD data pull for the given symbol. Admin key only.

    This writes a refresh-request flag for the scheduler process to pick up —
    it does not touch the Schwab client from the MCP process, keeping Schwab
    access isolated to the scheduler (confirmed open item, Module 11).
    Rate-limited to 5 calls/hour per key regardless of tier.
    """
    from mcp_server.auth import authorize, check_named_rate_limit
    from app.models import data_refresh_requests, api_keys as api_keys_model

    user = authorize(required_roles=("admin",))
    db = _db()

    # Dedicated 5/hour cap, independent of the role-based tool-call limit.
    key_hash = api_keys_model.hash_key(_bearer_key())
    check_named_rate_limit(db, key_hash, "refresh", limit=5)

    trade_date = _midnight_ct(_parse_date(date))
    symbol = symbol.upper().strip()
    request_id = data_refresh_requests.create(db, symbol, trade_date, user["_id"])

    _log_call("trigger_data_refresh", "success", f"{symbol} requested by {user['_id']}")
    return {"ok": True, "request_id": str(request_id), "symbol": symbol,
            "detail": "refresh requested — the scheduler will pick this up on its next poll"}


def _bearer_key() -> str:
    from mcp_server.auth import _extract_bearer_token
    return _extract_bearer_token()


@mcp.tool()
def trigger_backfill(symbol: str | None = None) -> dict:
    """Trigger a forward-window backfill (Module 05's backfill-forward-window).
    Admin key only."""
    from mcp_server.auth import authorize
    from app.services.pipeline_rerun import backfill_forward_window

    authorize(required_roles=("admin",))
    result = backfill_forward_window(symbol=symbol)
    _log_call("trigger_backfill", "success" if result.get("ok") else "failed", str(result))
    return result


@mcp.tool()
def generate_twitter_draft(post_type: str) -> dict:
    """Generate a Twitter post draft without publishing. Staff or admin key required.
    post_type: premarket | eod | eow."""
    from mcp_server.auth import authorize, _raise, ERR_FORBIDDEN
    from app.services.social_generation import generate_draft
    from app.models import social_posts

    user = authorize(required_roles=("staff", "admin"))
    if post_type not in ("premarket", "eod", "eow"):
        _raise(ERR_FORBIDDEN, "post_type must be premarket, eod, or eow")

    db = _db()
    post_id, chart_ok = generate_draft(db, post_type, triggered_by=user["_id"])
    post = social_posts.find_by_id(db, post_id)

    _log_call("generate_twitter_draft", "success", f"{post_type} draft {post_id}")
    return {"draft_id": str(post_id), "post_type": post_type, "tweets": post.get("tweets", []),
            "chart_ok": chart_ok}


@mcp.tool()
def publish_twitter_post(post_type: str, draft_id: str | None = None) -> dict:
    """Publish a specific draft (by draft_id from a prior generate_twitter_draft
    call), or generate and immediately publish if no draft_id supplied.
    Staff or admin key required."""
    from mcp_server.auth import authorize, _raise, ERR_FORBIDDEN, ERR_NOT_FOUND
    from app.services.social_generation import generate_draft, publish_post
    from app.models import social_posts

    user = authorize(required_roles=("staff", "admin"))
    if post_type not in ("premarket", "eod", "eow"):
        _raise(ERR_FORBIDDEN, "post_type must be premarket, eod, or eow")

    db = _db()
    if draft_id is None:
        draft_id, _chart_ok = generate_draft(db, post_type, triggered_by=user["_id"])
    else:
        post = social_posts.find_by_id(db, draft_id)
        if post is None:
            _raise(ERR_NOT_FOUND, f"no draft found with id {draft_id}")
        if post.get("status") != "draft":
            _raise(ERR_FORBIDDEN, "only draft posts can be published")

    tweet_ids = publish_post(db, draft_id)
    _log_call("publish_twitter_post", "success", f"{post_type} draft {draft_id}")
    return {"ok": True, "draft_id": str(draft_id), "tweet_ids": tweet_ids}


# ── Resources ────────────────────────────────────────────────────────────────
# URI-addressable equivalents of the tools above. Same authorization applies —
# each just delegates to the corresponding tool function rather than
# duplicating the tier/role checks.

@mcp.resource("gex://snapshot/{symbol}/{type}")
def snapshot_resource(symbol: str, type: str) -> dict:
    """Latest snapshot for a symbol/type combination."""
    return get_gex_snapshot(symbol, type)


@mcp.resource("gex://forward/{symbol}/weekly")
def forward_weekly_resource(symbol: str) -> dict:
    """Current forward week summary (net_gex per tracked week) for a symbol."""
    return get_forward_weeks(symbol)


@mcp.resource("gex://forward/{symbol}/monthly")
def forward_monthly_resource(symbol: str) -> dict:
    """Current forward monthly OPEX summary for a symbol."""
    return get_forward_months(symbol)


@mcp.resource("gex://status")
def status_resource() -> dict:
    """Platform status (scheduler heartbeat, last refresh times)."""
    return get_platform_status()


def main():
    from mcp_server.auth import ensure_indexes as ensure_auth_indexes
    from app.models import ensure_all_indexes

    db = _db()
    ensure_all_indexes(db)
    ensure_auth_indexes(db)

    log.info("Starting RetailGex MCP server on :5010/mcp")
    mcp.run(transport="http", host="0.0.0.0", port=5010, path="/mcp")


if __name__ == "__main__":
    main()
