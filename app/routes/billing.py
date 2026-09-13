import stripe
from flask import (
    Blueprint, redirect, url_for, request, session,
    flash, render_template, current_app, abort,
)

from app.extensions import mongo
from app.models import users as users_model
from app.models import subscriptions_log as subs_log_model
from app.utils.decorators import login_required

billing_bp = Blueprint("billing", __name__)


def _configure_stripe():
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]


# ── /upgrade ───────────────────────────────────────────────────────────────

@billing_bp.route("/upgrade")
@login_required
def upgrade():
    if session.get("role") != "free":
        flash("You already have an active subscription.", "info")
        return redirect(url_for("main.home"))

    _configure_stripe()
    checkout_session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": current_app.config["STRIPE_PRICE_ID"], "quantity": 1}],
        client_reference_id=session["user_id"],
        success_url=url_for("billing.upgrade_success", _external=True)
            + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("billing.upgrade_cancelled", _external=True),
    )
    return redirect(checkout_session.url, code=303)


@billing_bp.route("/upgrade/success")
@login_required
def upgrade_success():
    return render_template("billing/upgrade_success.html")


@billing_bp.route("/upgrade/cancelled")
@login_required
def upgrade_cancelled():
    return render_template("billing/upgrade_cancelled.html")


# ── /webhooks/stripe ───────────────────────────────────────────────────────

@billing_bp.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    webhook_secret = current_app.config["STRIPE_WEBHOOK_SECRET"]

    _configure_stripe()
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        abort(400)

    event_id = event["id"]
    event_type = event["type"]
    event_obj = event["data"]["object"]

    # Dedupe: if this event_id is already in the log, it was already processed.
    if subs_log_model.find_by_stripe_event(mongo.db, event_id):
        return {"status": "already_processed"}, 200

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(event_obj)
    elif event_type == "invoice.payment_failed":
        _handle_invoice_payment_failed(event_obj)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(event_obj)
    elif event_type == "customer.subscription.updated":
        _handle_subscription_updated(event_obj)

    # Log AFTER processing so a processing failure causes Stripe to retry.
    subs_log_model.insert_one(mongo.db, {
        "stripe_event_id": event_id,
        "event_type": event_type,
    })

    return {"status": "ok"}, 200


def _handle_checkout_completed(session_obj):
    user_id = session_obj.get("client_reference_id")
    if not user_id:
        return
    users_model.update_by_id(mongo.db, user_id, {
        "role": "paid",
        "subscription_status": "active",
        "stripe_customer_id": session_obj.get("customer"),
        "stripe_subscription_id": session_obj.get("subscription"),
    })


def _handle_invoice_payment_failed(invoice):
    customer_id = invoice.get("customer")
    user = users_model.find_by_stripe_customer_id(mongo.db, customer_id)
    if not user:
        return
    users_model.update_by_id(mongo.db, user["_id"], {
        "subscription_status": "past_due",
    })


def _handle_subscription_deleted(subscription):
    customer_id = subscription.get("customer")
    user = users_model.find_by_stripe_customer_id(mongo.db, customer_id)
    if not user:
        return
    users_model.update_by_id(mongo.db, user["_id"], {
        "role": "free",
        "subscription_status": "canceled",
        "stripe_subscription_id": None,
    })


def _handle_subscription_updated(subscription):
    customer_id = subscription.get("customer")
    user = users_model.find_by_stripe_customer_id(mongo.db, customer_id)
    if not user:
        return
    users_model.update_by_id(mongo.db, user["_id"], {
        "subscription_status": subscription.get("status", ""),
    })


# ── /billing/portal ────────────────────────────────────────────────────────

@billing_bp.route("/billing/portal")
@login_required
def billing_portal():
    if session.get("role") not in ("paid", "admin", "staff"):
        flash("You don't have an active subscription to manage.", "warning")
        return redirect(url_for("main.home"))

    user = users_model.find_by_id(mongo.db, session["user_id"])
    customer_id = user and user.get("stripe_customer_id")
    if not customer_id:
        flash("No billing account found. Please contact support.", "danger")
        return redirect(url_for("main.home"))

    _configure_stripe()
    portal_session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=url_for("main.home", _external=True),
    )
    return redirect(portal_session.url, code=303)
