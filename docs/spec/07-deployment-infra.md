# Module 07 — Deployment & Infrastructure

## Purpose
Run the Flask app and the background GEX collection scheduler reliably on a home Mac, exposed to the public internet via Cloudflare Tunnel, with reasonable resilience for a production-facing (even if small) paid service.

## Components to run
1. **Flask web app** — served via a production WSGI server (Gunicorn), not `flask run`. Standard multi-worker config is fine here **only if the Flask app never directly imports/calls the Schwab client** (per Module 02, it shouldn't — it only reads from Mongo).
2. **Scheduler process** — separate long-running process (Module 02), independent of the web app so a web app restart doesn't interrupt data collection. **Must run as a single instance** — the Schwab OAuth token file is not safe for concurrent access from multiple processes/workers; running more than one instance of the scheduler (or letting it run inside a multi-worker Gunicorn process) risks token-file corruption and duplicate/conflicting scheduler runs.
3. **MCP server** — separate lightweight process (`mcp_server/server.py`, Module 11), independent of both the Flask app and scheduler. Runs on port 5010 and handles external AI agent connections via Streamable HTTP. Read-only tools are safe to run multi-worker; the `trigger_data_refresh` action tool should be single-instance if it touches the Schwab client.
4. **MongoDB** — local instance on the Mac (or MongoDB Atlas free/low tier if you'd rather not manage DB uptime/backups yourself — worth considering given this is now a paying-customer-facing product).
5. **Cloudflare Tunnel** (`cloudflared`) — exposes the Mac's local Flask app and MCP server to public domains without port-forwarding or exposing your home IP.

## Process management
- Use `launchd` (macOS-native) to keep Gunicorn, the scheduler process, and `cloudflared` running and auto-restart on crash or after a Mac reboot. Each gets its own `.plist` file in `~/Library/LaunchAgents/`.
- Log rotation: route stdout/stderr from each process to log files, rotate to avoid unbounded growth (e.g. `logrotate` or a simple size-based rotation in launchd config).
- **Verify the scheduler's `launchd` plist is actually loaded, not just present on disk** — `launchctl list | grep <your-label>` should show it running. A plist file existing in `~/Library/LaunchAgents/` does nothing until it's loaded (`launchctl load`) and `RunAtLoad`/`KeepAlive` are set correctly. This is exactly the kind of thing that looks configured but silently isn't (the scheduler being down on a trading day with no automated detection is the real-world failure case this is meant to prevent).

## Scheduler liveness monitoring
The scheduler running is the single most important thing for this product to actually deliver value — if it's down, the dashboard is silently serving stale data and nothing else in the stack will tell you. Don't rely on noticing missing data yourself. Build:

1. **A liveness signal the scheduler writes on every cycle.** Beyond the per-job entries already written to `pipeline_health` (Module 03), have the scheduler process write/update a lightweight heartbeat document on a short fixed interval (e.g. every 1–5 minutes while running) — a single document with `last_heartbeat_at` is enough; doesn't need to be a growing log. This catches the case where the *process itself* is down (e.g. crashed, never started after a reboot, `launchd` plist not loaded) — which `pipeline_health`'s job-level entries don't reliably catch, since "no new entries" could mean "process is down" or could just mean "no jobs were due to run" (e.g. outside trading hours).
2. **A `/healthz`-style check specific to the scheduler**, separate from the Flask app's own `/healthz` (Module 07's existing checklist item 6) — e.g. `/healthz/scheduler`, which checks the heartbeat document's `last_heartbeat_at` and returns unhealthy if it's older than a reasonable threshold (a few minutes) **during market hours**, and healthy/not-applicable outside market hours (so it doesn't false-alarm overnight or on weekends/holidays). Point an external monitor (UptimeRobot or similar) at this in addition to the Flask app's general health check — they catch different failure modes.
3. **Surface staleness in the admin pipeline-health UI (Module 05)** — see that module's update for the specific behavior.

## Reliability considerations (important since this is now customer-facing, not personal use)
- **Power/internet outage at home** = your SaaS goes down. Decide upfront how much uptime risk is acceptable for v1 — acceptable for a beta/early-access launch, but worth flagging to users ("best-effort uptime during beta") or planning a future migration to a small VPS once you have paying customers who'd be upset by downtime.
- **Backups**: MongoDB should be backed up daily (e.g. `mongodump` to an external drive or cloud storage like Backblaze/S3) — a home Mac has no redundancy if the disk fails.
- **Cloudflare Tunnel** also gives you free SSL/TLS termination and basic DDoS protection — good fit for this setup.

## Environment variables (don't hardcode)
- `MONGO_URI`
- `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`
- `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` / `STRIPE_WEBHOOK_SECRET`
- `SCHWAB_API_KEY` / `SCHWAB_API_SECRET`
- `SCHWAB_CALLBACK_URL` (e.g. `https://127.0.0.1:8182`)
- `SCHWAB_TOKEN_PATH` (where the auto-refreshed OAuth token is cached — see Module 02's single-instance constraint)
- `FLASK_SECRET_KEY`
- `FLASK_HOST` (e.g. `0.0.0.0`), `FLASK_PORT` (e.g. `5001`), `FLASK_DEBUG` (`false` in production — your reference config has this `true`, flip it before going live)
- `TWITTER_API_KEY` / secret / bearer token — used by Module 08 (visitor-funnel bot), Module 10 (Post Generation Studio), and Module 05's admin connectivity test
- `EMAIL_FROM` — the "from" address for all outbound emails (e.g. `noreply@retailgex.csaini.org`)
- `EMAIL_SMTP_HOST` / `EMAIL_SMTP_PORT` / `EMAIL_SMTP_USER` / `EMAIL_SMTP_PASSWORD` — SMTP credentials for sending password reset and email verification emails (Module 01). Use a transactional email service (e.g. SendGrid, Mailgun, AWS SES, or Zoho — all have free tiers adequate for low-volume transactional mail) rather than your home ISP's SMTP, which may be blocked on port 25 and has poor deliverability. If you use SendGrid specifically, `SENDGRID_API_KEY` can substitute for the SMTP credentials (their API is simpler than raw SMTP). Pick one approach and store only those vars.
- `ADMIN_CONTACT_EMAIL` — the inbox that receives contact form submission notification emails (Module 01's `/contact` backend). Can be any email address you check regularly; doesn't need to match `EMAIL_FROM`.
- `MARKET_OPEN` / `MARKET_CLOSE` (trading-hour gate used by the scheduler, e.g. `08:45` / `15:00`, interpreted in `TIMEZONE` below)
- `TIMEZONE` (`America/Chicago`) — **single source of truth for all timestamps app-wide**, in both code and the database (see Module 03's Notes). Used for: the scheduler's trading-hour gate, `trade_date` bucketing, all `created_at`/`timestamp`/`run_at`-style fields written to MongoDB, and the free-tier stock view's Friday-close/3PM-CT reset logic (Module 04). Implement with a tz-aware library, not a fixed UTC offset, so daylight saving (CST/CDT) is handled automatically.
- `CONTRACT_SIZE` (`100`) — options contract multiplier used in the GEX formula (Module 02); kept configurable rather than hardcoded in case it ever needs to change
- `LOG_LEVEL`, `LOG_FILE` — app-wide logging config
- ~~Scheduler interval config via env vars (`SCHWAB_GEX_INTERVAL_MINUTES`, `CBOE_POLL_INTERVAL_MINUTES`)~~ — **superseded.** The 0DTE capture interval (the only configurable capture interval — this platform only captures same-day-expiry data, no separate general-intraday interval) is admin-configurable at runtime via `platform_settings` (Module 02/03/05), not an env var — no restart required to change it. Don't add env vars for this; it'd create two competing sources of truth.
- LLM provider config (Module 09, future) — see that module doc for `LLM_PROVIDER`, `OLLAMA_*` / `LM_STUDIO_*` vars; not required for v1

> CBOE's delayed-quotes endpoint requires no API key (public). yfinance also requires no key. Neither needs an entry here.
> Your reference `.env` from the old app also has `YAHOO_FINANCE_API_KEY` — leave it blank/unused; yfinance doesn't require a key, so this var isn't needed here.
> File-path variables from the old app (`OPTIONS_FILES_PATH`, `GEX_DATA_PATH`, `CACHE_PATH`, etc.) were for local CSV/JSON file dumps in the personal tool. This platform stores everything in MongoDB instead, so those aren't needed — keep only `LOGS_PATH` and a generic `EXPORTS_PATH` if you want a consistent place for the admin newsletter-export CSV (Module 05) to land.

## Deployment checklist for Claude to follow
1. `.env` file (gitignored) holds all secrets above; load via `python-dotenv`.
2. Gunicorn config for the **Flask web app**: reasonable worker count for a home Mac (e.g. 2–4 workers to start) — fine since the web app doesn't touch the Schwab client directly.
3. Scheduler process: run as a **single process, single instance** (no multi-worker/multi-process model) — see the Schwab token-file constraint above. A simple `launchd` keep-alive (not a multi-worker server) is the right model here.
4. MCP server: separate `launchd` plist (`org.retailgex.mcp.plist`), `RunAtLoad: true`, `KeepAlive: true`, bound to port 5010.
5. Cloudflare Tunnel config (`config.yml`): `retailgex.csaini.org → localhost:5005` (Flask) and `retailgex.csaini.org` path `/mcp*` → `localhost:5010` (MCP server), with the path rule ordered before the Flask rule for the same hostname. No separate `mcp.` subdomain — a second-level subdomain isn't covered by Cloudflare's free Universal SSL cert, so no new DNS entry is needed either.
6. Separate `launchd` plists for: web app, scheduler, MCP server, cloudflared (four total).
7. A simple `/healthz` Flask route for uptime checks (can be pinged by an external monitor like UptimeRobot, which is free and worth setting up given the home-hosting risk above).
8. A separate `/healthz/scheduler` route backed by the scheduler's heartbeat document (see "Scheduler liveness monitoring" above) — confirm the scheduler is actually writing the heartbeat on its interval, and that the route correctly distinguishes "stale during market hours" (unhealthy) from "stale outside market hours" (expected, not an alert).
