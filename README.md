# GreenCanaryIndia

India-market fork of the **GEX Intelligence Platform** — a web application that surfaces historical and near-real-time **Gamma Exposure (GEX)** data (dealer positioning, call/put walls, and related options-market-structure signals) for a curated list of symbols.

This repository was split out from the main [RetailGex](https://github.com/chetansaini39/RetailGex) codebase to keep India-specific work (Zerodha/NSE integration, IST market hours, INR-denominated symbols, etc.) separate from the US-market product.

> **Status:** initial snapshot imported from RetailGex's `arnav_changes` branch. Some US-specific code (Schwab/CBOE clients, SPX/SPY-oriented tests and scripts) is still present pending cleanup — see [Roadmap](#roadmap--cleanup) below.

## What this is

- Free users get a limited symbol set and limited lookback; paid users get the full symbol list, full history, and more granular views (intraday, weekly, monthly OPEX).
- Originally a relaunch of a personal-use tool as a multi-tenant SaaS product; this fork adapts that product for Indian index/equity options.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Python, Flask |
| Database | MongoDB |
| Frontend | Bootstrap, server-rendered Jinja templates + vanilla JS for charts/interactivity |
| Auth | Google OAuth (primary) + email/password fallback |
| Payments | Stripe (Checkout + Billing Portal + webhooks) |
| India data source | [Zerodha Kite Connect](https://kite.trade/) (`data_sources/zerodha_client.py`, `zerodha_stream.py`) |
| Legacy/US data sources | Schwab API, CBOE, yfinance *(carried over from RetailGex, to be phased out here — see Roadmap)* |
| Scheduler | APScheduler-based background jobs (`scheduler/`), with `scheduler/india_market_utils.py` for NSE market-hours handling |
| Distribution | Twitter bot posting GEX snapshots on a schedule |
| Hosting | Self-hosted, exposed via Cloudflare Tunnel (see `deploy/`) |

## Project structure

```
app/            Flask app: routes, models, services, templates, static assets
data_sources/   Market data clients (Zerodha, Schwab, CBOE, yfinance) + shared pricing utils
scheduler/      Background job runner and job definitions (GEX collection, social posts, etc.)
mcp_server/     MCP (Model Context Protocol) server for external AI agent access
docs/spec/      Module-by-module product/engineering specs
deploy/         Cloudflare Tunnel config + launchd services for self-hosting
scripts/        One-off/admin scripts (data backfills, Zerodha setup, admin user creation)
tests/          Test suite
```

## Getting started

1. **Clone and set up a virtualenv**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Configure environment**
   ```bash
   cp .env.example .env
   # fill in MongoDB URI, Zerodha API key/secret, Google OAuth, Stripe, etc.
   ```
3. **Run**
   ```bash
   ./start.sh              # web app + scheduler + MCP server
   ./start.sh --web-only   # web app only
   ./start.sh --tunnel     # also start the Cloudflare tunnel
   ```

See `docs/running.md` and `docs/spec/00-overview.md` for more detail on the overall product design (note: the spec docs currently describe the original US-market version and haven't yet been updated for the India-specific split).

## Roadmap / cleanup

This repo currently carries the full RetailGex codebase as a starting point. Known follow-ups to actually separate the India product:
- Remove or gate out Schwab/CBOE-specific data source code and tests (`data_sources/schwab_client.py`, `scripts/GetSchwabData.py`, `scripts/SchwabApi.py`, `tests/test_schwab_client.py`, etc.) once Zerodha is the sole data path here.
- Update `docs/spec/` to reflect Indian symbols (e.g. NIFTY, BANKNIFTY) instead of SPX/SPY/Mag7.
- Rename remaining `retailgex`/`RetailGex` references in config and deploy files (`deploy/launchd/com.retailgex.*.plist`, `RetailGex.code-workspace`) to match this project.
- Drop stray generated artifacts committed in `output/` and `outputs/` that came along with the snapshot.
