# Module 06 — Billing (Stripe)

## Purpose
Handle the free→paid upgrade flow, recurring billing, and keeping the user's `role`/`subscription_status` in MongoDB in sync with Stripe's source of truth.

## Flow
1. Free user clicks **Upgrade Subscription** → `GET /upgrade`.
2. Backend creates a Stripe Checkout Session (mode=subscription, price = your monthly/annual plan price ID), with `client_reference_id` = MongoDB `user_id`, and redirects the user to Stripe's hosted checkout page.
3. On success, Stripe redirects back to `/upgrade/success`; on cancel, `/upgrade/cancelled`.
4. Stripe sends webhook events to `/webhooks/stripe` — **this, not the redirect, is the source of truth for activation** (redirect can be spoofed/closed early; webhook is authoritative).
5. Webhook handler updates `users.subscription_status`, `users.role` (free→paid), `users.stripe_customer_id`, `users.stripe_subscription_id`, and logs the event in `subscriptions_log`.

## Webhook events to handle
| Event | Action |
|---|---|
| `checkout.session.completed` | Set `role=paid`, `subscription_status=active`, store customer/subscription IDs |
| `invoice.payment_failed` | Set `subscription_status=past_due`; optionally email user |
| `customer.subscription.deleted` | Set `role=free`, `subscription_status=canceled` |
| `customer.subscription.updated` | Sync status (e.g. trialing, active, past_due) |

## Billing portal
- `GET /billing/portal` — authenticated paid user → creates a Stripe Billing Portal session, redirects there for the user to manage payment method, cancel, or view invoices, without you building that UI yourself.

## Security
- Verify Stripe webhook signatures using the webhook signing secret (`STRIPE_WEBHOOK_SECRET`) — reject unsigned/invalid requests.
- Never trust client-side "I paid" signals — only the webhook updates role/status.
- Idempotency: dedupe on `stripe_event_id` (store in `subscriptions_log` with a unique index) so retried webhooks don't double-process.

## Config needed
- Stripe secret key, publishable key, webhook secret, and a Price ID for the subscription product — store in environment variables, never in code/Mongo.

## Edge cases
- User cancels via Stripe portal but keeps access until period end — Stripe's `cancel_at_period_end` should be respected; don't downgrade `role` until `customer.subscription.deleted` actually fires.
- Failed payment → grace period before downgrade (recommend: keep `role=paid` through `past_due`, only downgrade on actual subscription deletion/cancellation — matches Stripe's default dunning behavior).
