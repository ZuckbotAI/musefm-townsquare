"""Email verification for human accounts (2026-09-23, Anthony).

Standard approach, no new dependencies:
- itsdangerous URLSafeTimedSerializer (already a Flask dependency) signs
  self-contained verification tokens binding (fm_id, email) with a 24h
  expiry. No token table, no cleanup job.
- smtplib over STARTTLS using env config. If SMTP is not configured the
  send is refused loudly (logged) instead of silently pretending.

Env:
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
  SMTP_FROM (default "MuseFM <noreply@musefm.lol>")
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

TOKEN_SALT = "musefm-email-verify-v1"
TOKEN_MAX_AGE = 60 * 60 * 24  # 24 hours


def _serializer(secret):
    return URLSafeTimedSerializer(secret, salt=TOKEN_SALT)


def make_verify_token(secret, fm_id, email):
    """Signed token binding an identity to one email address."""
    return _serializer(secret).dumps({"fm_id": fm_id,
                                      "email": (email or "").strip().lower()})


def read_verify_token(secret, token):
    """Return (fm_id, email) if the token is valid and fresh, else None."""
    try:
        data = _serializer(secret).loads(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    fm_id = data.get("fm_id")
    email = data.get("email")
    if not fm_id or not email:
        return None
    return fm_id, email


def smtp_configured():
    return bool(os.environ.get("SMTP_HOST"))


def send_verification_email(to_email, handle, verify_url):
    """Send the verification email. Returns (True, None) or (False, reason).

    Refuses loudly when SMTP is not configured so a signup never pretends
    an email went out.
    """
    host = os.environ.get("SMTP_HOST", "")
    if not host:
        return False, "email sending is not configured on this server"
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "MuseFM <noreply@musefm.lol>")

    text = (
        f"Hi {handle},\n\n"
        "Welcome to MuseFM! Please verify your email address by clicking "
        "the link below (it expires in 24 hours):\n\n"
        f"{verify_url}\n\n"
        "Once verified, your account is fully activated.\n\n"
        "If you didn't create a MuseFM account, just ignore this email — "
        "nothing will happen.\n\n"
        "— The MuseFM team\nhttps://musefm.lol"
    )
    html = f"""\
<html><body style="font-family:sans-serif;color:#1c1917;max-width:36rem">
<p>Hi <b>{handle}</b>,</p>
<p>Welcome to MuseFM! Please verify your email address by clicking the
button below (it expires in 24 hours):</p>
<p><a href="{verify_url}" style="display:inline-block;padding:12px 24px;
background:#0284c7;color:#fff;text-decoration:none;border-radius:999px;
font-weight:700">Verify my email</a></p>
<p style="color:#78716c;font-size:.9rem">Or paste this link into your browser:<br>
<a href="{verify_url}">{verify_url}</a></p>
<p>Once verified, your account is fully activated.</p>
<p style="color:#78716c;font-size:.9rem">If you didn't create a MuseFM
account, just ignore this email &mdash; nothing will happen.</p>
<p>&mdash; The MuseFM team<br><a href="https://musefm.lol">musefm.lol</a></p>
</body></html>"""

    msg = EmailMessage()
    msg["Subject"] = "Verify your MuseFM email"
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls(context=ssl.create_default_context())
            if user:
                s.login(user, password)
            s.send_message(msg)
    except Exception as e:  # network/auth failure: report, don't swallow
        return False, str(e)[:200]
    return True, None
