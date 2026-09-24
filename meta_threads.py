"""Dormant Meta Threads connector for MuseFM (2026-09-23, Anthony).

Purpose: post to Threads on a user's behalf from musefm.lol, IF Meta ever
approves our developer app. Until then this module is inert — nothing in
app.py imports or calls it, and every function raises MetaNotConfigured
unless the env credentials exist.

Wiring it up later (after Meta app approval):
  1. Create the Meta developer app (business app), enable the Threads API,
     set the OAuth redirect URI to https://musefm.lol/meta/callback.
  2. Set env vars on Render: META_THREADS_APP_ID, META_THREADS_APP_SECRET,
     META_THREADS_REDIRECT_URI.
  3. Add two routes in app.py:
       @app.route("/meta/connect")  -> redirect(meta_threads.authorization_url(state))
       @app.route("/meta/callback") -> token = meta_threads.exchange_code(request.args["code"])
                                       store token + user id on the identity
  4. Post: meta_threads.post_text(user_threads_id, token, "hello from MuseFM")

Endpoints follow Meta's Threads API docs (graph.threads.com). Re-verify
against https://developers.facebook.com/documentation/threads/ at wiring
time — Meta moves these around.
"""

import json
import os
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://www.threads.com/oauth/authorize"
TOKEN_URL = "https://graph.threads.com/oauth/access_token"
GRAPH_BASE = "https://graph.threads.com/v1.0"

SCOPES_PUBLISH = "threads_basic,threads_content_publish"


class MetaNotConfigured(RuntimeError):
    """Raised when the Meta app credentials are not set in the environment."""


def _config():
    app_id = os.environ.get("META_THREADS_APP_ID", "").strip()
    app_secret = os.environ.get("META_THREADS_APP_SECRET", "").strip()
    redirect_uri = os.environ.get(
        "META_THREADS_REDIRECT_URI", "https://musefm.lol/meta/callback"
    ).strip()
    if not app_id or not app_secret:
        raise MetaNotConfigured(
            "META_THREADS_APP_ID / META_THREADS_APP_SECRET are not set. "
            "The Threads connector is dormant until Meta approves the app "
            "and credentials are configured."
        )
    return app_id, app_secret, redirect_uri


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _get_json(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=30) as resp:
        return json.loads(resp.read().decode())


def authorization_url(state, scopes=SCOPES_PUBLISH):
    """Build the Meta OAuth authorize URL to redirect the user to."""
    app_id, _secret, redirect_uri = _config()
    q = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "response_type": "code",
            "state": state,
        }
    )
    return AUTHORIZE_URL + "?" + q


def exchange_code(code):
    """Exchange an OAuth code for a user access token. Returns token dict."""
    app_id, app_secret, redirect_uri = _config()
    return _post_form(
        TOKEN_URL,
        {
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )


def refresh_token(token):
    """Refresh a long-lived token. Returns a fresh token dict."""
    _config()  # validates config presence
    return _get_json(
        TOKEN_URL,
        {"grant_type": "th_refresh_token", "access_token": token},
    )


def get_user_id(token):
    """Return the Threads user id for this token."""
    me = _get_json(GRAPH_BASE + "/me", {"fields": "id,username", "access_token": token})
    return me["id"]


def create_text_container(threads_user_id, token, text):
    """Step 1 of publishing: create a TEXT media container. Returns container id."""
    res = _post_form(
        f"{GRAPH_BASE}/{threads_user_id}/threads",
        {"media_type": "TEXT", "text": text, "access_token": token},
    )
    return res["id"]


def publish_container(threads_user_id, token, container_id):
    """Step 2 of publishing: publish the container. Returns the post id."""
    res = _post_form(
        f"{GRAPH_BASE}/{threads_user_id}/threads_publish",
        {"creation_id": container_id, "access_token": token},
    )
    return res.get("id")


def post_text(threads_user_id, token, text):
    """Publish a text post to Threads. Returns the published post id."""
    container_id = create_text_container(threads_user_id, token, text)
    return publish_container(threads_user_id, token, container_id)
