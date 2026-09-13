# Module 05 — Admin & Staff Panel

## Purpose
Internal tooling for managing users, subscriptions, the symbol list, and monitoring the data pipeline.

## Access
- `/admin` — admin role only.
- `/staff` — staff or admin role.
- Both created via username/password only (no Google OAuth, no self-registration) — per your original requirement ("custom username and password for admin and staff to access pages restricted to users").

## Admin capabilities
1. **User management**
   - List/search all users (email, role, subscription status, created date).
   - Manually change a user's role (e.g. comp a free user to paid).
   - Deactivate/reactivate a user account.
   - Create/deactivate Staff accounts (username + password, set role=staff).
2. **Subscription overrides**
   - View Stripe subscription status synced per user.
   - Manually mark a subscription as comped/overridden (e.g. for friends/family/press) without going through Stripe.
3. **Symbol management**
   - Add/remove symbols from `symbols_config`, set `tier` (free/paid) and `asset_type` (index/stock).
   - Toggle a symbol active/inactive (stops the scheduler from pulling it).
4. **Pipeline monitoring**
   - View `pipeline_health` log: last run time per job, success/failure, symbols processed.
   - Manually trigger a re-run of a specific job for a specific symbol/date (useful if Schwab/CBOE had downtime).
   - **Backfill the forward-tracking window on demand** — a specific action for the `gex_weekly`/`gex_monthly_opex` jobs (Module 02), distinct from the generic single-date rerun above: "populate all N tracked weeks / M tracked cycles for [symbol or all symbols] right now," not just one date. Useful when this data doesn't exist yet at all (initial rollout), when a new symbol is added to `symbols_config`, or after an admin increases `gex_weekly_forward_weeks`/`gex_monthly_forward_cycles` (Module 05's Platform settings, below) and wants the newly-added further-out weeks/cycles populated immediately rather than waiting for the next scheduled EOD/Monday-Friday cycle. This reuses the exact same write logic as the scheduled job — "ensure today's data exists for each of the next N upcoming weeks" — just triggered on demand instead of by the clock, so it should be idempotent and safe to run even if some of the data already exists.
   - **Scheduler liveness warning**: show a prominent banner on this page if `scheduler_heartbeat.last_heartbeat_at` (Module 03/07) is older than a few minutes **while it's currently market hours** — this is the case that bit you once already (scheduler process down, nothing else surfaced it). Don't warn outside market hours, since a stale heartbeat overnight/weekends/holidays is expected, not a problem.
5. **Platform settings**
   - Toggle **new user registration on/off** (both email/password and Google) — a global kill-switch for signups, backed by `platform_settings` (Module 03). Does not affect existing users' ability to log in or use the platform.
   - Show the current state clearly in the admin UI (e.g. a visible "Registration: OPEN / CLOSED" badge) so it's not accidentally left off.
   - **Set the 0DTE capture interval** (`gex_0dte_interval_minutes`, default 15) — a single global setting (Module 02/03), not per-symbol (this is the only configurable capture interval; the platform only captures same-day-expiry/0DTE intraday data, no separate "nearest active contract" interval exists). Changing it should take effect on the scheduler's next cycle without needing a restart — surface this expectation in the UI (e.g. "takes effect within [interval] minutes") so an admin doesn't assume it's instant or assume it's broken if the very next tick still uses the old value. **Only allow values that are exact divisors of 60** (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60) — the scheduler uses clock-aligned firing (`CronTrigger(minute='*/N')`) per Module 02, and a non-divisor value would produce misaligned or inconsistent firing times. Show a validation error for any other value; don't silently accept it.
   - **Set the Weekly/Monthly OPEX forward-tracking window widths** (`gex_weekly_forward_weeks`, default 12; `gex_monthly_forward_cycles`, default 3 — Module 02/03) — two independent global settings. Increasing either starts tracking additional further-out weeks/cycles from that point forward only (no retroactive backfill); decreasing either should stop writing new documents for the now-out-of-window weeks/cycles going forward, but shouldn't delete existing history for them. Surface the scale impact in the UI when increasing these (e.g. a note like "increasing this raises API call volume to Schwab/CBOE — see Module 02's scale consideration") so an admin doesn't casually 10x the write volume without understanding the tradeoff.
   - **Set the "GEX Next 3 Trading Days" card strike range** (`gex_term_structure_strike_range_pct`, default 7 — Module 03/04) — the percentage band around spot price used to filter strikes on the term structure panels. For example, 7 means ±7% of current spot. Applies to all index symbols on that card. Filtered server-side in `/api/gex/term-structure`. Accepts any positive integer or decimal; no divisor-of-60 restriction (unlike the scheduler interval).
6. **Twitter Post Generation Studio** (Module 10 — staff have equal rights here, see below)
   - View post history (drafts + published, last 2 months per the retention policy).
   - Trigger on-demand generation/preview of any post type.
   - Manually publish a draft.
   - Toggle auto-publish on/off per post type.
   - Edit post templates (`post_templates`).
7. **Twitter integration test (admin only)**
   - A diagnostic page showing whether the Twitter API credentials configured in the environment (Module 07) are currently working, with two independent checks:
     - **Credentials check** — calls a read-only Twitter API v2 endpoint (e.g. `GET /2/users/me`) using the configured keys/tokens. No tweet is posted. Confirms the API key/secret/bearer token are valid and the authenticated account resolves correctly. Surfaces the authenticated account's handle on success, or the specific error (expired token, invalid key, rate-limited, etc.) on failure.
     - **Full test post** — posts a single, clearly-marked test tweet (e.g. `"🧪 Connectivity test from [platform name] — automated check, please ignore. [timestamp]"`) with a sample chart image attached, to verify the full posting pipeline including media upload — then **immediately deletes the tweet** via the Twitter API's delete-tweet endpoint. Reports success/failure for the post step and the delete step separately (so a successful post + failed delete is visible as a distinct outcome, not silently swallowed — that scenario leaves a real tweet live and the admin needs to know to clean it up manually).
   - Both checks are manually triggered button-clicks (not scheduled) — this is a "click to verify right now" diagnostic tool, not a recurring health check. Admin-only, not staff, since it's a credentials/infra-level check rather than day-to-day content work.
   - Log every test run (which check, success/failure, error detail, timestamp, run by whom) to `pipeline_health` (Module 03) using `job_name: "twitter_credentials_check"` or `"twitter_test_post"` — reuses the existing pipeline-monitoring collection/UI rather than creating a parallel one.
9. **API key oversight (MCP — Module 11)**
   - View all active API keys across all users — label, owner, created date, last used, call count (read from `pipeline_health` MCP call logs). Admin only.
   - Revoke any key immediately (useful if a key is suspected to be compromised or a user's account is deactivated).
10. **Contact submissions** — read-only list of `contact_submissions` from the `/contact` page. Newest first, filterable by `type` (bug/feature/data/general) and `status` (new/read/resolved). Staff can mark individual submissions as read or resolved. Clicking a submission that has a `user_id` links to that user's admin detail page. No in-app reply mechanism — team replies from their own email. Include a count badge on the menu item showing unread (status=new) submissions so the team notices new messages without having to actively check.
9. **Basic usage analytics** (optional, nice-to-have)
   - Daily active users, free→paid conversion count, most-viewed symbols.

## Staff capabilities (read-only subset, except Twitter Post Studio — see below)
- View user list and subscription status (no edit).
- View pipeline health log (no manual trigger).
- View full GEX data for any symbol (same as paid, for support/troubleshooting purposes when a user reports an issue).
- **Exception:** full read/write access to the Twitter Post Generation Studio (Module 10) — generate, preview, publish, and toggle auto-publish, same rights as admin. This is a content-production tool, not a user/billing-management one, so it's deliberately not part of the read-only pattern above.
- The **Twitter integration test** (item 7 above) is admin-only — staff can view past test results in the pipeline health log (read-only, same as any other job) but cannot trigger a new test run.

## Routes
| Route | Method | Role | Description |
|---|---|---|---|
| `/admin/users` | GET | admin | User list/search |
| `/admin/users/<id>/role` | POST | admin | Change a user's role |
| `/admin/users/<id>/deactivate` | POST | admin | Deactivate account |
| `/admin/staff/create` | POST | admin | Create new staff account |
| `/admin/symbols` | GET/POST | admin | Manage `symbols_config` |
| `/admin/pipeline` | GET | admin, staff | View health log |
| `/admin/pipeline/<job>/rerun` | POST | admin | Manually re-trigger a job |
| `/admin/pipeline/backfill-forward-window` | POST | admin | Backfill all N tracked weeks / M tracked cycles for one symbol or all symbols, on demand |
| `/admin/settings/registration` | POST | admin | Toggle `registration_enabled` on `platform_settings` |
| `/admin/settings/capture-intervals` | POST | admin | Set `gex_0dte_interval_minutes` on `platform_settings` |
| `/admin/settings/forward-windows` | POST | admin | Set `gex_weekly_forward_weeks` / `gex_monthly_forward_cycles` on `platform_settings` |
| `/admin/settings/term-structure-range` | POST | admin | Set `gex_term_structure_strike_range_pct` on `platform_settings` |
| `/admin/contact` | GET | admin, staff | Contact submissions list — filterable by type and status |
| `/admin/contact/<id>/status` | POST | admin, staff | Mark a submission as read or resolved |
| `/admin/social` | GET | admin, staff | Twitter Post Generation Studio — history + drafts (Module 10) |
| `/admin/social/generate/<post_type>` | POST | admin, staff | On-demand generate/preview a draft (Module 10) |
| `/admin/social/<post_id>/publish` | POST | admin, staff | Manually publish a draft (Module 10) |
| `/admin/social/settings/autopublish` | POST | admin, staff | Toggle per-post-type auto-publish (Module 10) |
| `/admin/social/templates` | GET/POST | admin, staff | View/edit `post_templates` (Module 10) |
| `/admin/integrations/twitter` | GET | admin | Twitter integration test page — shows latest test results from `pipeline_health` |
| `/admin/integrations/twitter/test-credentials` | POST | admin | Run the credentials-only check (no tweet posted) |
| `/admin/integrations/twitter/test-post` | POST | admin | Run the full test (post + attach chart + auto-delete) |
| `/staff/users` | GET | staff | Read-only user list |

## Edge cases
- Prevent an admin from demoting their own account to a non-admin role (lockout risk) — block self-role-change, require a second admin account to do it.
- Audit log every role change and manual subscription override (who did it, when, old value → new value) — store in a `admin_audit_log` collection. Also log registration on/off toggles here (who flipped it, when, old → new state) — same audit trail, same collection.
- **Twitter full test post succeeds but the delete fails** — surface this prominently and distinctly from a normal failure (e.g. a red banner: "Test tweet posted successfully but could not be auto-deleted — tweet ID [X] is still live, delete it manually") rather than just logging a generic error. This is the one failure mode in this feature that leaves a visible side effect on the real Twitter account if not caught.
- Sample chart image for the full test post: use a static placeholder chart bundled with the app (not a live-generated one) so the test doesn't depend on Module 02's pipeline having fresh data — the point of this test is isolating "is Twitter posting working," not "is the whole pipeline working."
