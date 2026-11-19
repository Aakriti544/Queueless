"""Send transactional email through Brevo's HTTPS API."""

from html import escape

import requests
from django.conf import settings


BREVO_EMAIL_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
BREVO_TIMEOUT = (5, 15)


class BrevoEmailError(Exception):
    """A safe delivery error that never includes credentials or response data."""


def send_brevo_email(*, recipient, subject, text_content, html_content=None):
    """Send one transactional email and raise a sanitized error on failure."""
    api_key = getattr(settings, "BREVO_API", "")
    sender_email = getattr(settings, "BREVO_SENDER_EMAIL", "")
    sender_name = getattr(settings, "BREVO_SENDER_NAME", "QueueLess")
    if not api_key or not sender_email:
        raise BrevoEmailError("Email delivery is not configured.")

    if html_content is None:
        html_content = "<p>" + escape(text_content).replace("\n", "<br>\n") + "</p>"

    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "to": [{"email": recipient}],
        "subject": subject,
        "textContent": text_content,
        "htmlContent": html_content,
    }
    try:
        response = requests.post(
            BREVO_EMAIL_ENDPOINT,
            headers={"api-key": api_key, "accept": "application/json"},
            json=payload,
            timeout=BREVO_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise BrevoEmailError("Email delivery is temporarily unavailable.") from None

    if not 200 <= response.status_code < 300:
        raise BrevoEmailError(
            f"Email delivery failed (Brevo HTTP {response.status_code})."
        )
