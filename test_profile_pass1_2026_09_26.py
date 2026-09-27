"""Profile pass 1 (2026-09-26, Anthony): profile picture upload, /avatars/
serving, In the Air section, avatar in the sidebar, redesigned profile page.

Conventions match the rest of the suite: check() prints PASS/FAIL lines,
summary counts, sys.exit(1) on any failure.
"""
import base64
import hashlib
import io
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-profile-pass1.db"
TEST_DATA = "/tmp/test-townsquare-profile-pass1-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return (b64u(priv.private_bytes_raw()),
            b64u(pub.public_bytes_raw()))


def setup():
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


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.202.0.%d" % _ip[0]}


def make_png(n=200):
    return b"\x89PNG\r\n\x1a\n" + bytes(n)


def make_jpeg(n=200):
    return b"\xff\xd8\xff\xe0" + bytes(n)


def login_human(client, handle="PicHuman", password="supersecret1"):
    r = client.post("/signup", data={"handle": handle, "password": password,
                                     "password_confirm": password,
                                     "email": "%s@example.com" % handle.lower()},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    r = client.post("/login", data={"handle": handle, "password": password},
                    environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    return appmod.db.get_identity_by_handle(handle)


def register_muse(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def csrf_of(client):
    r = client.get("/settings", environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    i = body.find(marker)
    assert i != -1, "no csrf token in settings"
    return body[i + len(marker):].split('"')[0]


def post_avatar_human(client, tok, raw, filename="me.png"):
    data = {"csrf_token": tok,
            "avatar": (io.BytesIO(raw), filename, "image/png")}
    return client.post("/settings/avatar", data=data,
                       content_type="multipart/form-data",
                       environ_base=fresh_ip())


def post_avatar_muse(client, priv, fm_id, raw, filename="bot.png"):
    fields = signed_body(priv, "avatar_upload", fm_id)
    data = dict(fields)
    data["avatar"] = (io.BytesIO(raw), filename, "image/png")
    return client.post("/api/identity/avatar", data=data,
                       content_type="multipart/form-data",
                       environ_base=fresh_ip())


def t_human_upload(client):
    print("== human avatar upload ==")
    ident = login_human(client)
    fm_id = ident["fm_id"]
    body = client.get("/settings", environ_base=fresh_ip()).get_data(as_text=True)
    check("settings shows avatar upload form",
          'action="/settings/avatar"' in body, "form missing")
    check("settings previews current avatar",
          "Your current profile picture" in body)
    tok = csrf_of(client)

    r = post_avatar_human(client, tok, make_png(500))
    b = r.get_data(as_text=True)
    check("valid png upload -> 200 + notice",
          r.status_code == 200 and "Profile picture updated" in b,
          f"{r.status_code}")
    ident = appmod.db.get_identity(fm_id)
    want = "/avatars/%s.png" % fm_id
    check("avatar_url stored as site path", ident["avatar_url"] == want,
          ident["avatar_url"])
    check("file on disk",
          os.path.isfile(os.path.join(appmod.AVATAR_DIR, "%s.png" % fm_id)))

    # replace with a jpeg: old png removed, url updated
    r = post_avatar_human(client, csrf_of(client), make_jpeg(500), "me.jpg")
    ident = appmod.db.get_identity(fm_id)
    check("re-upload swaps extension",
          ident["avatar_url"] == "/avatars/%s.jpg" % fm_id,
          ident["avatar_url"])
    check("old png cleaned up",
          not os.path.exists(os.path.join(appmod.AVATAR_DIR,
                                           "%s.png" % fm_id)))

    # invalid + oversized
    before = ident["avatar_url"]
    r = post_avatar_human(client, csrf_of(client), b"not an image" * 50,
                          "evil.txt")
    check("non-image rejected -> 400", r.status_code == 400, r.status_code)
    check("avatar unchanged after reject",
          appmod.db.get_identity(fm_id)["avatar_url"] == before)
    big = b"\x89PNG\r\n\x1a\n" + bytes(appmod.MAX_AVATAR_BYTES + 100)
    r = post_avatar_human(client, csrf_of(client), big, "big.png")
    check("oversize rejected -> 400", r.status_code == 400, r.status_code)

    # bad csrf
    r = post_avatar_human(client, "wrong-token", make_png(100))
    check("bad csrf -> 403", r.status_code == 403, r.status_code)

    # anonymous
    anon = appmod.app.test_client()
    r = anon.post("/settings/avatar", data={"csrf_token": "x"},
                  environ_base=fresh_ip())
    check("anonymous upload redirects to login", r.status_code in (301, 302),
          r.status_code)
    return fm_id


def t_serve(client, fm_id):
    print("== GET /avatars/ ==")
    r = client.get("/avatars/%s.jpg" % fm_id)
    check("serve -> 200 image/jpeg",
          r.status_code == 200 and r.content_type == "image/jpeg",
          f"{r.status_code} {r.content_type}")
    check("cache header set", "max-age=86400" in
          r.headers.get("Cache-Control", ""), r.headers.get("Cache-Control"))
    r = client.get("/avatars/no-such-member.png")
    check("missing file -> 404", r.status_code == 404, r.status_code)
    r = client.get("/avatars/../app.py")
    check("path traversal -> 404", r.status_code == 404, r.status_code)
    r = client.get("/avatars/evil.exe")
    check("bad extension -> 404", r.status_code == 404, r.status_code)


def t_muse_upload(client):
    print("== signed muse avatar upload ==")
    priv, fm_id = register_muse(client, "PicMuse")
    r = post_avatar_muse(client, priv, fm_id, make_png(300))
    j = r.get_json()
    check("valid signed upload -> 200",
          r.status_code == 200 and j.get("avatar_url") == "/avatars/%s.png" % fm_id,
          f"{r.status_code} {j}")
    ident = appmod.db.get_identity(fm_id)
    check("muse avatar_url stored", ident["avatar_url"] == "/avatars/%s.png" % fm_id)

    # tampered signature
    fields = signed_body(priv, "avatar_upload", fm_id)
    fields["fm_id"] = "fm_tampered"
    data = dict(fields)
    data["avatar"] = (io.BytesIO(make_png(100)), "x.png", "image/png")
    r = client.post("/api/identity/avatar", data=data,
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    check("tampered signature -> 401", r.status_code == 401, r.status_code)

    # non-image bytes
    r = post_avatar_muse(client, priv, fm_id, b"GIF89a" + bytes(100), "x.gif")
    check("non-image bytes -> 400", r.status_code == 400, r.status_code)

    # missing file
    fields = signed_body(priv, "avatar_upload", fm_id)
    r = client.post("/api/identity/avatar", data=dict(fields),
                    content_type="multipart/form-data",
                    environ_base=fresh_ip())
    check("missing file -> 400", r.status_code == 400, r.status_code)
    return fm_id


def t_avatar_url_validation():
    print("== db.update_identity avatar_url validation ==")
    db = appmod.db
    priv_b64, pub_b64 = fresh_keypair()
    db.register_identity("ValMuse", pub_b64)
    ident = db.get_identity_by_handle("ValMuse")
    fm_id = ident["fm_id"]
    db.update_identity(fm_id, avatar_url="/avatars/%s.png" % fm_id)
    check("site avatar path accepted",
          db.get_identity(fm_id)["avatar_url"] == "/avatars/%s.png" % fm_id)
    db.update_identity(fm_id, avatar_url="https://example.com/a.png")
    check("https avatar still accepted",
          db.get_identity(fm_id)["avatar_url"] == "https://example.com/a.png")
    try:
        db.update_identity(fm_id, avatar_url="javascript:alert(1)")
        ok = False
    except ValueError:
        ok = True
    check("javascript: url rejected", ok)
    try:
        db.update_identity(fm_id, avatar_url="/avatars/../../etc.png")
        ok = False
    except ValueError:
        ok = True
    check("traversal avatar path rejected", ok)


def t_in_the_air(client):
    print("== In the Air ==")
    priv, fm_id = register_muse(client, "AirMuse")
    db = appmod.db
    check("no presence -> no live rooms", db.live_rooms_for(fm_id) == [])
    # seed a room + presence heartbeat
    db._exec("INSERT INTO rooms (id, episode_slug, title, audio_src,"
             " duration_sec, host_fm_id, created_at)"
             " VALUES (?,?,?,?,?,?,?)",
             ("room-air-1", "ep1", "Nightly Listening", "/audio/x.mp3",
              60, fm_id, time.time()))
    db.heartbeat("room-air-1", fm_id, fm_id, "AirMuse")
    live = db.live_rooms_for(fm_id)
    check("heartbeat shows live room",
          len(live) == 1 and live[0]["title"] == "Nightly Listening", str(live))

    r = client.get("/m/%s" % fm_id, environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("profile 200", r.status_code == 200, r.status_code)
    check("In the Air section renders", 'id="in-the-air"' in body)
    check("live room shown on air", "ON THE AIR" in body and
          "Nightly Listening" in body)

    # quiet member: no rooms, no posts -> tasteful quiet state, never empty
    _p2, fm2 = register_muse(client, "QuietMuse")
    r = client.get("/m/%s" % fm2, environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("quiet profile still has In the Air", 'id="in-the-air"' in body)
    check("quiet state copy present", "Quiet right now" in body)


def t_profile_page(client, human_fm_id, muse_fm_id):
    print("== redesigned profile page ==")
    r = client.get("/m/%s" % human_fm_id, environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("human profile 200", r.status_code == 200, r.status_code)
    check("no Facebook cover header", 'class="profile-cover"' not in body)
    check("avatar-centric identity card", 'class="card p2-id"' in body)
    check("badges on avatar", "p2-avb-tier" in body)
    check("uploaded avatar rendered", "/avatars/%s.jpg" % human_fm_id in body)
    check("signal card", "p2-signalnum" in body)
    check("signal progress bar", 'class="p2-bar"' in body)
    check("about rail card", "Joined" in body)
    check("currently building card", "Currently Building" in body)
    # Style law scoped to this pass's new copy: the identity card and the
    # In the Air rail card carry no em dashes. (base.html meta/footer copy
    # predates this pass and is out of scope.)
    idsec = body.split('class="card p2-id"')[1].split("</section>")[0]
    airsec = body.split('id="in-the-air"')[1].split("</section>")[0]
    check("no em dashes in new profile copy",
          "\u2014" not in idsec and "\u2014" not in airsec)

    r = client.get("/m/%s" % muse_fm_id, environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("muse profile 200", r.status_code == 200, r.status_code)
    check("muse uploaded avatar rendered",
          "/avatars/%s.png" % muse_fm_id in body)

    # overseer badge on the overseer's own avatar
    priv, ov_fm = register_muse(client, appmod.OVERSEER_HANDLE)
    r = client.get("/m/%s" % ov_fm, environ_base=fresh_ip())
    body = r.get_data(as_text=True)
    check("overseer badge overlay on avatar", "p2-avb-overseer" in body)
    check("overseer flair by name", "overseer-badge" in body)


def t_sidebar_avatar(client):
    print("== sidebar avatar ==")
    ident = login_human(client, "SideHuman")
    fm_id = ident["fm_id"]
    body = client.get("/", environ_base=fresh_ip()).get_data(as_text=True)
    check("sidebar account block renders avatar img",
          'class="sb-avatar"' in body)
    check("sidebar avatar uses robot fallback before upload",
          "/avatarbot/" in body)
    tok = csrf_of(client)
    post_avatar_human(client, tok, make_png(200))
    body = client.get("/", environ_base=fresh_ip()).get_data(as_text=True)
    check("sidebar avatar switches to upload",
          "/avatars/%s.png" % fm_id in body)


def main():
    client = setup()
    human_fm = t_human_upload(client)
    t_serve(client, human_fm)
    muse_fm = t_muse_upload(client)
    t_avatar_url_validation()
    t_in_the_air(client)
    t_profile_page(client, human_fm, muse_fm)
    t_sidebar_avatar(client)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
