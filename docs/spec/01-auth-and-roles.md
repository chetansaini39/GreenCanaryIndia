# Module 01 — Authentication & Roles

## Purpose
Handle account creation, login, session management, role-based routing, password reset, and email verification for Visitor, Free User, Paid User, Staff, and Admin.

## User stories
- As a visitor, I can view this week's data without creating an account.
- As a visitor, I can register using my Google account or an email + password.
- As a registered user, I can log in with Google or email + password.
- As a logged-in user, I'm routed to the correct landing page based on my role (User → Home Page, Admin → Admin Page).
- As a user, I can log out from anywhere via a persistent nav control.
- As staff/admin, I log in through the same screen but land on an internal page, not the public dashboard.
- As an email/password user, I can request a password reset link sent to my registered email if I forget my password.
- As a newly registered email/password user, I receive an email verification link and am prompted to verify my email.

## Routes (Flask)

| Route | Method | Auth required | Description |
|---|---|---|---|
| `/` | GET | No | **Homepage** — marketing landing page with hero, features, pricing, and CTA. Links to Login, Register, and "View today's data" (anonymous public view). The homepage replaces the bare "index page with three links" originally described here — see the homepage spec section below. |
| `/this-week` | GET | No | Public, read-only snapshot view, no symbol switching |
| `/contact` | GET | No | Contact / feedback page — accessible to everyone, logged in or not |
| `/contact` | POST | No | Submits the contact form — stores in `contact_submissions` (Module 03), sends an email notification to the configured admin address, returns a success/error JSON response (no page reload — the form submits via fetch/AJAX and updates the UI in place) |
| `/login` | GET/POST | No | Renders login form; POST validates credentials or completes Google OAuth |
| `/login/google` | GET | No | Initiates Google OAuth redirect |
| `/login/google/callback` | GET | No | OAuth callback; creates session, creates user record if first login |
| `/register` | GET/POST | No | Email/password registration form |
| `/register/google` | GET | No | Google OAuth registration (same callback path as login; if no existing user, create one) |
| `/logout` | POST | Yes | Destroys session, redirects to `/` |
| `/home` | GET | Yes (role=user/paid) | Landing page after login for regular users |
| `/dashboard` | GET | Yes (role=user/paid) | User Dashboard (see Module 04) |
| `/upgrade` | GET | Yes (role=user) | Stripe Checkout entry point (see Module 06) |
| `/admin` | GET | Yes (role=admin) | Admin landing page |
| `/staff` | GET | Yes (role=staff or admin) | Staff landing page |
| `/forgot-password` | GET/POST | No | GET renders the "enter your email" form; POST sends a reset link to the given email |
| `/reset-password/<token>` | GET/POST | No | GET renders the new-password form (validates token first); POST saves the new password and invalidates the token |
| `/verify-email/<token>` | GET | No | Marks the user's email as verified; one-click, no form |
| `/resend-verification` | POST | Yes | Resends the verification email to the logged-in user's address |
| `/account/api-keys` | GET | Yes (role=user/paid/admin/staff) | Lists the user's active API keys (label, created date, last used) — raw key is never shown after creation |
| `/account/api-keys` | POST | Yes | Generates a new API key — returns the raw key once (store it now, can't retrieve later), stores the hash |
| `/account/api-keys/<id>/revoke` | POST | Yes | Revokes (deactivates) an API key immediately |

## Roles & permissions

| Capability | Free User | Paid User | Staff | Admin |
|---|---|---|---|---|
| View 4 free symbols, limited lookback | ✅ | ✅ | ✅ | ✅ |
| View full symbol list, full lookback | ❌ | ✅ | ✅ (read-only, for support) | ✅ |
| Manage own subscription (upgrade/cancel) | ✅ | ✅ | n/a | n/a |
| View other users' account info | ❌ | ❌ | ✅ (read-only) | ✅ |
| Edit/refund/override subscriptions | ❌ | ❌ | ❌ | ✅ |
| View data pipeline health/logs | ❌ | ❌ | ✅ | ✅ |
| Manage symbol list / scheduler config | ❌ | ❌ | ❌ | ✅ |
| Create/deactivate staff accounts | ❌ | ❌ | ❌ | ✅ |
| Generate/preview Twitter posts (Module 10) | ❌ | ❌ | ✅ | ✅ |
| Publish a Twitter post / toggle auto-publish (Module 10) | ❌ | ❌ | ✅ | ✅ |

Confirmed as-is — no further changes needed to this table.

## Auth methods
1. **Google OAuth 2.0** — via `Flask-Dance` or `Authlib`. Store `google_id`, email, name, avatar URL on first login. Google-only users do not have a `password_hash` and cannot use the forgot-password flow (they sign in via Google).
2. **Email/Password** — store with `werkzeug.security.generate_password_hash` (or `bcrypt`). Standard login form with error messaging on failure (per diagram: "No, Nav to Login, with error").
3. **Admin/Staff login** — same `/login` form, but these accounts are *never* self-registered. They're created directly in MongoDB or via an admin-only "create staff" action (see Module 05). Distinguished by a `role` field, not a separate login page.

## Session handling
- Use Flask `session` with server-side session store (e.g. `Flask-Session` backed by MongoDB) — avoids large cookies and allows server-side session revocation (important for an admin "force logout" capability later).
- Session should carry: `user_id`, `role`, `subscription_status` (cached, refreshed on each Stripe webhook — see Module 06).

## Registration flow detail
- New user → Registration screen → on success → **auto-login, redirected directly into `/home`**.
- **Email verification email sent immediately on registration** — auto-login still happens (user isn't blocked), but a dismissible verification banner appears on `/home` and `/dashboard` until they verify. Banner includes a "Resend verification email" link.
- **Unverified users can use the app fully** — email verification is a prompt, not a gate, for v1. The only restriction: password reset requires a verified email (see below). This avoids locking out users who mistype their email on registration — they can still use the app and update their email address if needed.
- On registration failure → re-shown registration screen with inline error.
- On login failure → re-shown login screen with inline error ("NO, Nav to Login, with error").
- **Google OAuth registrations skip email verification entirely** — Google has already verified the email.

## Password reset flow
1. User clicks "Forgot password?" on the login page → `GET /forgot-password` renders a simple form (email input only).
2. User submits their email → `POST /forgot-password`:
   - If the email exists AND belongs to an email/password account (not Google-only) AND is verified: generate a secure reset token, store it hashed in `password_reset_tokens` (Module 03), send the reset email, show "If that email is registered, a reset link has been sent" — same message whether the email exists or not (prevents email enumeration).
   - If the email belongs to a Google-only account: show "That account uses Google Sign-In — please log in with Google instead," not the generic message.
   - If the email is unverified: show "Please verify your email address first — check your inbox for the verification email, or request a new one below." Don't send a reset link to an unverified address (it may not belong to the person requesting the reset).
3. User clicks the link in the email → `GET /reset-password/<token>`: validate token (exists, not expired, not already used) → render the new-password form. If invalid/expired, show a clear error with a link back to `/forgot-password`.
4. User submits a new password → `POST /reset-password/<token>`: re-validate token, update `password_hash`, mark the token `used: true`, invalidate any other unused tokens for this user, log the user in automatically, redirect to `/home`.

### Token security requirements
- Generate tokens with `secrets.token_urlsafe(32)` — 256 bits of entropy.
- Store only a hash of the token in the database (`hashlib.sha256`), never the raw token. The raw token only appears in the email URL.
- Token expiry: **1 hour** from creation.
- Single-use: mark `used: true` on first successful use; any subsequent attempt with the same token returns the same "invalid or expired" error.
- Rate-limit `POST /forgot-password`: max 5 requests/hour per IP (Flask-Limiter, same pattern as Module 08's rate limits) to prevent email flooding.

## Admin kill-switch: disable new registrations
- Admin can globally disable new user registration (both email/password and Google OAuth) from the admin panel (see Module 05) — e.g. for a beta cap, abuse response, or planned downtime.
- Backed by a single document in the `platform_settings` collection (see Module 03): `registration_enabled: bool`.
- Enforcement is **server-side on every registration entry point**, not just hidden in the UI:
  - `GET/POST /register` — if disabled, render the registration page with a "registration is currently closed" message instead of the form (GET), and reject POST attempts with the same message (in case someone bookmarks/replays the form).
  - `GET /register/google` — if disabled, redirect to `/register` with the same message rather than starting the OAuth flow.
  - `GET /login/google/callback` — this path doubles as registration for first-time Google users (per the routes table above). If `registration_enabled` is false and the callback would otherwise create a new user record, block account creation and show the same message — but allow the callback to proceed normally for *existing* users (this flag stops new signups, not existing-user logins).
- Existing users (free, paid, staff, admin) are **never** affected by this flag — they can always log in, view their dashboard, and manage their subscription regardless of the toggle's state.
- Cache this flag for at most a few seconds (or read it fresh on each registration request) so an admin's toggle takes effect immediately rather than waiting on a long-lived cache.

## Edge cases to handle
- Email already registered via Google, user tries password registration with same email → block with clear message: "An account with that email already exists — sign in with Google."
- User tries forgot-password on a Google-only account → tell them to use Google Sign-In instead (don't send a reset email, don't show the generic "if registered" message).
- User tries forgot-password with an unverified email → prompt them to verify first; don't send a reset link.
- Reset token clicked more than once → treat as invalid/expired on second use, show the same error as an expired token (don't reveal it was already used vs. expired).
- Session expiry mid-dashboard-use → redirect to `/login` with a "session expired" flash message, preserve intended destination via `next` param.
- Staff/Admin accounts must not be able to "Upgrade Subscription" (hide that nav item entirely for those roles).
- Staff/Admin password reset: same flow as regular users (they have email/password accounts too), no special handling needed.

## Data model (see Module 03 for full schema)
`users` collection fields relevant here: `_id`, `email`, `password_hash` (nullable if Google-only), `google_id` (nullable), `role` (`free|paid|staff|admin`), `email_verified` (bool), `email_verification_token` (string|null), `created_at`, `last_login_at`.
`password_reset_tokens` collection: new — see Module 03.
`contact_submissions` collection: new — see Module 03.

## Homepage (`GET /`)

The homepage is the public marketing landing page — not a bare link list. Key sections and requirements:

1. **Navigation bar** — logo left, links (Features / Pricing / Contact) center, "Log in" + "Get started free" right. "Get started free" routes to `/register`. If the user is already logged in, show "Go to dashboard" instead of the two auth buttons.
2. **Hero section** — headline, subheadline describing the platform in one sentence, two CTAs: "Start free — no card needed" (`/register`) and "View today's data" (`/today`, public anonymous view).
3. **Ticker strip** — live spot prices for SPY, QQQ, SPX, NDX, VIX. Pull from the most recent `gex_intraday` snapshot or a lightweight `/api/prices` endpoint — don't hardcode. Show a "Market closed" or last-updated label outside market hours.
4. **Features section** — four feature cards (GEX by strike, Weekly evolution, 12-week forward view, Monthly OPEX cycles). Static content, no dynamic data.
5. **Pricing section** — Free vs Pro, two-column card layout. Features listed per tier must match the current confirmed free/paid tier rules in Module 04. The Pro price comes from the Stripe price config — don't hardcode it in the template; read it from an env var or a `/api/pricing` endpoint so changing the price doesn't require a template edit.
6. **Bottom CTA** — "Today's GEX — no account needed" strip linking to `/today`.
7. **Footer** — copyright, Contact / feedback, Privacy, Terms links.

**Authentication-aware rendering:** the nav "Log in / Get started free" buttons collapse to "Go to dashboard" when the user already has an active session. Everything else on the page is identical regardless of auth state.

## Contact / feedback page (`GET /contact`, `POST /contact`)

### Page structure
1. **Message type selector** — pill/tab toggle with 4 options: Bug report / Feature idea / Data question / General. Selection is sent as the `type` field on submission. This is a required field — don't let the form submit without a type selected.
2. **Name field** — required, plain text.
3. **Email field** — required, validated as a valid email format. If the user is logged in, pre-fill this from their session's email address (saves a step, reduces friction).
4. **Message field** — required, textarea, min ~20 chars to prevent empty "test" submissions. Helper text: "Describe what you're seeing, what you expected, or what you'd like to see. For bugs: symbol, date, and expected vs. actual behavior is super helpful."
5. **Submit button** — "Send message". On click: validate all fields, submit via `fetch` to `POST /contact`, update the UI in place (show success state or inline error) without a full page reload.
6. **Success state** — replace the form with a confirmation message: "Message sent — we'll get back to you within 1–2 business days."
7. **Sidebar** (desktop layout) — response time badge, what-we-help-with list (data questions, billing, bugs), quick resource links (What is GEX?, Free vs Pro). Static content.

### Backend (`POST /contact`)
- Validate all fields server-side (don't rely only on client-side validation — a direct API call skips it).
- Store the submission in `contact_submissions` (Module 03) with `status: "new"`.
- **Email notification:** send an email to the admin address (`ADMIN_CONTACT_EMAIL` env var — see Module 07) with the submission details, so the team doesn't need to log into the admin panel to know something came in. Use the same email infrastructure as Module 01's password reset emails. No reply-to or auto-responder needed for v1 — the team replies manually from their own email client.
- **If the user is logged in:** attach their `user_id` to the submission so admins can see the submission in context on the user's account page (Module 05's admin panel).
- Rate-limit: 5 submissions/hour per IP (Flask-Limiter, same pattern as Module 08). Prevents spam without inconveniencing real users.
- Return JSON `{"ok": true}` on success, or `{"ok": false, "error": "..."}` on validation failure — the frontend handles the UI state update, no redirect needed.

### Admin access to submissions
Add a read-only view of `contact_submissions` to the admin panel (Module 05) — newest first, filterable by `type` and `status` (new / read / resolved). Staff can mark submissions as read or resolved. No public-facing reply mechanism in v1 — the team uses their own email client to follow up.
