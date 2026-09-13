# Deployment — Home Mac + Cloudflare Tunnel

## One-time setup

```bash
# 1. Create logs & exports directories
mkdir -p logs exports

# 2. Copy and fill in secrets
cp .env.example .env
# edit .env — fill in all blank values

# 3. Create the Cloudflare Tunnel (once per machine)
cloudflared tunnel create retailgex
# Copy the UUID printed above into deploy/cloudflare/config.yml
# Then route DNS:
cloudflared tunnel route dns retailgex yourdomain.com
```

The MCP server does not get its own hostname — a second-level subdomain
(e.g. `mcp.yourdomain.com`) isn't covered by Cloudflare's free Universal SSL
cert, only an Advanced Certificate is. Route it as a path under the existing
hostname instead (`https://yourdomain.com/mcp`) — see `config.yml`.

## Installing launchd agents

Copy plists to `~/Library/LaunchAgents/` and load them:

```bash
cp deploy/launchd/*.plist ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.retailgex.webapp.plist
launchctl load ~/Library/LaunchAgents/com.retailgex.scheduler.plist
launchctl load ~/Library/LaunchAgents/com.retailgex.mcp.plist
launchctl load ~/Library/LaunchAgents/com.retailgex.cloudflared.plist
```

## Common operations

```bash
# Restart web app after a code deploy
launchctl unload ~/Library/LaunchAgents/com.retailgex.webapp.plist
launchctl load  ~/Library/LaunchAgents/com.retailgex.webapp.plist

# Tail logs
tail -f logs/webapp.stderr.log
tail -f logs/scheduler.stderr.log
tail -f logs/cloudflared.stderr.log

# Health check
curl http://localhost:5001/healthz
```

## Gunicorn

Config lives in `gunicorn.conf.py` at the repo root. Workers default to 4.
The scheduler runs as a **single process** (not gunicorn) — never add workers to it;
the Schwab token file is not safe for concurrent access.
