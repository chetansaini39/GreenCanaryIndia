# Module 11 — MCP Server (External Agent Access)

## Purpose
Expose RetailGex's GEX data and selected actions to external AI agents and tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). This lets agents built on Claude, GPT, or any MCP-compatible framework fetch live GEX snapshots, analyze options market structure, and trigger platform actions — all with the same free/paid tier enforcement as the dashboard.

## Architecture

**Separate lightweight process**, not part of the Flask web app, for the same reason the scheduler is separate: isolation. A crash or overload in the MCP server doesn't affect the dashboard, and the MCP server can be restarted independently. It connects to the same MongoDB instance as Flask.

| Component | Process | Port | Notes |
|---|---|---|---|
| Flask web app | `wsgi.py` | 5005 | Existing |
| GEX scheduler | `scheduler/runner.py` | — | Existing |
| MCP server | `mcp_server/server.py` | 5010 | New |
| MongoDB | mongod | 27017 | Shared by all |
| cloudflared | `cloudflared tunnel run` | — | Exposes all via tunnel |

**Transport:** Streamable HTTP (the current MCP standard as of 2025) — `POST /mcp` for client→server messages, `GET /mcp` for server→client SSE stream. The Cloudflare tunnel exposes it as a path under the existing hostname, `retailgex.csaini.org/mcp` — see "Cloudflare tunnel" below for why the obvious `mcp.retailgex.csaini.org` subdomain doesn't work.

**Library:** `fastmcp` (Python, wraps the `mcp` SDK) — simplest approach for a Flask shop already on Python. Install via `pip install fastmcp`.

## Authentication & authorization

### API key model
- Users generate API keys from their account settings page (`/account/api-keys`) — see Module 01/03 for the UI and schema.
- Each key is linked to the user's account and inherits their tier (free/paid) and role.
- Keys are sent via the `Authorization: Bearer <key>` header on every MCP request.
- The MCP server validates the key against the `api_keys` collection (Module 03) on every request — no session cookies, no OAuth, just key lookup.
- **Tier enforcement is identical to the dashboard API** — a free user's API key gets 403 on paid-only tools/resources, exactly as their browser session does on paid-only `/api/gex` endpoints. Reuse the same authorization helpers, not a parallel implementation.
- **Role-based action gating:** action tools (data refresh, Twitter post) check the user's `role` field from the linked account — admin-only actions are 403 for free/paid users even with a valid key.

### Rate limits
- Free-tier API keys: 60 tool calls/hour per key.
- Paid-tier API keys: 300 tool calls/hour per key.
- Admin keys: 1000 calls/hour.
- Return MCP protocol error with code `-32429` (Too Many Requests) on limit breach.
- Use the same Flask-Limiter-compatible logic, but keyed on the API key value rather than IP.

## MCP tools

Tools are callable functions — agents invoke them to fetch data or trigger actions. All tools return JSON-serializable results.

### Data tools (read-only)

| Tool | Parameters | Auth | Description |
|---|---|---|---|
| `list_symbols` | `tier?: "free"\|"paid"` | Any valid key | Returns available symbols. If `tier` omitted, returns symbols for the caller's own tier. |
| `get_gex_snapshot` | `symbol`, `type: "intraday"\|"weekly"\|"monthly"\|"rolling"`, `date?: YYYY-MM-DD`, `expiry_date?: YYYY-MM-DD` | Any valid key, tier-enforced | Returns the latest (or specified) GEX snapshot including `gex_by_strike`, `net_gex`, `call_wall`, `put_wall`, `spot_price`, `timestamp`. |
| `get_intraday_snapshots` | `symbol`, `date?: YYYY-MM-DD`, `n?: int (1–10)` | Any valid key, tier-enforced | Returns metadata (timestamp, net_gex, spot_price, call_wall, put_wall) for the last N intraday snapshots for that symbol/date. Default N=5. No `gex_by_strike` — use `get_gex_snapshot` with a specific timestamp for full detail. |
| `get_weekly_evolution` | `symbol`, `expiry_date: YYYY-MM-DD` | Paid key | Returns every trade_date snapshot for the specified upcoming Friday expiry — the day-by-day GEX evolution for that week. |
| `get_forward_weeks` | `symbol`, `n?: int (1–12)` | Any valid key | Returns net_gex/call_wall/put_wall summary (no gex_by_strike) across the next N tracked upcoming weeks. Free keys get max 3; paid keys get up to 12. This mirrors the free/paid distinction on the "GEX Next 12 Weeks" dashboard card (Module 04). |
| `get_forward_months` | `symbol`, `m?: int (1–3)` | Any valid key | Returns net_gex/call_wall/put_wall summary across the next M tracked monthly OPEX cycles. |
| `get_term_structure` | `symbol` | Paid key | Returns the next 3 upcoming trading days' full GEX data from the `term_structure` embedded field, filtered to ±`gex_term_structure_strike_range_pct`% of spot (same as the "GEX Next 3 Trading Days" dashboard card). Index symbols only. |
| `get_rolling_trend` | `symbol` | Paid key | Returns the rolling 21-day EOD net_gex trend series. |
| `get_platform_status` | — | Any valid key | Returns scheduler heartbeat (`last_heartbeat_at`), last data refresh per job from `pipeline_health`, and current market hours status. Useful for agents to know if data is fresh before analyzing. |

### Action tools (write/trigger)

| Tool | Parameters | Auth | Description |
|---|---|---|---|
| `trigger_data_refresh` | `symbol`, `date?: YYYY-MM-DD` | Paid or admin key | Triggers a manual EOD data pull for the given symbol (same logic as Module 05's admin "rerun" button). Rate-limited to 5 calls/hour per key regardless of tier, since each call hits Schwab/CBOE. |
| `trigger_backfill` | `symbol?: string (omit = all symbols)` | Admin key only | Triggers a forward-window backfill (Module 05's `/admin/pipeline/backfill-forward-window`). |
| `generate_twitter_draft` | `post_type: "premarket"\|"eod"\|"eow"` | Staff or admin key | Generates a Twitter post draft (Module 10) without publishing. Returns the draft text and chart metadata. |
| `publish_twitter_post` | `post_type`, `draft_id?: string` | Staff or admin key | Publishes a specific draft (by `draft_id` from a prior `generate_twitter_draft` call), or generates and immediately publishes if no `draft_id` supplied. Writes to `social_posts` with `status: published`. |

## MCP resources

Resources are URI-addressable data — agents can subscribe to them or fetch them by URI. Useful for agents that want to monitor a symbol continuously.

| Resource URI | Description |
|---|---|
| `gex://snapshot/{symbol}/{type}` | Latest snapshot for a symbol/type combination — refreshes each time the scheduler writes a new one. |
| `gex://forward/{symbol}/weekly` | Current forward week summary (net_gex per tracked week) for a symbol. |
| `gex://forward/{symbol}/monthly` | Current forward monthly OPEX summary for a symbol. |
| `gex://status` | Platform status (scheduler heartbeat, last refresh times). |

Resource access enforces the same tier rules as the corresponding tools.

## MCP server implementation notes

- **`fastmcp` setup:** define tools with `@mcp.tool()` decorator, resources with `@mcp.resource()`. The server mounts at `/mcp` and handles Streamable HTTP transport automatically.
- **Database connection:** reuse the same PyMongo client pattern as the Flask app — connect on startup, shared across all tool calls (no per-request connection overhead).
- **Authorization middleware:** write a single `authorize(api_key, required_role=None, required_tier=None)` helper that looks up the key in `api_keys`, fetches the linked user's role/subscription_status, and raises an MCP error if the check fails. Every tool calls this at the top.
- **Error codes:** use MCP's JSON-RPC error codes — `-32401` for unauthorized (bad/missing key), `-32403` for forbidden (tier/role insufficient), `-32404` for not found (no data for that symbol/date), `-32429` for rate limit.
- **Logging:** write tool call logs to `pipeline_health` with `job_name: "mcp_tool_{tool_name}"` — gives admin visibility into MCP usage without a separate collection.
- **Single instance constraint:** same as the scheduler, the MCP server should run as a single process (not multi-worker) if it ever touches the Schwab client for the `trigger_data_refresh` action. For read-only tools it's safe to run multiple workers.

## launchd plist
Add a fourth `launchd` plist alongside the existing three (Flask, scheduler, cloudflared):
```
~/Library/LaunchAgents/org.retailgex.mcp.plist
ProgramArguments: ["python", "/path/to/mcp_server/server.py"]
RunAtLoad: true
KeepAlive: true
```

## Cloudflare tunnel
`mcp.retailgex.csaini.org` (a second-level subdomain) is **not** covered by
Cloudflare's free Universal SSL certificate — only the apex and one level of
wildcard (`*.retailgex.csaini.org`) are. Requesting it as its own hostname
fails the TLS handshake at Cloudflare's edge before reaching the tunnel at
all. Route the MCP server as a **path** under the existing covered hostname
instead. In `config.yml`, the path rule must come before the Flask catch-all
rule for the same hostname:
```yaml
- hostname: retailgex.csaini.org
  path: /mcp*
  service: http://localhost:5010

- hostname: retailgex.csaini.org
  service: http://localhost:5005
```
No new DNS route is needed — `retailgex.csaini.org` is already routed.

(An Advanced Certificate would let `mcp.retailgex.csaini.org` work as its own
hostname if that's ever preferred, but it's a paid Cloudflare add-on.)

## MCP endpoint for clients
Clients (Claude, custom agents, etc.) connect to:
```
https://retailgex.csaini.org/mcp
Authorization: Bearer <api_key>
```

## Open items — resolved
- `get_forward_weeks` free-key cap: kept at 3 weeks for free / 12 for paid, consistent with the dashboard.
- `trigger_data_refresh` access: **admin-only** (tightened from the earlier paid-or-above draft).
- `trigger_data_refresh` mechanism: **writes a `data_refresh_requests` flag document**; the MCP process never touches the Schwab client directly. The scheduler polls that collection every 2 minutes (`scheduler/jobs/data_refresh_requests.py`) and runs the request through the same EOD job the admin panel's manual "rerun" button uses. This keeps Schwab/OAuth-token access isolated to the scheduler process. `trigger_backfill` was left calling `pipeline_rerun.backfill_forward_window()` directly, matching the admin panel's existing behavior for that action (which already runs from the Flask process, not just the scheduler).
