# Module 10 — Twitter Post Generation Studio (Staff/Admin)

## Purpose
Generate the platform's "content marketing" Twitter posts — Pre-Market brief, EOD thread, EOW thread — using real GEX data, auto-rendered charts, and templated explanatory copy. Staff/admin use Social Studio to review, edit, and publish. The EOD pipeline is always review-required and can never auto-publish.

This is **separate from Module 08's existing automated bot.** Module 08 posts a random free-tier symbol snapshot 3x/day purely to drive visitor funnel traffic (no human involved, ever). This module is the higher-production-value SPX "daily GEX brief" content engine described in the market-research workbook — different goal (build a FinTwit following / credibility), different content shape (multi-tweet threads, branded charts, narrative copy), different access model (staff/admin can see and intervene).

## Source material
Templates and visual references came from `Market Research.xlsx`, especially **"EOD-Twitter Post" B1:B60**, **"Pre-Market -Twitter Post"**, and **"Chart Formats Update"**.

## Post types & schedule

| Post type | Format | Cadence (per workbook) | Symbol | Chart attached |
|---|---|---|---|---|
| **Pre-Market brief** | Single tweet | **7:45 AM CST (confirmed)** | SPX | No chart — text only, links back to prior EOD thread |
| **EOD report** | 5-tweet thread | **3:15 PM CT / 4:15 PM ET on trading days** | SPX | Five content-only images — one corresponding image per thread post |
| **EOW wrap** | 6-tweet thread | Fridays, 4:30–5:30pm ET (≈ 3:30–4:30pm CT) | SPX | Yes — weekly GEX-by-strike chart |

All times stored/scheduled in Central Time per the platform-wide timezone standard (Module 03/07). SPX is the only symbol covered by these templates for v1 — extending to other symbols is a future enhancement, not required now.

## Relationship to scheduled automation
- A scheduled job (same scheduler process as Module 02) fires at each post type's target time, pulls the latest relevant GEX data, fills the template, and renders the chart image.
- The EOD job creates one idempotent draft per SPX trade date using `scheduled:eod:SPX:<trade-date>`. It never calls the X publishing path; a staff/admin must explicitly approve and publish it.
- Staff/admin can disable auto-publish per post type (a toggle, same pattern as the registration kill-switch in Module 01/05) — when disabled, the scheduled job still generates the post and chart at the target time but leaves it as a **draft** for a human to review and manually publish from the studio.
- EOD has no auto-publish toggle. Social Studio displays **Review required** instead.
- Staff/admin can also trigger generation **on demand**, any time, independent of the schedule — this always creates a draft (never auto-publishes), so they can preview/verify before deciding to publish. This covers "generate chart and verify data and keep track" even when the scheduled run hasn't fired yet.
- If no staff/admin approves an EOD draft, nothing is posted to X.

## Access
- `/social` is gated by `role in (staff, admin)`. Both roles can view history, create manual drafts, edit templates, and manually publish. Auto-publish toggles remain for non-EOD post types only.

## Manual EOD previews

Social Studio can exercise the complete EOD draft workflow without production SPX data.
Staff/admin can use the built-in positive/negative examples or upload a JSON/`.xlsx` file up
to 5 MB. Inputs support two modes:

- **Raw contracts** — validate same-day SPX contracts, then run the shared production GEX,
  wall, hot-zone, regime, and zero-gamma calculations. Historical trade dates are allowed,
  but expiry, snapshot-time, IV/OI, sign, wall, and crossing validation still applies.
- **Calculated metrics** — validate supplied scalar metrics and billion-denominated
  per-strike exposures, then pass them into the production template and renderer.

The file can supply the six bounded narrative fields, or the user can explicitly select LM
Studio generation. Uploads are parsed in memory and never written to market-data collections.
Successful records use `trigger: "preview"` and `preview_only: true`, retain their input mode,
format, filename, and narrative source, and contain the normal five posts and five media assets.
The UI and publishing service both block these records from X before credential or media checks.
Deleting a preview uses the normal EOD media cleanup path.

## Content generation pipeline
1. **Data pull** — for EOD, read the same-day `gex_rolling_21d` SPX snapshot, reject stale/missing data, filter `options_slice` to that trade date's 0DTE expiry, and recompute GEX. Holidays and empty 0DTE slices fail generation.
2. **Analysis** — derive regime/dealer positioning, convert net and strike exposure to billions, and calculate the zero-gamma crossing nearest spot from stored IV/OI on a ±20% Black–Scholes spot grid using snapshot time and 3:00 PM CT expiry.
3. **Bounded narrative** — LM Studio receives validated metrics and allowed levels only, at temperature `0.2`. It returns six short JSON prose fields. Invalid JSON gets one repair retry; any model-authored digit is rejected so the model cannot invent levels.
4. **Template fill** — calculations, numbers, five-post order, formatting, and hashtags are deterministic and follow the workbook template.
5. **Media render** — generate five required `1200×675` PNGs before inserting an EOD draft: chart, regime, levels, playbook, and verdict. Any missing data, text overflow, invalid PNG, or file over 5 MB prevents draft creation and removes partial files.
6. **Assemble draft** — enforce exactly five X-safe posts, no unresolved tokens, and media indexes `[0,1,2,3,4]`; store `media_version: 4`, a validated theme snapshot, the ordered asset manifest, analysis, model, validation, source trade date, and schedule key. `chart_path` remains an alias to image 1 for compatibility.
7. **Publish (manual for EOD)** — validate and upload all five images, then set each image's alt text before creating post 1. Create the reply chain with exactly one matching media ID on each post using OAuth 1.0a user credentials.
8. **Record outcome** — save all post IDs. If a later reply fails, preserve created IDs under a partial-failure status for manual cleanup.

## Chart formats
Two distinct visual styles found in the workbook — use these as the v1 target, not the older unbranded "Current Charts" versions:

1. **EOD five-image thread** — institutional, content-only research cards using bundled IBM Plex Sans/Mono typography. Post 1 uses a focused strike chart plus positioning rail; post 2 uses a two-column regime note; post 3 stacks call wall, put wall, and hot zone vertically; post 4 uses a compact scenario matrix; and post 5 combines the verdict with a four-level risk strip. A slim footer contains the configured logo or `GX` fallback, display name, handle, and source date. Images never imitate X chrome: there is no fake avatar/header, post timestamp, thread line, post numbering, engagement UI, or hashtags. Social Studio centers each image at a responsive maximum width of 760 px immediately before its editable accessible X text and warns that text edits do not recompute media.
2. **Weekly chart for EOW** — same general visual language, scoped to the weekly expiry's strikes/data, used as the attached chart for the **EOW wrap** thread.

> **Branding:** use the dark theme with **GEX Intelligence / @gexintelligence**. Staff and admins may change the display name, handle, optional square PNG logo, and approved palette in Social Studio. Validation enforces field lengths, dark surfaces, accessible contrast, safe logo bounds, and hex colors. Geometry and typography are fixed. Theme changes affect new drafts only, and every EOD draft stores its theme snapshot.

## Data persistence & retention
- New collection `social_posts` stores every generated post (draft or published) — see Module 03 for the schema.
- **Retain 2 months of history**, then purge — implement via a MongoDB TTL index on `created_at` with `expireAfterSeconds` set to 60 days. Drafts that were never published also age out after 2 months along with published ones (simplest single retention rule, no separate cleanup logic needed for drafts vs. published).
- The chart PNG itself: store either as a binary blob in the document (fine at this volume/size) or write to disk/object storage and store a path/URL reference — recommend file storage + reference, to keep `social_posts` documents small and fast to list in the studio UI.
- Deleting an EOD draft removes its generated media directory. Version-2 mock-X and version-3 content-only drafts remain fully viewable with all five images, while older single-image drafts retain their fallback view. All legacy versions must be regenerated as version 4 before publishing.

## Confirmed decisions
- Pre-Market go-time: 7:45 AM CST.
- Staff have full publish rights in this studio, same as admin (not read-only like Module 05's general staff pattern).
- EOD go-time: 3:15 PM CT on trading days, always review-required.
- Chart branding: GEX Intelligence / @gexintelligence, config-driven.
- Reddit post templates (same workbook, more detailed): out of scope for now — the `social_posts` schema (Module 03) stays generic (`platform: "twitter"`) so this can extend later without a redesign.
