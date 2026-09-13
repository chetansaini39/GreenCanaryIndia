# Module 02 — GEX Data Pipeline & Collection Engine

## Purpose
Pull options data from Schwab and CBOE (with yfinance as a fallback/supplement when either is unavailable or rate-limited), compute/store Gamma Exposure (GEX) for tracked symbols on multiple schedules, and expose it to the app via DB read requests. This runs as a separate background process from the Flask web app (likely a long-running scheduler script or a set of cron-triggered jobs).

## Data sources

### Schwab API (primary — index 0DTE, intraday, weekly, monthly)
- **Library:** `schwab-py`
- **Auth:** OAuth2 Authorization Code flow (one-time browser login), token cached at a configurable path (e.g. `/tmp/token.json`), auto-refreshed by the library on subsequent calls.
- **Callback URL:** `https://127.0.0.1:8182` (local redirect for the one-time auth handshake).
- **Rate limit:** 120 requests/minute per app — comfortably within range for scheduled polling across the full symbol universe, but worth tracking as the paid symbol list grows.
- **Key methods used:** `get_option_chain()` for the options chain (gamma, open interest, delta/theta/vega, IV, strike, expiration), `get_price_history_every_five_minutes()` for intraday OHLCV bars used in chart overlays.
- **⚠️ Critical operational constraint:** the process that holds the Schwab client (the scheduler) must run as a single instance / single worker. Multiple concurrent processes touching the same token file can corrupt it and cause duplicate scheduler runs. Since the Flask web app never calls Schwab directly (see Pipeline stages below), this constraint applies to the scheduler process only — but it must be enforced there (e.g. only one scheduler instance running at a time, not multiple Gunicorn-style workers for that process). See Module 07 for how this affects deployment.

### CBOE Delayed Quotes API (supplemental index data)
- **Base URL:** `https://cdn.cboe.com/api/global/delayed_quotes/options/`
- **Auth:** none (public API).
- **Data delay:** ~15–20 minutes behind real-time — factor this into how "live" the dashboard claims to be for CBOE-sourced data.
- **Polling cadence:** roughly once per hour is sufficient given the delay; no official rate limit documented.
- **URL pattern:** underscore-prefixed ticker is the primary form for indices (e.g. `_SPX.json`, `_NDX.json`, `_VIX.json`); fall back to the non-prefixed form (`SPX.json`) automatically if the primary URL raises a parsing error.
- **Field extraction:** the option symbol embeds ticker/expiry/type/strike (e.g. `"SPX  240419C05430"`) and must be parsed via pattern matching to get type, strike, and expiration.

### yfinance (fallback/supplement)
- **Library:** `yfinance` (PyPI), no auth required.
- **Role in this platform:** fallback when Schwab/CBOE calls fail or are rate-limited, and as a supplemental source for historical OHLCV (`ticker.history()`) and option chains (`ticker.option_chain(expiry)`) on symbols where Schwab/CBOE coverage is thin.
- **Rate limit:** unofficial, ~2,000 requests/hour per IP — use caching to stay well under this.
- **Note:** intraday intervals (`<1d`) are limited to the last 60 days of history on the free tier — fine for the rolling/recent views this platform needs, but not a substitute for the platform's own permanent Mongo history.
- **Fallback logic:** `db_writer.py` should tag the document's source field (`schwab | cboe | yfinance`) so it's visible (to admins, and optionally to users) when a snapshot came from the fallback path rather than the primary source.

## Timezone standard
All scheduling times below, and all timestamps written to MongoDB by this pipeline, use **Central Time** (`America/Chicago`). Note this is *not* a fixed UTC-6 offset — Central Time alternates between CST (UTC-6, winter) and CDT (UTC-5, summer) — so implement with a tz-aware library (e.g. Python's `zoneinfo`/`pytz` with the `America/Chicago` tz, not a hardcoded offset) so daylight saving transitions are handled automatically. See Module 03 for how this applies to stored documents, and Module 07 for the `TIMEZONE` env var that should drive this app-wide.

## Granularity definitions (what each view actually measures)
These three are easy to conflate — they differ in *what's being measured*, not just how often:

- **Intraday** (`gex_intraday`, `snapshot_type: "intraday"`) — **0DTE only.** This collection only ever holds same-day-expiring contract data; there is no general "nearest active contract" fallback for symbols without a same-day expiry. Captured on a **configurable interval, default 15 minutes** (`platform_settings.gex_0dte_interval_minutes`, Module 03/05 — admin-adjustable without a deploy).
  - **Clock-aligned firing (required):** the scheduler must fire at exact multiples of the interval on the clock, not N minutes from whenever the process started. For a 15-minute interval this means :00, :15, :30, :45 past every hour; for 5 minutes this means :00, :05, :10, :15 ... :55 past every hour. Use APScheduler's `CronTrigger` with `minute='*/N'` syntax (e.g. `minute='*/15'`) rather than a plain `IntervalTrigger` from startup time — the latter fires at startup-time + N, which drifts away from clock boundaries on every restart and makes the time selection boxes on the dashboard (Module 04) show timestamps that don't align with what users expect.
  - **Valid interval values:** only values that are exact divisors of 60 produce meaningful clock-aligned times — i.e. 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60. An admin setting `gex_0dte_interval_minutes` to a non-divisor (e.g. 7 or 13) would produce misaligned or irregular firing times. The admin settings page (Module 05) must validate this: only allow values from the valid set, and show a clear error if an invalid value is submitted rather than silently accepting it and producing confusing behavior.
  - **Index symbols** (SPX, NDX, SPY, QQQ, IWM) — every trading day, since they always have same-day expiries. **The same option chain pull also captures the next 3 upcoming trading days' expiries** (term structure — see below), embedded in the same document. This isn't a separate scheduled job; it's the same pull, sliced by days-to-expiration.
  - **Mag7 stocks** — **Fridays only**, since that's their weekly expiry day and the only day they have a same-day-expiring contract. Monday through Thursday, stocks simply have **no intraday data** — not sparse, genuinely none for those days. See Module 04 for how the dashboard should communicate this rather than rendering an empty/broken chart. Term structure does not apply to stocks (index-only feature).

## Term structure (formerly "1DTE/2DTE/3DTE") — retired terminology, now a permanent dashboard card
What used to be separate "1DTE/2DTE/3DTE" selector options in the dashboard's granularity dropdown has been retired as a UI concept. It's now: a single **"EOD GEX Analysis" card**, always visible on the dashboard for index symbols (paid-tier only, regardless of symbol — see Module 04), showing the next 3 upcoming trading days' GEX side by side, each panel titled by its **actual expiration date** (not a "1DTE"/"2DTE"/"3DTE" label — that terminology no longer appears anywhere in the UI).
- **Data source:** embedded in the same `gex_intraday` document as the 0DTE data (Module 03's `term_structure` field) — captured from the same option chain pull, same interval, not a separate scheduled job.
- **Trading-day math, not calendar-day math:** "1/2/3 trading days out" must skip weekends and holidays — reuse the same holiday/market-closed calendar logic already used to gate the schedulers (see the table above), don't reimplement separately. Example: on a Thursday, the 3 panels show Friday, Monday, and Tuesday's expiries (skipping the weekend) — not Friday/Saturday/Sunday.
- **"Latest available" semantics:** the card always shows whatever the most recent `gex_intraday` document's `term_structure` contains — during market hours this updates every interval along with 0DTE; outside market hours (evenings, weekends, holidays) it simply shows the last captured data rather than going blank or erroring.
- **Weekly** (`gex_weekly`) — **tracks the next N upcoming Friday expiries in parallel, not just one.** `N = platform_settings.gex_weekly_forward_weeks`, default **12**, admin-configurable (Module 03/05). Each of the N tracked weeks gets its own *daily time-series* — the EOD job writes one document per symbol per tracked week per trading day. Concretely: on any given day, the scheduler is simultaneously writing "today's update" for this week's expiry, next week's, the week after, ... out to the 12th week out. This is what powers two distinct dashboard views (Module 04): the original "how did Monday's expectation for *this* Friday change by Wednesday" (day-by-day evolution toward one expiry, paid-only full detail) and the new "how does the GEX picture differ between this week, 5 weeks out, and 12 weeks out, as of today" (a forward summary trend, available to free users too). **Sliding window, not a special case:** each day, just compute "next N upcoming Fridays from today" — a Friday that passes simply stops being in that set; no explicit rollover/cleanup logic needed. Applies to all symbols (index + Mag7 stocks).
- **Monthly OPEX** (`gex_monthly_opex`) — **tracks the next M upcoming monthly OPEX cycles in parallel.** `M = platform_settings.gex_monthly_forward_cycles`, default **3**, admin-configurable (Module 03/05). Each tracked cycle gets a twice-weekly evolution (Monday EOD + Friday EOD). Applies to all symbols — index group and Mag7 stocks both have a standard monthly (3rd Friday) expiration cycle.
  - **Sliding window (this replaces what used to be a special-cased rollover rule):** each Monday/Friday capture, compute "the next M upcoming 3rd-Friday dates from today." When a cycle's expiry Friday arrives, it simply stops being "upcoming" and a new cycle enters the window from the far end — no explicit "is today expiry day, jump to next cycle" branch needed. This is a cleaner restatement of the same idea an earlier version of this doc handled with an explicit special case; the sliding-window framing (borrowed from how Weekly above now works) makes that special case unnecessary.
  - Example: with M=3, starting from a date before September 2026's expiry (Sep 18), the system tracks Sep 18, Oct 16, and Nov 20 in parallel. Once Sep 18 passes, the window automatically becomes Oct 16, Nov 20, Dec 18 — no code branch fired specifically "because" Sep 18 was expiry day, it's just that Sep 18 no longer satisfies "upcoming."

## Forward trend summary views (free + paid) vs. full detail (paid only)
Both Weekly and Monthly OPEX now produce two different things, and it's important these stay distinct in implementation:
1. **Full per-strike detail for any one tracked week/cycle** — same `gex_by_strike` shape as always, paid-only (consistent with the existing next-week-is-paid-only rule, Module 04). A free user can see *this* week's current-day snapshot in full detail, but not next week's or any further-out week's full strike-by-strike data.
2. **A forward summary trend** — just `net_gex` (and maybe `call_wall`/`put_wall`) plotted across all N tracked weeks / M tracked cycles as of today, with no per-strike breakdown. This is available to **free users too** — it's a coarser view that doesn't expose the detail the paid tier is gating, just a trend line. See Module 04 for the UI and API split between these two.
  - **Rollover rule:** when the Friday capture lands *on* the actual 3rd-Friday expiry day itself, that run does **not** capture data for the cycle that's expiring (the `gex_intraday` 0DTE collection already covers that day in full detail — for stocks, the 3rd Friday is also a weekly-expiry Friday, so they have 0DTE intraday data that day same as the index group) — instead, it immediately rolls forward and captures the **first data point for next month's OPEX cycle**. So every Friday run produces a write; on 3 of the 4 (or 5) Fridays a month it's "another point in the current cycle's evolution," and on the expiry Friday specifically it's "the first point of the next cycle."
  - Example: October 2026's 3rd Friday is Oct 16. Starting the Monday after September's expiry, every Monday and Friday EOD captures a new `gex_monthly_opex` document with `expiry_date: 2026-10-16` and a `trade_date` of that Monday/Friday — right up through Oct 16 itself, where instead of one more "October cycle" point, that Friday's run captures the first point of the **November** cycle (`expiry_date: 2026-11-20`).
  - This mirrors the `gex_weekly` daily-evolution fix (Module 03) but at 2 points/week instead of 5, and resolves what was previously an open item in this doc.
- **Rolling 21-day EOD** (`gex_rolling_21d`) — one EOD snapshot per symbol per trading day, queried as a trailing 21-day window at read time (see Module 03's retention note). This is a trend strip, not tied to any specific expiry. (Renamed from "Rolling 5-day" — the window was widened to give a fuller trend picture; the collection/field name changed to match.)

## Symbol universe
- **0DTE Index group** (checked daily, intraday): SPX, NDX, SPY, QQQ, IWM
- **Stock group** (weekly expiry only, 0DTE just on Friday): Mag7 — AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA. This is the confirmed paid-tier stock list for v1 (read from a configurable `Stock List` / `symbols_config` so it can be extended later without a code change).

## Scheduling (from diagrams)
All times below are Central Time.

| Job | Frequency | Notes |
|---|---|---|
| Trading-hour gate | Continuous | `Is Trading Hour` check gates all schedulers; if market closed/holiday → "Stop Running Schedulers" |
| Holiday/market-closed check | Daily, pre-market | If holiday → skip all jobs for the day |
| Intraday scheduler | **Clock-aligned at multiples of the configured interval, default every 15 min** (`platform_settings.gex_0dte_interval_minutes`). Fires at :00/:15/:30/:45 past every hour for the default 15-min interval — use `CronTrigger(minute='*/15')` not `IntervalTrigger`. 8:45 AM–2:55 PM CT window. | Index: SPX, NDX, SPY, QQQ, IWM, every trading day. Stocks: Fridays only (the only day they have a same-day expiry) — no intraday capture for stocks Monday–Thursday |
| EOD scheduler | **9:00 AM CT and 2:50 PM CT daily** (two runs per trading day) | Triggers both runs: Save Rolling 21-day GEX (EOD); Save Weekly GEX — **writes one document per symbol PER each of the N tracked upcoming weeks** (`gex_weekly_forward_weeks`, default 12). The **9:00 AM run** captures the morning state of each tracked expiry (useful for seeing how GEX looks at open vs. close, especially for the Weekly GEX card's day-by-day evolution). The **2:50 PM run** is the primary EOD capture. Both runs are otherwise identical in scope and write logic — same idempotent upsert pattern, same symbol universe. |
| Monthly OPEX scheduler (originally labeled "Weekly scheduler" in the source diagram — renamed here since "weekly" now also refers to the unrelated `gex_weekly` daily-evolution job above) | **Monday and Friday EOD, twice/week** | Triggers: Save Monthly OPEX GEX — **all symbols** (index group + Mag7 stocks), **writes one document per symbol PER each of the M tracked upcoming cycles** (`gex_monthly_forward_cycles`, default 3 — see Granularity definitions above) |
| Legacy/simple timer (from Schedule_Gex_Collection / TickerTracking diagrams) | 9:00 AM, 12:00 PM, 2:30 PM CT | Coarser fallback/legacy schedule — reconcile with the more granular scheduler above; recommend keeping the granular one as source of truth and treating 9/12/2:30 as the original MVP version being superseded |

## Pipeline stages
1. **Scheduler trigger** fires (per table above).
2. **Data Download Engine** pulls raw options chain data from Schwab API (primary) or CBOE (supplemental index data), falling back to yfinance if both fail or are rate-limited, for the relevant symbol(s).
3. **GEX calculation** — compute gamma exposure by strike, dealer positioning, call wall / put wall levels, using the formula below.
4. **Insert Into DB** — write computed result as a JSON-shaped document into the appropriate MongoDB collection (see Module 03), tagged with symbol, date, snapshot type (0dte/intraday/weekly/monthly/rolling5d), and data `source` (schwab/cboe/yfinance).
5. **Read path** (separate from write): Flask app issues a DB Read Request → Mongo query → JSON returned to frontend. This is decoupled from the collection engine — the web app never calls Schwab/CBOE/yfinance directly, only reads from Mongo.

## GEX calculation formula
Carried over from the existing personal tool — this is the core IP, applied consistently regardless of data source:

```
GEX = spot_price × gamma × open_interest × contract_multiplier × spot_price × 0.01
```
Where `contract_multiplier = 100` (standard for equity/index options). Simplified: `GEX = spot² × gamma × open_interest`.

**Sign convention:**
- `CALL` → GEX is **positive** (dealer long gamma, stabilizing).
- `PUT` → GEX is **negative** (dealer short gamma, amplifying).

```python
df["GEX"] = spot_price * df["gamma"] * df["open_interest"] * 100 * spot_price * 0.01
df.loc[df["type"] == "P", "GEX"] *= -1
gex_by_strike = df.groupby("strike")["GEX"].sum()
```

Aggregate by strike for `gex_by_strike`; net GEX is the sum across all strikes; call wall / put wall are the strikes with the largest positive / largest negative GEX respectively.

## Components to build
- `scheduler.py` — APScheduler (or similar) process running independently of Flask, with jobs registered per the table above. Should be restart-safe (if the Mac reboots, scheduler relaunches — see Module 07 for process management via `launchd` or `pm2`). Must run as a single instance (see Schwab constraint above).
- `data_sources/schwab_client.py` — Schwab API auth (OAuth token refresh via `schwab-py`) + options chain / price history fetch.
- `data_sources/cboe_client.py` — CBOE delayed-quotes fetch, including primary/fallback URL handling and option-symbol parsing.
- `data_sources/yfinance_client.py` — yfinance-based fallback fetch (OHLCV history, option chain) used when Schwab/CBOE fail or are rate-limited.
- `gex_engine.py` — pure calculation functions: input = options chain (from any of the three sources, normalized to a common shape first), output = GEX by strike, call wall, put wall, net GEX, dealer positioning summary.
- `db_writer.py` — normalizes calc output into Mongo document shape (including `source` field), handles upserts (avoid duplicate snapshots if a job re-runs).
- `db_reader.py` — query functions used by Flask routes (e.g. `get_intraday(symbol, date)` — returns the day's 0DTE series, or empty/no-data for stocks on non-Friday dates, `get_weekly(symbol, expiry_date, trade_date=None)` — defaults to latest `trade_date` for that tracked week if omitted; `expiry_date` (not `week_of`) selects which of the N tracked weeks, consistent with Module 03's schema, `get_weekly_trend(symbol)` — new, returns the free-accessible summary across all N tracked weeks, `get_monthly_opex(symbol, expiry_date, trade_date=None)` — same latest-if-omitted default, `get_monthly_trend(symbol)` — new, same summary pattern for the M tracked cycles, `get_rolling_21d(symbol)` — renamed from `get_rolling_5d`).

## Idempotency & error handling
- Every insert should be an **upsert** keyed on `(symbol, snapshot_type, timestamp)` to safely handle scheduler overlap/retries.
- Retry policy for Schwab/CBOE/yfinance calls: max 3 retries, initial delay 5 seconds, exponential backoff (delay doubles after each attempt), catching connection/timeout/SSL-type errors specifically. After exhausting retries on the primary source, fall back to the next source in priority order (Schwab → CBOE → yfinance, or CBOE → yfinance for index-only data) before giving up and logging a skip — do not crash the scheduler process. Surface failures in an admin-visible health log (Module 05), including which source ultimately served the data.
- If market is a holiday: skip cleanly, log "skipped — holiday" rather than erroring.

## Scale consideration: write/API-call volume
Tracking N=12 weeks and M=3 monthly cycles in parallel, across all ~12 symbols (5 index + 7 Mag7), is a meaningful multiplier over the original single-cycle design:
- **Weekly:** up to 12 × 12 = 144 `gex_weekly` writes/day (was 12/day under the old single-week model) — each requiring its own options chain pull for that specific expiry, so this is also ~12x the Schwab/CBOE API calls for this job specifically.
- **Monthly OPEX:** up to 12 × 3 = 36 writes per Monday/Friday (was 12 under the old model) — similarly ~3x the API calls for this job.
- Both are well within Schwab's 120 req/min rate limit (Module 02's API spec) for a symbol universe this size, but worth keeping in mind if the symbol list or N/M grow significantly later — this is exactly the kind of thing `gex_weekly_forward_weeks`/`gex_monthly_forward_cycles` being configurable protects against (an admin can dial it back without a code change if rate limits or storage become a concern).

## Confirmed decisions
- Paid stock list is Mag7 only for v1 (see Symbol universe above).
- yfinance fallback is logged only (`source` field on the document, visible to admins in pipeline health) — no user-facing "secondary source" indicator on the dashboard.
- **No new data collection/pipeline work needed for the "12-Week Comparison" / "3-Month Comparison" dashboard view (Module 04, item 2)** — `gex_weekly` and `gex_monthly_opex` already store full per-strike detail (`gex_by_strike`) for every tracked week/cycle as part of the existing forward-tracking sliding window. This is purely a new API surface and frontend rendering mode over data that's already being captured; if this view shows no data, the bug is in the new `weekly-comparison`/`monthly-comparison` API endpoints or the frontend, not in the scheduler.
- **The scheduler that populates this data already exists** — the EOD scheduler (writes `gex_weekly` for all N tracked weeks daily) and the Monthly OPEX scheduler (writes `gex_monthly_opex` for all M tracked cycles, Monday/Friday) are both already specified above. What's new is the ability to **manually trigger a backfill of the full tracked window on demand** from the admin panel (Module 05) — useful for initial rollout (this data may not exist yet at all) or after increasing N/M and wanting the newly-added further-out weeks/cycles populated immediately rather than waiting for the next scheduled run.
