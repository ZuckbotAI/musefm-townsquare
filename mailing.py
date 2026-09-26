"""Email alerts mailing list (2026-09-26, Anthony).

The alerts list is strictly opt-in for new signups (double opt-in via the
/newsletter form) and includes existing account emails per Anthony's
2026-09-26 decision, each with a one-click unsubscribe link.

send_alert() is the ONLY way alerts go out. It never sends unless called
explicitly (admin route POST /api/admin/mailing/send). Every alert email
carries a signed per-recipient unsubscribe link in its footer.
"""

import time

import auth_email


def _unsub_url(base_url, secret, email):
    token = auth_email.make_unsubscribe_token(secret, email)
    return base_url.rstrip("/") + "/newsletter/unsubscribe?token=" + token


def send_alert(db, subject, body_text, base_url, secret):
    """Send an alert to every subscribed address. Returns
    {"sent": n, "failed": [(email, reason), ...]}.

    - Only status='subscribed' rows receive anything.
    - Each email carries its own one-click unsubscribe link.
    - A per-recipient failure never stops the rest of the batch.
    """
    results = {"sent": 0, "failed": []}
    for email in db.mailing_subscribed_emails():
        unsub = _unsub_url(base_url, secret, email)
        text = (body_text.rstrip() + "\n\n---\n"
                "You're getting MuseFM alerts because this address is on\n"
                "the list. Unsubscribe anytime with one click:\n" + unsub + "\n")
        html = (
            "<html><body style=\"font-family:sans-serif;color:#1c1917;"
            "max-width:36rem\">"
            "<p>" + _html_escape(body_text).replace("\n", "<br>") + "</p>"
            "<hr><p style=\"color:#78716c;font-size:.85rem\">You're getting "
            "MuseFM alerts because this address is on the list.<br>"
            "<a href=\"" + unsub + "\">Unsubscribe with one click</a></p>"
            "</body></html>")
        ok, reason = auth_email.send_simple_email(
            email, subject, text, html)
        if ok:
            results["sent"] += 1
        else:
            results["failed"].append((email, reason))
    return results


def _html_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
        .replace(">", "&gt;").replace('"', "&quot;")


def confirm_email_html(confirm_url):
    """HTML body for the double opt-in confirmation email."""
    return (
        "<html><body style=\"font-family:sans-serif;color:#1c1917;"
        "max-width:36rem\">"
        "<p>Someone (probably you) asked for MuseFM email alerts.</p>"
        "<p>Click below to confirm. The link expires in 7 days:</p>"
        "<p><a href=\"" + confirm_url + "\" style=\"display:inline-block;"
        "padding:12px 24px;background:#0284c7;color:#fff;text-decoration:none;"
        "border-radius:999px;font-weight:700\">Yes, send me alerts</a></p>"
        "<p style=\"color:#78716c;font-size:.9rem\">Or paste this link into "
        "your browser:<br><a href=\"" + confirm_url + "\">" + confirm_url +
        "</a></p>"
        "<p style=\"color:#78716c;font-size:.9rem\">If you didn't ask for "
        "this, just ignore the email. Nothing will happen.</p>"
        "<p>The MuseFM team<br><a href=\"https://musefm.lol\">musefm.lol</a>"
        "</p></body></html>")


def confirm_email_text(confirm_url):
    return (
        "Someone (probably you) asked for MuseFM email alerts.\n\n"
        "Confirm by clicking the link below (it expires in 7 days):\n\n"
        + confirm_url + "\n\n"
        "If you didn't ask for this, just ignore this email.\n"
        "Nothing will happen.\n\n"
        "The MuseFM team\nhttps://musefm.lol\n")
