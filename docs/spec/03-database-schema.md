# Module 03 — MongoDB Schema

## Database: `retailgex`

## Collection: `users`
```json
{
  "_id": ObjectId,
  "email": "string, unique, indexed",
  "password_hash": "string | null",
  "google_id": "string | null, indexed",
  "name": "string",
  "role": "free | paid | staff | admin",
  "subscription_status": "none | active | past_due | canceled",
  "stripe_customer_id": "string | null",
  "stripe_subscription_id": "string | null",
  "email_verified": "bool, default false for email/password registrations, default true for Google OAuth",
  "email_verification_token": "string | null (raw token stored temporarily during verification flow — nulled out once verified)",
  "created_at": "datetime (Central Time)",
  "last_login_at": "datetime (Central Time)",
  "is_active": "bool"
}
```
Indexes: `email` (unique), `google_id`, `role`.

## Collection: `password_reset_tokens`
Time-limited, single-use tokens for the forgot-password flow. Only email/password users ever have entries here; Google-only accounts cannot use this flow.
```json
{
  "_id": ObjectId,
  "user_id": "ObjectId (FK → users._id)",
  "token_hash": "string (SHA-256 hash of the raw token — raw token only ever appears in the email URL, never stored)",
  "expires_at": "datetime (Central Time, 1 hour from creation)",
  "used": "bool, default false",
  "created_at": "datetime (Central Time)"
}
```
Indexes: `token_hash` (unique — fast lookup on incoming reset requests); `user_id` (for invalidating all unused tokens for a user when a new one is issued or a reset succeeds); `expires_at` (optional TTL index — auto-purge expired tokens after e.g. 24 hours to keep the collection clean, since used/expired tokens have no ongoing value).

## Collection: `gex_intraday`
**0DTE only — same-day-expiring contracts.** No general "nearest active contract" fallback. This means: index symbols (SPX, NDX, SPY, QQQ, IWM) get intraday data every trading day, since they always have same-day expiries. Mag7 stocks only have same-day expiry on **Fridays** (their weekly expiry day) — so stocks only get documents in this collection on Fridays; Monday–Thursday, there is **no intraday data for stocks at all** (not "sparse data," genuinely none — see Module 04 for how the dashboard should communicate this rather than showing an empty/broken chart).

**For index symbols only**, each document also carries a `term_structure` array — the next 3 upcoming trading-day expiries' GEX, captured from the **same option chain pull** as the 0DTE data (not a separately scheduled job). This is what powers the dashboard's "EOD GEX Analysis" card (Module 04) — retired terminology: this used to be called "1DTE/2DTE/3DTE" in the UI, but that labeling is gone now; each entry is identified by its actual `expiry_date`, not a "days out" label.
```json
{
  "_id": ObjectId,
  "symbol": "SPX | NDX | SPY | QQQ | IWM | AAPL | ...",
  "asset_type": "index | stock",
  "timestamp": "datetime (Central Time, tz-aware — see Notes)",
  "trade_date": "date (for fast date-range queries)",
  "snapshot_type": "intraday",
  "source": "schwab | cboe | yfinance",
  "spot_price": "float",
  "net_gex": "float",
  "call_wall": "float",
  "put_wall": "float",
  "gex_by_strike": [{
    "strike": "float",
    "call_gex": "float",
    "put_gex": "float",
    "call_greeks": {"delta": "float", "gamma": "float", "theta": "float", "vega": "float", "iv": "float", "open_interest": "int", "volume": "int"},
    "put_greeks": {"delta": "float", "gamma": "float", "theta": "float", "vega": "float", "iv": "float", "open_interest": "int", "volume": "int"}
  }],
  "term_structure": [{
    "expiry_date": "date (an actual upcoming TRADING day, not calendar day — see note below)",
    "trading_days_out": "int (1, 2, or 3)",
    "net_gex": "float",
    "call_wall": "float",
    "put_wall": "float",
    "gex_by_strike": [{
      "strike": "float",
      "call_gex": "float",
      "put_gex": "float",
      "call_greeks": {"delta": "float", "gamma": "float", "theta": "float", "vega": "float", "iv": "float", "open_interest": "int", "volume": "int"},
      "put_greeks": {"delta": "float", "gamma": "float", "theta": "float", "vega": "float", "iv": "float", "open_interest": "int", "volume": "int"}
    }]
  }],
  "created_at": "datetime (Central Time)"
}
```
`term_structure` is `null`/omitted for stock documents (Mag7) — this feature is index-only. **Trading-day-aware date math:** "1/2/3 trading days out" must skip weekends and holidays using the same trading-calendar logic as Module 02's holiday/market-closed gate — e.g. on a Thursday, 1-trading-day-out is Friday, but 2-trading-days-out is Monday (skipping the weekend), not Saturday. Reuse that calendar utility rather than reimplementing date math separately here.

Indexes: compound `(symbol, trade_date, timestamp)`.

**Migration note if `contract_basis`/`nearest_active` already exist in this collection:** drop the `contract_basis` field entirely (it no longer has two meaningfully different values — everything in this collection is now 0DTE by definition). If any documents have `contract_basis: "nearest_active"` from the earlier (now-reverted) design, those represent non-same-day-expiry captures that no longer fit this collection's model — decide with the agent whether to delete them or migrate them elsewhere; don't silently keep them mixed in with 0DTE data going forward.

## Collection: `gex_weekly`
**Tracks the next N upcoming weekly Friday expiries in parallel, each with its own daily EOD evolution** (`N = platform_settings.gex_weekly_forward_weeks`, default 12). Previously this collection only tracked the single nearest upcoming Friday — now every trading day, the EOD job writes one document **per symbol, per each of the next N upcoming Friday expiries**, so the dashboard can show "how does the picture for 5-weeks-out compare to 2-weeks-out, as of today" in addition to the original "how did this week's picture evolve day by day" view. **Applies to all symbols** — index group and Mag7 stocks both have weekly expiries.
- **Sliding window, not a special rollover rule:** each capture day, the scheduler simply computes "the next N upcoming Friday expiries from today" (trading-day-aware, same calendar utility used elsewhere) and ensures a document exists for each, tagged with that day's `trade_date`. Once a Friday passes, it naturally falls out of the "next N" window on its own — no special-case logic needed, unlike the rollover rule that was previously written for Monthly OPEX (which this same pattern actually simplifies — see `gex_monthly_opex` below).
- **Key change: `expiry_date` is now the primary identity, not `week_of`.** With 12 (or however many) weeks tracked in parallel, `week_of` alone is ambiguous — it's still stored (useful for "which calendar week is this" display grouping) but the uniqueness and the "which of the 12 parallel tracks is this" question is answered by `expiry_date`.
```json
{
  "_id": ObjectId,
  "symbol": "string",
  "asset_type": "index | stock",
  "week_of": "date (Monday of the week containing expiry_date — informational/display grouping only, not the unique key)",
  "expiry_date": "date (the specific upcoming Friday this document tracks — identifies which of the N parallel tracks this is)",
  "trade_date": "date (the actual day this EOD snapshot was taken)",
  "snapshot_type": "weekly",
  "source": "schwab | cboe | yfinance",
  "spot_price": "float",
  "net_gex": "float",
  "call_wall": "float",
  "put_wall": "float",
  "gex_by_strike": [{
    "strike": "float",
    "call_gex": "float",
    "put_gex": "float",
    "call_greeks": {"delta": "float", "gamma": "float", "theta": "float", "vega": "float", "iv": "float", "open_interest": "int", "volume": "int"},
    "put_greeks": {"delta": "float", "gamma": "float", "theta": "float", "vega": "float", "iv": "float", "open_interest": "int", "volume": "int"}
  }],
  "created_at": "datetime (Central Time)"
}
```
Indexes: compound `(symbol, expiry_date, trade_date)` unique — changed from `(symbol, week_of, trade_date)` since `expiry_date` is now the correct identity for "which parallel track is this." Also index `(symbol, trade_date)` for "give me today's snapshot across all N tracked weeks" (the forward-trend summary view, Module 04) and keep `(symbol, expiry_date)` for "give me every day's snapshot for one specific Friday's evolution" (the original per-week evolution chart).

**Migration note if the old `(symbol, week_of, trade_date)` index/single-week-tracking model is already built:** this is a scope expansion, not a bug fix — existing single-week history under the old model is still valid (it's just "week 1 of what's now a 12-wide window"), but the scheduler needs to start writing the additional N-1 further-out weeks going forward, and the unique index needs to change to `(symbol, expiry_date, trade_date)`.

## Collection: `gex_monthly_opex`
**Tracks the next M upcoming monthly OPEX cycles in parallel, each with its own twice-weekly evolution** (`M = platform_settings.gex_monthly_forward_cycles`, default 3) — a Monday EOD and a Friday EOD capture, every week, for each of the M tracked cycles. **Applies to all symbols** — the index group (SPX, NDX, SPY, QQQ, IWM) and Mag7 stocks (AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA) both have a standard monthly (3rd Friday) expiration cycle, in addition to stocks' weekly Friday expiries.

**Sliding window (this replaces the earlier special-cased rollover rule with something simpler):** each Monday/Friday capture, the scheduler computes "the next M upcoming 3rd-Friday-of-month dates from today" and ensures a document exists for each. When a cycle's expiry Friday arrives, that cycle simply stops being "upcoming" and falls out of the window on its own — the (M+1)-th cycle that was waiting just outside the window now enters it. **There's no longer a special "today is expiry day, skip and jump to next cycle" rule to implement** — that behavior emerges naturally from "always track the next M upcoming cycles," the same way `gex_weekly`'s sliding window works (see that collection above, and Module 02's Granularity definitions for the full reasoning). If `gex_intraday`'s 0DTE data already covers the expiry day in full detail (true for both index symbols every day and stocks on their weekly-expiry Fridays, which the 3rd Friday always is), there's simply no need for that expiring cycle to get another `gex_monthly_opex` point that day — it already rolled out of the window by definition.
```json
{
  "_id": ObjectId,
  "symbol": "string",
  "asset_type": "index | stock",
  "month": "string YYYY-MM (the month of expiry_date, for display/grouping — not a unique key component, since trade_date can fall in the prior calendar month)",
  "expiry_date": "date (the 3rd Friday this snapshot is forecasting toward — identifies which of the M parallel tracks this is)",
  "trade_date": "date (the actual Monday or Friday this EOD snapshot was taken)",
  "snapshot_type": "monthly_opex",
  "source": "schwab | cboe | yfinance",
  "spot_price": "float",
  "net_gex": "float",
  "call_wall": "float",
  "put_wall": "float",
  "gex_by_strike": [{
    "strike": "float",
    "call_gex": "float",
    "put_gex": "float",
    "call_greeks": {"delta": "float", "gamma": "float", "theta": "float", "vega": "float", "iv": "float", "open_interest": "int", "volume": "int"},
    "put_greeks": {"delta": "float", "gamma": "float", "theta": "float", "vega": "float", "iv": "float", "open_interest": "int", "volume": "int"}
  }],
  "created_at": "datetime (Central Time)"
}
```
Indexes: compound `(symbol, expiry_date, trade_date)` unique. Also index `(symbol, trade_date)` for "give me today's snapshot across all M tracked cycles" (the forward-trend summary view, Module 04), and `(symbol, expiry_date)` for fetching one whole cycle's evolution at once.

**Migration note:** if the old single-cycle rollover logic is already built, this is a scope expansion — remove the special-case "is today expiry day" branch and replace it with the general sliding-window computation; existing single-cycle history is still valid, just needs the scheduler to start also writing the additional M-1 further-out cycles going forward.

## Collection: `gex_rolling_21d`
EOD snapshot, rolling 21 trading days, for trend charts. **Renamed from `gex_rolling_5d`/`rolling_5d_eod`** — window widened from 5 to 21 trading days for a fuller trend picture; the collection and `snapshot_type` value were renamed to match rather than leaving a "5d" name on a 21-day window.
```json
{
  "_id": ObjectId,
  "symbol": "string",
  "trade_date": "date",
  "snapshot_type": "rolling_21d_eod",
  "source": "schwab | cboe | yfinance",
  "spot_price": "float",
  "net_gex": "float",
  "call_wall": "float",
  "put_wall": "float",
  "created_at": "datetime (Central Time)"
}
```
Indexes: compound `(symbol, trade_date)` unique. **Retention policy (unchanged):** keep all EOD history here permanently — do not TTL or prune. Compute the rolling 21-day view at query time by slicing the last 21 `trade_date`s per symbol. This is simpler than maintaining a true 21-row buffer and preserves full history for paid users' deeper lookback.

**Migration note if `gex_rolling_5d` already exists:** rename the collection (or create `gex_rolling_21d` and migrate documents) rather than dropping history — this collection retains all EOD history regardless of the display window, so existing data is still valid under the new name, just needs the collection/snapshot_type renamed to match.

## Collection: `symbols_config`
Drives the scheduler and the dashboard symbol picker — single source of truth.
```json
{
  "_id": ObjectId,
  "symbol": "string",
  "asset_type": "index | stock",
  "tier": "free | paid",
  "weekly_expiry": "bool",
  "active": "bool",
  "added_at": "datetime (Central Time)"
}
```
Indexes: `symbol` (unique), `tier`, `active`.

## Collection: `subscriptions_log`
Audit trail of Stripe events (see Module 06).
```json
{
  "_id": ObjectId,
  "user_id": ObjectId,
  "stripe_event_id": "string, unique",
  "event_type": "string",
  "raw_payload": "object",
  "processed_at": "datetime (Central Time)"
}
```
Indexes: `stripe_event_id` (unique), `user_id` (for looking up all events for a user, e.g. on the admin user-detail page).

## Collection: `pipeline_health`
For admin/staff monitoring (Module 05).
```json
{
  "_id": ObjectId,
  "job_name": "string",
  "run_at": "datetime (Central Time)",
  "status": "success | failed | skipped",
  "detail": "string",
  "symbols_processed": "int"
}
```
Indexes: compound `(job_name, run_at)`; `status`.

## Collection: `scheduler_heartbeat`
Single-document collection the scheduler process updates on a short fixed interval (e.g. every 1–5 minutes while running), independent of any individual job's success/failure. Backs the `/healthz/scheduler` check and the admin staleness warning (Module 05/07) — this is how you detect "the scheduler process itself is down," which job-level `pipeline_health` entries alone can't reliably distinguish from "no jobs were due to run right now."
```json
{
  "_id": "global",
  "last_heartbeat_at": "datetime (Central Time)",
  "process_started_at": "datetime (Central Time)"
}
```
Use a fixed `_id` (e.g. `"global"`) and `upsert` on every heartbeat write, same pattern as `platform_settings` — no need for a growing log here, just the latest timestamp.

## Collection: `newsletter_subscribers`
Email-only capture for visitors who decline full registration (see Module 08).
```json
{
  "_id": ObjectId,
  "email": "string, unique, indexed",
  "source": "snapshot_decline | other",
  "subscribed_at": "datetime (Central Time)",
  "is_active": "bool"
}
```
Indexes: `email` (unique).

## Collection: `api_keys`
API keys for MCP server access (Module 11). Each key is linked to a user account and inherits their tier and role. Keys are stored as SHA-256 hashes — the raw key is only shown once on creation.
```json
{
  "_id": ObjectId,
  "user_id": "ObjectId (FK → users._id)",
  "key_hash": "string (SHA-256 of the raw key — raw key never stored)",
  "label": "string (user-defined name, e.g. 'Claude agent', 'Trading bot')",
  "is_active": "bool",
  "created_at": "datetime (Central Time)",
  "last_used_at": "datetime (Central Time) | null"
}
```
Indexes: `key_hash` (unique — fast lookup on every MCP request); `user_id` (for listing/managing a user's keys).

## Collection: `contact_submissions`
Stores every contact/feedback form submission from `/contact`. Used as the persistent record; email notification is sent on arrival (see Module 01 and Module 07 for email config).
```json
{
  "_id": ObjectId,
  "type": "bug | feature | data | general",
  "name": "string",
  "email": "string",
  "message": "string",
  "user_id": "ObjectId | null (null if submitted by an anonymous visitor, linked to the users collection if the visitor was logged in)",
  "submitted_at": "datetime (Central Time)",
  "status": "new | read | resolved",
  "notes": "string | null (internal staff notes, not visible to submitter)"
}
```
Indexes: `submitted_at` (descending — admin list view loads newest first); `status`; `user_id` (for linking submissions back to a user's account on the admin user-detail page).

## Collection: `platform_settings`
Single-document collection holding global platform toggles. Read on every request that needs it (or cached for a few seconds at most) — not meant for high-frequency data, just admin-controlled flags.
```json
{
  "_id": "global",
  "registration_enabled": "bool, default true",
  "social_premarket_autopublish": "bool, default true",
  "social_eod_autopublish": "bool, default true",
  "social_eow_autopublish": "bool, default true",
  "gex_0dte_interval_minutes": "int, default 15",
  "gex_weekly_forward_weeks": "int, default 12",
  "gex_monthly_forward_cycles": "int, default 3",
  "gex_term_structure_strike_range_pct": "int, default 7",
  "updated_at": "datetime (Central Time)",
  "updated_by": "ObjectId (admin user_id)"
}
```
Use a fixed, well-known `_id` (e.g. the string `"global"`) so the app always reads/writes the same single document via `upsert`, rather than needing a query + handling "no settings doc exists yet." Add fields here for future global toggles as needed (e.g. "maintenance mode") rather than creating a new collection per toggle.

**`gex_0dte_interval_minutes`** — admin-configurable capture cadence (Module 02/05), a single global value applying across all symbols (not per-symbol). The scheduler process must **re-read this document on every cycle** (not just at startup) so an admin's change takes effect without requiring a scheduler restart — same "read fresh, don't cache long" principle already established for `registration_enabled`.

**`gex_weekly_forward_weeks`** (default 12) and **`gex_monthly_forward_cycles`** (default 3) — admin-configurable sliding-window widths for how many upcoming weekly/monthly expiries are tracked in parallel (Module 02). Both global, not per-symbol. Same "scheduler re-reads fresh each cycle" principle applies — increasing either takes effect on the scheduler's next run without a restart, and will simply start tracking additional further-out expiries from that point forward (it does not retroactively backfill history for newly-added weeks/months).

**`gex_term_structure_strike_range_pct`** (default 7) — controls how wide the strike range shown on the "GEX Next 3 Trading Days" card (Module 04, item 9) is, expressed as a percentage of the current spot price. For example, with default 7 and SPY spot at 590, only strikes within 590 ± (590 × 0.07) = 548–631 are shown. Applies to all index symbols on that card. Admin-configurable; the `/api/gex/term-structure` endpoint reads this value and filters `gex_by_strike` server-side before returning — the full per-strike data is never sent to the client when this filter is active.

## Collection: `post_templates`
Editable templates for the Twitter Post Generation Studio (Module 10). One document per post type.
```json
{
  "_id": ObjectId,
  "post_type": "premarket | eod | eow",
  "platform": "twitter",
  "template_text": "string (with [BRACKET] placeholders, may contain multiple tweets separated by a delimiter for thread posts)",
  "updated_by": "ObjectId (staff/admin user_id)",
  "updated_at": "datetime (Central Time)"
}
```
Indexes: `(post_type, platform)` unique.

## Collection: `social_posts`
Generated Twitter posts — drafts and published — for the Twitter Post Generation Studio (Module 10).
```json
{
  "_id": ObjectId,
  "post_type": "premarket | eod | eow",
  "platform": "twitter",
  "symbol": "SPX",
  "status": "draft | published | failed",
  "trigger": "scheduled | manual",
  "generated_by": "ObjectId (staff/admin user_id) | null (if scheduled)",
  "source_snapshot_ref": "ObjectId (FK into gex_intraday/gex_weekly)",
  "tweets": ["string", "string", "..."],
  "chart_path": "string (file path or object storage reference) | null",
  "tweet_ids": ["string", "..."],
  "published_at": "datetime (Central Time) | null",
  "error_detail": "string | null",
  "created_at": "datetime (Central Time)"
}
```
Indexes: `(post_type, created_at)`; `status`. **TTL index on `created_at` with `expireAfterSeconds = 5184000` (60 days)** — both drafts and published posts age out automatically after ~2 months, per Module 10's retention policy.

## Notes
- All "GEX by strike" arrays are stored embedded rather than in a separate collection — snapshot documents are read-mostly and rarely huge (strike counts are bounded), so embedding keeps reads simple (single query → full chart data).
- Per-strike `call_greeks`/`put_greeks` (delta, gamma, theta, vega, IV, open interest, **volume**) are stored alongside `call_gex`/`put_gex` so future features (unusual-flow detection, dealer-positioning analysis, richer per-strike tooltips) can be built without a schema migration — even though v1 dashboard views may only surface GEX and walls. `volume` was added specifically to support the parallel coordinates view (Module 04), which needed per-contract volume that wasn't previously captured.
- **Timezone standard: Central Time (`America/Chicago`) everywhere, in code and in the database.** All `datetime`/`timestamp` fields across every collection in this doc are stored as Central Time, not UTC. Use a tz-aware datetime library (Python `zoneinfo`/`pytz` with the `America/Chicago` zone) rather than a fixed offset, since Central Time shifts between CST (UTC-6) and CDT (UTC-5) with daylight saving — a hardcoded offset will silently produce wrong timestamps twice a year. This applies to `created_at`, `subscribed_at`, `run_at`, `processed_at`, `last_login_at`, etc. across all collections, not just the GEX snapshot collections.
- Use `trade_date` (date-only) fields alongside full `timestamp` everywhere so date-range queries don't need timezone gymnastics — `trade_date` should be derived from the Central Time `timestamp`, not from server-local or UTC time, so a snapshot taken late in the trading day doesn't get bucketed into the wrong day.
- `source` (`schwab | cboe | yfinance`) is stored on every snapshot so the admin pipeline-health view (Module 05) can show which data source actually served a given snapshot, especially when a fallback was used.
