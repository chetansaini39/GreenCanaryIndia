"""Transactional email sending via SendGrid's Web API (Module 01).

Only two flows exist in v1: email verification on registration and password
reset. Both build an absolute link (the caller passes it in, already built
with url_for(..., _external=True)) and send a small HTML email.

If SENDGRID_API_KEY is unset (e.g. local dev), emails are logged instead of
sent so the registration/reset flows never 500 without credentials — the link
shows up in the app log and can be pasted into the browser to test the flow.
"""
import logging

from flask import current_app

logger = logging.getLogger(__name__)


def _send(to_email: str, subject: str, html_content: str, text_content: str) -> bool:
    """Send one email. Returns True on success, False if not sent (missing key
    or SendGrid error) — callers surface a generic message regardless, so a
    delivery failure never blocks the user's flow."""
    api_key = current_app.config.get("SENDGRID_API_KEY")
    from_email = current_app.config.get("EMAIL_FROM")

    if not api_key:
        logger.warning(
            "SENDGRID_API_KEY not configured — email to %s NOT sent (subject: %s).",
            to_email, subject,
        )
        logger.info("Dev email fallback — body:\n%s", text_content)
        return False

    # Imported lazily so the package is only required when actually sending.
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=html_content,
        plain_text_content=text_content,
    )
    try:
        SendGridAPIClient(api_key).send(message)
        logger.info("Sent '%s' email to %s", subject, to_email)
        return True
    except Exception:
        logger.exception("Failed to send '%s' email to %s", subject, to_email)
        return False


def send_verification_email(to_email: str, name: str, verify_url: str) -> bool:
    subject = "Verify your email — GEX Intelligence"
    greeting = f"Hi {name}," if name else "Hi,"
    html = f"""
      <p>{greeting}</p>
      <p>Thanks for signing up for GEX Intelligence. Please confirm your email
         address by clicking the link below:</p>
      <p><a href="{verify_url}">Verify my email</a></p>
      <p>You can keep using your account in the meantime — this just removes the
         reminder banner. If you didn't create this account, you can ignore this
         email.</p>
    """
    text = (
        f"{greeting}\n\n"
        "Thanks for signing up for GEX Intelligence. Confirm your email address:\n"
        f"{verify_url}\n\n"
        "If you didn't create this account, you can ignore this email."
    )
    return _send(to_email, subject, html, text)


_CONTACT_TYPE_LABELS = {
    "bug": "Bug report",
    "feature": "Feature idea",
    "data": "Data question",
    "general": "General",
}


def send_contact_notification_email(to_email: str, submission: dict) -> bool:
    """Notify the team that a contact/feedback form was submitted (Module 01).

    `to_email` is the ADMIN_CONTACT_EMAIL inbox; `submission` is the stored
    document (or an equivalent dict) with type/name/email/message fields.
    """
    type_label = _CONTACT_TYPE_LABELS.get(submission.get("type"), submission.get("type", ""))
    name = submission.get("name", "")
    from_addr = submission.get("email", "")
    message = submission.get("message", "")
    linked = "yes" if submission.get("user_id") else "no (anonymous)"

    subject = f"[Contact — {type_label}] from {name}"
    html = f"""
      <p>A new contact/feedback submission arrived on GEX Intelligence.</p>
      <ul>
        <li><strong>Type:</strong> {type_label}</li>
        <li><strong>Name:</strong> {name}</li>
        <li><strong>Email:</strong> {from_addr}</li>
        <li><strong>Logged-in user:</strong> {linked}</li>
      </ul>
      <p><strong>Message:</strong></p>
      <p style="white-space:pre-wrap">{message}</p>
    """
    text = (
        "New contact/feedback submission on GEX Intelligence.\n\n"
        f"Type: {type_label}\n"
        f"Name: {name}\n"
        f"Email: {from_addr}\n"
        f"Logged-in user: {linked}\n\n"
        f"Message:\n{message}\n"
    )
    return _send(to_email, subject, html, text)


def send_password_reset_email(to_email: str, name: str, reset_url: str) -> bool:
    subject = "Reset your password — GEX Intelligence"
    greeting = f"Hi {name}," if name else "Hi,"
    html = f"""
      <p>{greeting}</p>
      <p>We received a request to reset your GEX Intelligence password. Click the
         link below to choose a new one. This link expires in 1 hour and can only
         be used once.</p>
      <p><a href="{reset_url}">Reset my password</a></p>
      <p>If you didn't request this, you can safely ignore this email — your
         password won't change.</p>
    """
    text = (
        f"{greeting}\n\n"
        "We received a request to reset your GEX Intelligence password. Use the "
        "link below (expires in 1 hour, single use):\n"
        f"{reset_url}\n\n"
        "If you didn't request this, you can safely ignore this email."
    )
    return _send(to_email, subject, html, text)
