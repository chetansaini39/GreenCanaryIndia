# How to use this spec set with Cursor / Claude Code

This folder contains the full spec for the GEX Intelligence Platform, broken into modules. Keep this whole folder in the repo (e.g. `docs/spec/`) so your AI coding tool can reference files directly instead of relying on copy-pasted context.

## Before you start

1. Scaffold the project structure first — folders for `routes/`, `models/`, `templates/`, `static/`, `scheduler/`, `data_sources/`, a Flask app factory, `.env.example`. Don't load any module doc until this skeleton exists.
2. Initialize a git repo and commit after every module — gives you a clean rollback point if a build session goes sideways.
3. Always include `00-overview.md` as context, in every session, regardless of which module you're building. It's the one doc that prevents drift (e.g. forgetting which symbols are free vs paid).

## Build order

| Step | Module | Why this order |
|---|---|---|
| 0 | Project scaffold (no doc) | Gives every module a place to land |
| 1 | `03-database-schema.md` | Nearly everything else depends on collections existing |
| 2 | `01-auth-and-roles.md` | Nothing else can be gated without this |
| 3 | `02-data-pipeline-gex-collection.md` | Independent process — can run in parallel with step 2 if you're working with two sessions |
| 4 | `04-dashboard-frontend.md` | Needs schema + auth |
| 5 | `06-billing-stripe.md` | Needs the `users` collection + auth |
| 6 | `05-admin-staff-panel.md` | Needs auth + users + pipeline + billing to actually manage |
| 7 | `08-public-distribution-twitter.md` | Smallest, most independent piece |
| 8 | `10-twitter-post-studio.md` | Builds on Modules 02 (data), 03 (schema), 05 (admin shell) — do after those exist; independent of Module 08 |
| 9 | `07-deployment-infra.md` | Last — deploy once there's something to deploy |
| 10 | `11-mcp-server.md` | After deployment is stable — separate process, builds on the existing auth/data layer |
| — | `09-llm-trade-analysis-future.md` | **Future, post-v1.** Don't build until 1–8 are live and stable; revisit the open decisions in that doc first. |

## Ready-to-paste prompts per module

Copy/paste these into Cursor or Claude Code, adjusting the file paths if your repo structure differs.

### Step 0 — Scaffold
```
Read docs/spec/00-overview.md for context on the project.
Scaffold a Flask app using the application factory pattern with this structure:
- app/__init__.py (factory)
- app/routes/ (blueprints, empty for now)
- app/models/ (Mongo model/access helpers, empty for now)
- app/templates/ (Bootstrap base layout)
- app/static/
- scheduler/ (separate from the Flask app, for the background GEX collection process)
- data_sources/ (Schwab/CBOE client stubs)
- .env.example listing all env vars referenced across docs/spec/*.md
- requirements.txt with Flask, PyMongo, Flask-Session, Authlib, Stripe, APScheduler, Flask-Limiter, gunicorn
Don't implement any business logic yet — just the skeleton and a working "hello world" route.
```

### Step 1 — Database schema
```
Read docs/spec/00-overview.md and docs/spec/03-database-schema.md.
Implement the MongoDB collections and indexes described in 03, using PyMongo.
Create model/access helper modules under app/models/ for each collection (users, gex_intraday,
gex_weekly, gex_monthly_opex, gex_rolling_21d, symbols_config, subscriptions_log, pipeline_health,
newsletter_subscribers, password_reset_tokens).
Ask me if anything in the doc is ambiguous before writing code.
```

### Step 2 — Auth & roles
```
Read docs/spec/00-overview.md and docs/spec/01-auth-and-roles.md.
Implement all routes in the table in 01, including Google OAuth (Authlib) and email/password auth.
Use the users collection from docs/spec/03-database-schema.md.
Implement session handling exactly as described (server-side sessions via Flask-Session backed by Mongo).
Cover every item in the "Edge cases" section — implement each one, don't skip any.
Flag the two open decisions in the doc (auto-login after registration; staff permission defaults)
back to me rather than guessing.
```

### Step 3 — Data pipeline
```
Read docs/spec/00-overview.md and docs/spec/02-data-pipeline-gex-collection.md.
Build the scheduler process (separate from the Flask app) per the scheduling table in 02.
Implement data_sources/schwab_client.py and data_sources/cboe_client.py as the data pull layer
(stub the actual API calls with TODOs if I haven't given you credentials/docs yet, but build the
calling structure, retry/error handling, and idempotent upsert logic exactly as specified).
Implement gex_engine.py for the GEX calculation step — flag clearly that the exact formula needs
to be supplied by me before this is production-correct.
Write to MongoDB using the collections from docs/spec/03-database-schema.md.
```

### Step 4 — Dashboard frontend
```
Read docs/spec/00-overview.md and docs/spec/04-dashboard-frontend.md.
Implement the /home, /dashboard routes and the API endpoints listed in 04.
Build the frontend with Bootstrap + Chart.js per the UI components section.
Enforce all tier/symbol authorization server-side as instructed — write a quick test or two that
proves a free user gets a 403 on a paid-only endpoint even if they craft the request manually.
Cover the edge cases section.
```

### Step 5 — Stripe billing
```
Read docs/spec/00-overview.md and docs/spec/06-billing-stripe.md.
Implement the /upgrade flow, Stripe Checkout session creation, the /webhooks/stripe handler for
all four events listed, and the /billing/portal route.
Follow the security section exactly: verify webhook signatures, never trust client redirects as
the source of truth for activation, dedupe on stripe_event_id.
```

### Step 6 — Admin & staff panel
```
Read docs/spec/00-overview.md and docs/spec/05-admin-staff-panel.md.
Implement the /admin and /staff routes and capabilities described, including the admin_audit_log
collection and the self-demotion lockout protection.
```

### Step 7 — Public distribution / Twitter
```
Read docs/spec/00-overview.md and docs/spec/08-public-distribution-twitter.md.
Implement the /snapshot/<symbol>/<date> and /today public routes, the newsletter signup endpoint
and collection, and the Twitter posting job (tweepy) on the schedule specified.
Apply the rate limiting section exactly as written using Flask-Limiter.
```

### Step 8 — Twitter Post Generation Studio
```
Read docs/spec/00-overview.md and docs/spec/10-twitter-post-studio.md.
Implement the social_posts and post_templates collections per docs/spec/03-database-schema.md,
including the TTL index for the 2-month retention policy.
Build the scheduled generation job (Pre-Market/EOD/EOW per the cadence table) that pulls SPX data,
fills templates, renders the chart, and auto-publishes via tweepy unless auto-publish is toggled
off in platform_settings for that post type.
Build the /admin/social studio routes for on-demand generation, manual publish, and template editing.
Flag the open items at the bottom of the doc back to me before finalizing the schedule times and
chart branding — don't guess on those.
```

### Step 9 — Deployment
```
Read docs/spec/00-overview.md and docs/spec/07-deployment-infra.md.
Set up Gunicorn config, launchd plist files for the web app/scheduler/cloudflared, a Cloudflare
Tunnel config.yml, and a /healthz route.
List every environment variable referenced across all docs/spec/*.md files and make sure
.env.example covers all of them.
```

## After each module is built

Go back through that module's doc, line by line, and check off each bullet — especially "Edge cases" sections, which coding tools tend to skip unless explicitly told to address each one. Commit to git once a module passes your check.
