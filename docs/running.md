# Running the App & Scheduler

## Prerequisites

- Python 3.11+
- MongoDB running locally (`mongod`) or a remote URI in `.env`
- Schwab developer account for US collection (not needed to run the web app alone)
- Zerodha Kite Connect credentials for NIFTY collection (optional; access tokens rotate daily)

---

## 1. First-time setup

```bash
cp .env.example .env          # fill in secrets before running
python -m venv .venv 
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/seed_symbols.py   # required — pipeline reads symbols from MongoDB
```

### Optional: initialize isolated Zerodha/NIFTY storage

Zerodha market data uses a second database on the same MongoDB server. The
primary application database continues to own users, settings, pipeline health,
Social Studio, and all existing Schwab data.

```bash
mongosh --eval 'db.runCommand({ ping: 1 })'
```

Keep the existing `MONGO_URI` unchanged and add a distinct database name:

```dotenv
ZERODHA_MONGO_URI=mongodb://127.0.0.1:27017/retailgex_zerodha
ZERODHA_API_KEY=
ZERODHA_ACCESS_TOKEN=
```

Then initialize and check the integration:

```bash
python scripts/init_zerodha.py
python scripts/check_zerodha.py
```

`init_zerodha.py` is idempotent. It creates only the four NIFTY market-data
indexes in the secondary database and upserts the paid NIFTY symbol metadata in
the primary database. It does not modify users, settings, Social Studio data,
Schwab symbols, or US market data. The readiness check is read-only and never
prints credentials or full connection strings.

---

## 2. Web app

### Development

```bash
source .venv/bin/activate
python wsgi.py
```

Runs at **http://127.0.0.1:5005** by default.  
Override host/port in `.env`:

```
FLASK_HOST=0.0.0.0
FLASK_PORT=5005
```

### Production (gunicorn)

```bash
gunicorn wsgi:app --bind 0.0.0.0:5005 --workers 2
```

---

## 3. Background scheduler (GEX collection)

The scheduler is a **separate process** — it never runs inside the Flask web app.

For how automatic scheduling relates to admin manual reruns (`/admin/pipeline`), see [pipeline-rerun-and-scheduling.md](pipeline-rerun-and-scheduling.md).

```bash
source .venv/bin/activate
python -m scheduler.runner
```

Existing US jobs are unchanged. Zerodha adds `zerodha_nifty_intraday`,
`zerodha_nifty_eod`, and `zerodha_nifty_monthly`. All Zerodha automatic jobs
and the WebSocket are disabled by default and can be configured in Pipeline
Health. Manual NIFTY runs remain available with valid credentials.

Keep it alive in production with a process manager:

```bash
# launchd (macOS) or systemd (Linux) — see docs/spec/07-deployment-infra.md
# Quick option for dev/testing:
nohup python -m scheduler.runner > logs/scheduler.log 2>&1 &
```

---

## 4. MCP server (external agent access)

A separate process, same pattern as the scheduler — lets Claude or another
MCP-compatible agent read GEX data and trigger a few actions via API key,
with the same free/paid tier enforcement as the dashboard.

```bash
source .venv/bin/activate
python -m mcp_server.server
```

Runs at **http://localhost:5010/mcp**. See [mcp-server.md](mcp-server.md) for
how it works, generating an API key, connecting Claude Desktop/Code, and the
Cloudflare Tunnel setup.

---

## 5. Environment variables

All variables are documented in [`.env.example`](../.env.example).  
Required to start the web app:

| Variable | Description |
|---|---|
| `FLASK_SECRET_KEY` | Session signing key |
| `MONGO_URI` | MongoDB connection string |
| `ZERODHA_MONGO_URI` | Separate NIFTY market-data database; must have a different database name |

Additional variables (Google OAuth, Stripe, Schwab, Zerodha, Twitter) are only
required when the corresponding module is used. A Zerodha database or API
outage does not prevent authentication, Social Studio, or existing Schwab
dashboard data from operating.

---

## 6. Stopping processes

```bash
# Dev server / scheduler started in foreground — Ctrl+C
# Background scheduler started with nohup:
pkill -f "scheduler.runner"
pkill -f "mcp_server.server"
```
