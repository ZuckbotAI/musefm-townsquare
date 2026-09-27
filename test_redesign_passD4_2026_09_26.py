"""Redesign pass D4 (2026-09-26, Anthony): custom avatars everywhere.

Every member surface renders the uploaded profile picture through the
final_avatar filter (generated robot avatar as fallback):
- forum post cards (community.html)
- music/podcast cards (audio_upload.html)
- wall notes (wall.html) and homepage wall notes (index.html)
- short cards (_short_card.html)
- DM conversation list + peer header (dm.html, via peer_avatar_url)

db.py supplies avatar_url on list_uploads and bulletin_latest;
list_posts already carries it via _add_tiers. DM threads serialize
peer_avatar_url via _dm_peer_avatar.

Deliberately untouched (verified, no actor data to render):
- notifications (no actor handle stored on notification rows)
- listening room (listener count only, no participant list rendered)
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from cryptography.hazmat.primitives.asymmetric.ed25519 import \
    Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, "static", "css", "redesign.css")).read()


def _pub():
    import base64
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes_raw()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _fresh_db():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="d4db_")
    return appmod.init_db(os.path.join(tmp, "t.db"))


def tpl(name):
    return open(os.path.join(HERE, "templates", name)).read()


def test_list_uploads_carries_avatar_url():
    db = _fresh_db()
    fm = db.register_identity("D4MusA", _pub())["fm_id"]
    db.update_identity(fm, avatar_url="/avatars/x.png")
    db._exec("INSERT INTO uploads (fm_id, handle, title, filename,"
             " stored_path, bytes, mime, attestation, created_at)"
             " VALUES (?,?,?,?,?,?,?,?,?)",
             (fm, "D4MusA", "t", "t.mp3", "uploads/1.mp3", 10,
              "audio/mpeg", "att", 1))
    rows = db.list_uploads()
    assert rows and rows[0]["avatar_url"] == "/avatars/x.png"


def test_bulletin_latest_carries_avatar_url():
    db = _fresh_db()
    fm = db.register_identity("D4MusB", _pub())["fm_id"]
    db.update_identity(fm, avatar_url="/avatars/y.png")
    db.bulletin_post(fm, "D4MusB", "hello wall")
    notes = db.bulletin_latest(5)
    assert notes and notes[0]["avatar_url"] == "/avatars/y.png"


def test_forum_cards_render_avatar_img():
    body = tpl("community.html")
    assert 'class="post-avatar"' in body
    assert "final_avatar(p.handle)" in body, \
        "forum post cards must render the author avatar via final_avatar"


def test_upload_cards_render_avatar_img():
    body = tpl("audio_upload.html")
    assert body.count("final_avatar(u.handle)") >= 2, \
        "music and podcast cards must render uploader avatars"


def test_wall_notes_render_avatar_img():
    assert "final_avatar(n.agent)" in tpl("wall.html"), \
        "wall notes must render the note author's avatar"
    assert "final_avatar(n.agent)" in tpl("index.html"), \
        "homepage wall notes must render the note author's avatar"


def test_short_cards_render_avatar_img():
    body = tpl("_short_card.html")
    assert "short-avatar" in body
    assert "final_avatar(it.handle)" in body, \
        "short cards must render the author avatar via final_avatar"


def test_dm_serializes_peer_avatar():
    src = open(os.path.join(HERE, "app.py")).read()
    assert "_dm_peer_avatar" in src
    assert '"peer_avatar_url"' in src, \
        "DM thread JSON must include peer_avatar_url"
    body = tpl("dm.html")
    assert "peer_avatar_url" in body, \
        "DM client must consume peer_avatar_url"
    assert "dm-avatar-img" in body


def test_d4_avatar_css():
    for cls in [".post-avatar img", ".wall-note-avatar img",
                ".rz-note-avatar", ".short-avatar", ".dm-avatar-img"]:
        assert cls in CSS, f"D4 avatar CSS missing: {cls}"


def test_avatars_theme_independent_dark():
    # Avatar photos must not be hidden or recolored in dark mode.
    for m in re.finditer(r'\[data-theme="dark"\][^{]*\{([^}]*)\}', CSS):
        sel = m.group(0)
        if any(k in sel for k in (".post-avatar img", ".wall-note-avatar",
                                  ".rz-note-avatar", ".short-avatar",
                                  ".dm-avatar-img", ".dm-avatar-wrap")):
            assert "display: none" not in m.group(1)
            assert "visibility: hidden" not in m.group(1)
