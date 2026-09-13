# GEX Intelligence Platform — Project Overview

## 1. What this is
A web application that gives traders historical and near-real-time **Gamma Exposure (GEX)** data — dealer positioning, call/put walls, and related options-market-structure signals — for a curated list of symbols. Free users get a limited symbol set and limited lookback; paid users get the full symbol list, full history, and more granular views (intraday, weekly, monthly OPEX).

This is a relaunch of an existing personal-use tool as a multi-tenant SaaS product.

## 2. Tech stack
- **Backend:** Python, Flask
- **Database:** MongoDB
- **Frontend:** Bootstrap, vanilla JS (server-rendered Jinja templates + JS for charts/interactivity)
- **Auth:** Google OAuth (primary) + email/password fallback
- **Payments:** Stripe (Checkout + Billing Portal + webhooks)
- **Data sources:** Schwab API (primary), CBOE (primary for index delayed quotes), yfinance (fallback/supplement when Schwab/CBOE are unavailable or rate-limited)
- **Hosting:** Self-hosted at home (Mac), exposed via Cloudflare Tunnel
- **Distribution/marketing:** Twitter bot posting GEX snapshots on a schedule (8:00 AM, 9:30 AM, 3:30 PM)

## 3. User types
| Role | Description |
|---|---|
| **Anonymous / Visitor** | Lands on index page, can view "this week's data" with no login, can click a Twitter-shared link to view one symbol/date snapshot without logging in |
| **Free User** | Registered (Google or email/password). Access to 4 symbols: SPY, QQQ, TSLA, NVDA. Index granularities (Intraday/weekly/monthly) available for SPY/QQQ — the "GEX Next 3 Trading Days" card is paid-only and not available here even on these free-tier symbols; stocks (TSLA, NVDA) get a **4-tab weekly toggle (Past Week │ This Week │ Next Week │ Week+2)**, all 4 tabs available to free users on their free-tier symbols; week+3 and beyond (or more than 1 week back) is paid-only |
| **Paid User** | Full symbol list (Mag7 + SPX + SPY, QQQ, IWM, NDX), full historical lookback, all granularities (intraday 5-min, EOD, weekly, monthly OPEX) |
| **Staff** | Internal role, limited admin permissions — read-only support access (users, pipeline health), plus full rights in the Twitter Post Studio (Module 10). Exact set confirmed in Module 01. |
| **Admin** | Full internal access: user management, subscription overrides, system/data pipeline monitoring |

## 4. High-level user flow (from diagrams)
1. User lands on **Index Page**.
2. Three paths from Index Page:
   - **Login** (Google OAuth or UserID/Password) → on success, role-based redirect (Admin → Admin panel, User → Home Page)
   - **Register** (email/password or Google) → on success, redirected into Login → Home Page
   - **Check this week's data** → no signup/login required, limited public view
3. Logged-in **User** → **Home Page** → **User Dashboard**, **Upgrade Subscription**, or **Logout**
4. **Admin** → separate Admin home/dashboard
5. Dashboard loads **Free** or **Paid** UI based on subscription status:
   - Free: Index view (Intraday + weekly/monthly, no "GEX Next 3 Trading Days" card — paid-only) for SPY/QQQ; Stock view (weekly expiry) for TSLA/NVDA with **4-tab toggle: Past Week | This Week | Next Week | Week+2** — all 4 free on TSLA/NVDA, week+3 and beyond paid-only, tabs roll forward after Friday close or 3PM CT
   - Paid: full symbol list, full views, no lookback restriction
6. A separate **backend Data Pipeline** runs on schedule, pulls data from Schwab/CBOE, computes/stores GEX, and serves read requests to the dashboard as JSON.
7. A **Twitter distribution job** posts snapshots 3x/day; clicking the link from Twitter drops a visitor straight into a single symbol/date view without requiring login.

## 5. Module breakdown (separate docs)
1. `01-auth-and-roles.md` — Login/Registration, Google OAuth, roles & permissions
2. `02-data-pipeline-gex-collection.md` — Schwab/CBOE ingestion, schedulers, GEX calculation/storage flow
3. `03-database-schema.md` — MongoDB collections, indexes, document shapes
4. `04-dashboard-frontend.md` — User dashboard, free vs paid views, symbol/date selection, charts
5. `05-admin-staff-panel.md` — Admin/staff tools, user & subscription management, data pipeline monitoring
6. `06-billing-stripe.md` — Stripe subscription, checkout, webhooks, entitlement sync
7. `07-deployment-infra.md` — Home Mac hosting, Cloudflare Tunnel, process management, backups
8. `08-public-distribution-twitter.md` — Twitter posting job, public single-snapshot landing page, anonymous "this week" view
9. `09-llm-trade-analysis-future.md` — **future module, not v1** — LLM-generated GEX analysis/predictions, carried over conceptually from the existing personal tool
10. `10-twitter-post-studio.md` — staff/admin Twitter Post Generation Studio (Pre-Market/EOD/EOW branded content posts) — separate from Module 08's simpler automated visitor-funnel bot
11. `11-mcp-server.md` — MCP server (Model Context Protocol) for external AI agent access — separate lightweight process, API-key authenticated, same free/paid tier enforcement as the dashboard

## 6. Open items you said you'll confirm later
- Scope/timing for Module 09 (LLM trade analysis) — confirmed in-scope as a future module, but not part of v1 build
- Module 10 (Twitter Post Studio) chart branding (real Twitter handle, logo, colors) — using placeholder/generic styling for now, to be finalized before public launch

## 7. How to use these docs with Claude
Give Claude `00-overview.md` first for context, then feed each module doc when you want Claude to build that piece. Each module doc is self-contained with: purpose, user stories, data/API contracts, and edge cases — written so Claude can implement directly from it.
