# MCP Server — External Agent Access

How Claude (or any MCP-compatible agent) can read live GEX data and trigger a
few platform actions through RetailGex's own API-key system, instead of
scraping the dashboard or going through a user's browser session.

Design spec: [11-mcp-server.md](spec/11-mcp-server.md). This doc is the
practical "how do I actually turn this on and connect to it" version.

---

## 1. How it works

**It's a separate process**, just like the scheduler — not part of the Flask
web app. If it crashes or gets overloaded, the dashboard keeps working.

```
mcp_server/server.py   → runs on port 5010, Streamable HTTP at /mcp
```

**Auth is API keys, not sessions.** A user generates a key from their own
account (`/account/api-keys`); the key inherits that user's `role` and
`subscription_status` at the time of the request. Every tool call sends:

```
Authorization: Bearer <api_key>
```

The server looks up the key's SHA-256 hash in the `api_keys` collection,
loads the linked user, and enforces the **exact same tier/role rules as the
dashboard** — a free key gets a 403 on anything paid-only, same as a free
user's browser session hitting `/api/gex`. This isn't a parallel
implementation; the MCP server reuses the same authorization helpers as
`app/routes/api.py`.

**Rate limits** are per-key, tracked in a Mongo collection so they hold
across restarts/workers:

| Key tier | Calls / hour |
|---|---|
| Free | 60 |
| Paid | 300 |
| Staff | 300 |
| Admin | 1000 |

`trigger_data_refresh` additionally has its own 5/hour cap regardless of tier,
since each call queues a real data pull.

**Schwab access stays isolated to the scheduler.** `trigger_data_refresh`
doesn't call Schwab from the MCP process — it writes a row to
`data_refresh_requests`, and the scheduler (the only process that touches the
Schwab OAuth token) polls that collection every 2 minutes and runs the
request. See [scheduler/jobs/data_refresh_requests.py](../scheduler/jobs/data_refresh_requests.py).

### What's available

**Read tools** (tier-enforced): `list_symbols`, `get_gex_snapshot`,
`get_intraday_snapshots`, `get_weekly_evolution` (paid), `get_forward_weeks`,
`get_forward_months`, `get_term_structure` (paid), `get_rolling_trend` (paid),
`get_platform_status`.

**Action tools** (role-enforced): `trigger_data_refresh` (admin),
`trigger_backfill` (admin), `generate_twitter_draft` (staff/admin),
`publish_twitter_post` (staff/admin).

**Resources** (URI-addressable, same auth as their tool equivalents):
`gex://snapshot/{symbol}/{type}`, `gex://forward/{symbol}/weekly`,
`gex://forward/{symbol}/monthly`, `gex://status`.

Full parameter list for each: [11-mcp-server.md](spec/11-mcp-server.md#mcp-tools).

---

## 2. Running it

### Dev

```bash
source .venv/bin/activate
python -m mcp_server.server
```

Runs at `http://localhost:5010/mcp`.

### Via start.sh

`./start.sh` starts it automatically alongside the web app and scheduler.
`./start.sh --web-only` skips it (along with the scheduler). Logs go to
`logs/mcp.log`.

### Production (launchd, macOS)

```bash
cp deploy/launchd/com.retailgex.mcp.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.retailgex.mcp.plist
```

Runs as a single always-on process (`RunAtLoad`/`KeepAlive`), same pattern as
the scheduler's plist. Unlike the scheduler, it has no single-instance
constraint — it never touches the Schwab client directly — so it would be
safe to run behind multiple workers if you ever need to scale it, though the
plist as shipped just runs one.

### Exposing it externally (Cloudflare Tunnel)

Route it as a **path**, not its own subdomain:

```yaml
- hostname: retailgex.csaini.org
  path: /mcp*
  service: http://localhost:5010

- hostname: retailgex.csaini.org
  service: http://localhost:5005   # Flask — must come after the /mcp* rule
```

**Why not `mcp.retailgex.csaini.org`:** Cloudflare's free Universal SSL cert
only covers the zone apex and one level of wildcard (`*.csaini.org`). A
second-level subdomain like `mcp.retailgex.csaini.org` has no valid cert at
Cloudflare's edge and fails the TLS handshake before the request ever reaches
the tunnel (`SSLv3 alert handshake failure` from curl is the symptom). Routing
by path under the already-covered `retailgex.csaini.org` hostname sidesteps
this with no new DNS entry needed. An Advanced Certificate (paid Cloudflare
add-on) would let the subdomain work if you ever want it.

After editing `config.yml`, restart the tunnel to pick up the change —
`cloudflared` doesn't watch the file:

```bash
launchctl unload ~/Library/LaunchAgents/com.retailgex.cloudflared.plist
launchctl load ~/Library/LaunchAgents/com.retailgex.cloudflared.plist
```

Public endpoint: `https://retailgex.csaini.org/mcp`.

---

## 3. Getting an API key

Log in to the dashboard → click **API Keys** in the nav bar (or go directly to
`/account/api-keys`) → enter a label → **Generate key**. The raw key is shown
once — copy it now. Only its hash is stored, so it can't be recovered later;
generate a new one if you lose it. Revoke a key any time from the same page.

**Oversight for staff/admin:** every key across every account, regardless of
who created it, is also visible from the admin panel's **API Keys** section
(`/admin/api-keys`) — staff get a read-only list (`/staff/api-keys`), admins
can revoke any user's key from there. Useful for a suspected leaked key or
offboarding a user.

---

## 4. Connecting a client

Any MCP client that speaks Streamable HTTP and can send a custom header
works. Two concrete paths:

### Claude Desktop — native remote connector (preferred, no Node needed)

Settings → Connectors → **Add custom connector**:
- URL: `https://retailgex.csaini.org/mcp`
- Header: `Authorization: Bearer <your_key>`

### Claude Desktop / other clients — via `mcp-remote` (needs Node.js)

If your client only supports the config-file / stdio format:

```json
{
  "mcpServers": {
    "retailgex": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://retailgex.csaini.org/mcp",
        "--header", "Authorization:${AUTH_HEADER}"
      ],
      "env": { "AUTH_HEADER": "Bearer <your_key>" }
    }
  }
}
```

No space around the colon in `--header` — some clients (Claude Desktop on
Windows, Cursor, Codex-CLI) mangle spaces inside `args` when spawning `npx`.
Put the space in the env var instead, as shown.

Requires Node.js (`brew install node` on macOS) since `mcp-remote` is an npm
package launched via `npx`. If `npx` isn't on PATH, the client will fail with
`Failed to spawn process: No such file or directory`.

### Claude Code (this CLI)

```bash
claude mcp add --transport http retailgex https://retailgex.csaini.org/mcp \
  --header "Authorization: Bearer <your_key>"
```

No Node/npx dependency — Claude Code speaks Streamable HTTP directly.

### Don't commit your key

Keep the raw key out of anything checked into git (`.mcp.json` in a repo,
shared configs, etc.). Revoke and regenerate from `/account/api-keys` if one
ever leaks.

---

## 5. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `missing Authorization: Bearer <api_key> header` | Client didn't send the header — check `mcp-remote`'s `--header` syntax or the connector's header field |
| `invalid or revoked API key` | Typo in the key, or it was revoked from `/account/api-keys` |
| `this tool requires a paid plan` / `symbol not available on your plan` | Working as intended — the key's linked account is free-tier |
| `this tool requires role in (...)` | Action tool called with a non-staff/non-admin key |
| `rate limit exceeded` | Hit the hourly cap for the key's tier (or the 5/hour `trigger_data_refresh` cap) — wait for the next hour bucket |
| `TLS connect error ... handshake failure` hitting `mcp.retailgex.csaini.org` | That subdomain has no valid cert — use `https://retailgex.csaini.org/mcp` instead (see §2) |
| `Failed to spawn process: No such file or directory` from `mcp-remote` | Node.js/`npx` isn't installed — `brew install node`, or use Claude Desktop's native connector instead |
