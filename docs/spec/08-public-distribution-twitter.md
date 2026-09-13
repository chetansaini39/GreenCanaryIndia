# Module 08 — Public Distribution (Twitter) & Anonymous Access

## Purpose
Drive top-of-funnel traffic by posting GEX snapshots to Twitter on a schedule, and letting clicks land on a single-symbol/date view without requiring login — converting visitors into registered free users.

## Twitter posting job
- Schedule: **8:00 AM, 9:30 AM, 3:30 PM** (per diagram).
- Job picks a symbol/date snapshot (likely one of the free-tier symbols, to make the linked page accessible without paywall) and posts a summary (e.g. net GEX, call/put wall) with a link back to the site: `https://yourdomain.com/snapshot/<symbol>/<date>`.
- Implementation: Twitter API v2 (`tweepy` library), scheduled via the same scheduler process as Module 02, or a separate lightweight job.

## Public snapshot landing page
- Route: `GET /snapshot/<symbol>/<date>` — **no auth required**.
- Renders the single GEX snapshot (chart + summary) for that exact symbol/date — read-only, no symbol switching, no date picker.
- Below the chart: prompt to **Register/Login** to view other symbols or historical data → routes into the standard Login/Registration screen.
- After successful login/registration from this entry point, redirect into `/dashboard` (not back to the snapshot) — per diagram, the "Want to look at historical data or other symbols' GEX?" decision point leads to Login/Registration when "Yes".
- If user declines ("No, Want to look at historical data..." → "Do Nothing"): present a lightweight **newsletter signup** (email-only capture, no password/account required) as a softer conversion path for visitors who don't want a full account yet. This is in scope for v1.

### Newsletter signup — v1 requirements
- Route: `POST /newsletter/signup` — accepts an email address, no auth.
- Store in a `newsletter_subscribers` collection: `{ _id, email, source ("snapshot_decline" | other), subscribed_at, is_active }`.
- Validate email format server-side; dedupe on email (unique index) — re-submitting an existing email should succeed silently rather than erroring.
- No double opt-in confirmation required for v1 (can be added later if deliverability becomes an issue).
- Surface this subscriber list to Admin (Module 05) for export/use with whatever mailing tool you choose later (e.g. for periodic GEX newsletter content) — a simple `/admin/newsletter` read/export view is sufficient for v1.

## "Today's data" public view
- Route: `GET /today` — no login required, accessible directly from the Index Page.
- Shows **today's daily/intraday data** for the free-tier symbol set only (SPY, QQQ, TSLA, NVDA) — no historical lookback, no symbol-switching beyond the free 4.
- Weekly granularity is a **paid-only feature** — this public view never exposes weekly, monthly OPEX, or rolling 21-day data, regardless of symbol.
- Same authorization rule as the dashboard: server-side enforce that this route can never return paid-tier symbols, weekly/monthly granularity, or non-today data, regardless of query params passed.

## Rate limiting
- Public/unauthenticated routes (`/snapshot/...`, `/today`, `/api/gex/public`) should be rate-limited (e.g. `Flask-Limiter`) to prevent scraping of your GEX data by competitors or bots.
- Suggested limits (tune after launch based on real traffic):
  - `/snapshot/<symbol>/<date>`: 30 requests/hour per IP
  - `/today`: 30 requests/hour per IP
  - `/api/gex/public`: 20 requests/hour per IP
  - `/newsletter/signup`: 5 requests/hour per IP (prevent spam submissions)
- Key limiter on IP address by default; consider also keying on a combination of IP + User-Agent if you see bots rotating IPs.
- Return HTTP 429 with a simple "too many requests, try again later" message on limit breach — no need for a custom error page.
- Apply rate limiting at the Flask app layer (`Flask-Limiter` with an in-memory or Redis backend); Cloudflare Tunnel doesn't give you this for free, so don't rely on it for abuse protection.
- Authenticated routes (`/api/gex`, `/dashboard`) can have looser limits (e.g. 120 requests/hour per user) — mainly to catch runaway client-side polling bugs, not to police legitimate paid users.

