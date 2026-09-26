"""Redesign pass D2 (2026-09-26, Anthony): Fresh faces welcome wall.

Homepage section showcasing the newest members with their avatars
(uploaded pictures where present, robot fallback), handles, join dates.
Real DB data via db.newest_posting_members(). Warm strip, not a
leaderboard.
"""
import os
import re
import base64
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(HERE, "hidden_files", "test-d2.db")
TEST_DATA = os.path.join(HERE, "hidden_files", "test-d2-data")
CSS = open(os.path.join(HERE, "static", "css", "redesign.css")).read()

_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.203.0.%d" % _ip[0]}


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return enc(priv.private_bytes_raw()), enc(pub.public_bytes_raw())


@pytest.fixture()
def client():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    appmod.AVATAR_DIR = os.path.join(TEST_DATA, "avatars")
    os.makedirs(appmod.AVATAR_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register_muse(client, handle):
    _, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["fm_id"]


def test_fresh_faces_section_renders(client):
    db = appmod.db
    fm1 = register_muse(client, "FreshA")
    fm2 = register_muse(client, "FreshB")
    db.update_identity(fm1, avatar_url="/avatars/%s.png" % fm1)
    db.create_post("lobby", "FreshA", "hello one", "body one",
                   bypass_filter=True)
    db.create_post("lobby", "FreshB", "hello two", "body two",
                   bypass_filter=True)

    r = client.get("/", environ_base=fresh_ip())
    assert r.status_code == 200
    body = r.get_data(as_text=True)

    assert "Fresh faces" in body, "welcome wall heading missing"
    assert 'class="fresh-faces' in body, "fresh-faces section missing"
    assert "u/FreshA" in body and "u/FreshB" in body, \
        "newest member handles missing from showcase"
    assert "joined" in body, "join date line missing"
    assert "/avatars/%s.png" % fm1 in body, \
        "uploaded avatar not used in showcase"
    assert "/agent/FreshA" in body, "showcase card should link to profile"


def test_fresh_faces_css():
    assert ".fresh-faces" in CSS
    assert ".ff-avatar" in CSS
    assert ".ff-card:hover" in CSS
    assert '[data-theme="dark"] .fresh-faces' in CSS, \
        "dark theme must restyle the warm strip"
    # Anthony 2026-09-26: photos must be LARGER than standard avatar size
    # (44px site-wide), so the showcase reads as a wall of fresh selfies.
    m = re.search(r'\.ff-avatar\s*\{([^}]*)\}', CSS)
    assert m, ".ff-avatar rule not found"
    w = re.search(r'width:\s*(\d+)px', m.group(1))
    assert w and int(w.group(1)) > 44, \
        f".ff-avatar must exceed standard 44px avatar size, got {w.group(1) if w else 'none'}"
    # dark-mode readability of the section content
    assert '[data-theme="dark"] .ff-posts' in CSS, \
        "dark theme must restyle the post-count pill"


def test_showcase_uses_real_data_not_fakes():
    tpl = open(os.path.join(HERE, "templates", "index.html")).read()
    assert "newest_members" in tpl, "must iterate real newest_members"
    assert "final_avatar" in tpl, "must use final_avatar (upload or robot)"
    m = re.search(r'<section class="fresh-faces.*?</section>', tpl, re.S)
    assert m, "fresh-faces section not found"
    assert "lorem" not in m.group(0).lower()
    assert "placeholder" not in m.group(0).lower()
