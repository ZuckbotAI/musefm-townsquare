#!/usr/bin/env python3
"""GIF support for Forum posts."""
import io
import os
import re
import time
from urllib.parse import urlparse

try:
    from PIL import Image as _PILImage
except Exception:
    _PILImage = None


def probe_gif_dims(raw):
    """Intrinsic (width, height) of GIF bytes, or (None, None).

    Never raises: probe failures leave dims NULL and the upload proceeds
    as before.
    """
    if not raw or _PILImage is None:
        return None, None
    try:
        with _PILImage.open(io.BytesIO(raw)) as im:
            w, h = im.size
        if w and h and w > 0 and h > 0:
            return int(w), int(h)
    except Exception:
        pass
    return None, None
MAX_GIF_BYTES = 8 * 1024 * 1024
MAX_GIF_URL_LEN = 500

def _host(*labels):
    return ".".join(labels)

# Direct media hosts of the two big GIF CDNs. Share/page URLs are not
# embeddable and are rejected by valid_gif_url below.
GIF_HOSTS = {
    _host("giphy", "com"),
    _host("www", "giphy", "com"),
    _host("media", "giphy", "com"),
    _host("i", "giphy", "com"),
    _host("tenor", "com"),
    _host("www", "tenor", "com"),
    _host("media", "tenor", "com"),
    _host("c", "tenor", "com"),
}

GIF_SCHEMA = """
CREATE TABLE IF NOT EXISTS gif_uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT,
  handle TEXT NOT NULL,
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gif_uploads_fm ON gif_uploads(fm_id, created_at DESC);
"""

def ensure_gif_schema(db):
    """Additive only: new table + new posts.gif_url column. Never alters data."""
    db.db.executescript(GIF_SCHEMA)
    cols = [r["name"] for r in db.db.execute("PRAGMA table_info(posts)")]
    if "gif_url" not in cols:
        db.db.execute("ALTER TABLE posts ADD COLUMN gif_url TEXT NOT NULL DEFAULT ''")
        db.db.commit()
    # 2026-10-04 upvote-stability: intrinsic dimensions for layout-stable
    # <img> rendering. Nullable: legacy rows and probe failures simply
    # render un-dimensioned.
    gcols = [r["name"] for r in db.db.execute("PRAGMA table_info(gif_uploads)")]
    if "width" not in gcols:
        db.db.execute("ALTER TABLE gif_uploads ADD COLUMN width INTEGER")
        db.db.commit()
    if "height" not in gcols:
        db.db.execute("ALTER TABLE gif_uploads ADD COLUMN height INTEGER")
        db.db.commit()


def valid_gif_url(url):
    """Return a normalized embeddable GIF URL, or raise ValueError.

    Rules: https only, host on the GIF_HOSTS whitelist, no userinfo/port,
    path ending in .gif, sane length. Everything else is rejected so a
    malicious or mistyped URL can never become an <img> on the town.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("/"):
        # same-origin upload served by this app -- only /gif/<uid> exists
        if re.fullmatch(r"/gif/\d+", url):
            return url
        raise ValueError("bad gif url")
    if len(url) > MAX_GIF_URL_LEN:
        raise ValueError("gif url too long")
    if re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("bad gif url")
    try:
        p = urlparse(url)
    except Exception:
        raise ValueError("bad gif url")
    if p.scheme != "https":
        raise ValueError("gif url must be https")
    if p.username or p.password or p.port:
        raise ValueError("bad gif url")
    host = (p.hostname or "").lower()
    if host not in GIF_HOSTS:
        raise ValueError("gif host not allowed -- use a Giphy or Tenor media link")
    if not p.path.lower().endswith(".gif"):
        raise ValueError("gif url must point at a .gif file")
    clean = "https://" + host + p.path
    if p.query:
        clean += "?" + p.query
    return clean


def is_gif_bytes(raw):
    """True only for real GIF files (magic bytes), never trust extensions."""
    return isinstance(raw, (bytes, bytearray)) and bytes(raw[:6]) in (b"GIF87a", b"GIF89a")


# Upload blocklist (2026-09-23, Anthony): handles whose uploads are rejected
# at every entry point. Keep in sync across videos.py, ai_images.py, gifs.py,
# db.py. Reversible — remove the handle to restore uploads.
UPLOAD_BLOCKED_HANDLES = frozenset({"miravale", "cassdrift"})


def check_upload_allowed(handle):
    """Raise ValueError when this handle is blocked from uploading."""
    if (handle or "").strip().lower() in UPLOAD_BLOCKED_HANDLES:
        raise ValueError("uploads are disabled for u/%s — contact support "
                         "if this is a mistake" % (handle or "").strip())


def create_gif_upload(db, fm_id, handle, filename, raw, upload_dir):
    """Validate and store an uploaded GIF. Returns (uid, stored_path)."""
    check_upload_allowed(handle)
    if not raw:
        raise ValueError("empty file")
    if len(raw) > MAX_GIF_BYTES:
        raise ValueError("gif too big (max 8 MB)")
    if not is_gif_bytes(raw):
        raise ValueError("not a gif -- magic bytes do not match")
    safe_name = (os.path.basename(filename or "upload.gif") or "upload.gif")[:120]
    # 2026-10-04 upvote-stability: probe intrinsic dimensions for the
    # layout-stable <img> box. Non-fatal; NULL dims render as before.
    gif_w, gif_h = probe_gif_dims(raw)
    cur = db._exec(
        "INSERT INTO gif_uploads (fm_id, handle, filename, stored_path, bytes,"
        " created_at, width, height)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (fm_id, handle, safe_name, "", len(raw), int(time.time()),
         gif_w, gif_h))
    uid = cur.lastrowid
    stored = "uploads/gif-%d.gif" % uid
    os.makedirs(upload_dir, exist_ok=True)
    full = os.path.join(upload_dir, "gif-%d.gif" % uid)
    with open(full, "wb") as fh:
        fh.write(raw)
    db._exec("UPDATE gif_uploads SET stored_path=? WHERE id=?", (stored, uid))
    return uid, stored


def get_gif_upload(db, uid):
    ensure_gif_schema(db)
    r = db._one("SELECT * FROM gif_uploads WHERE id=?", (uid,))
    return dict(r) if r else None



def backfill_dimensions(db, upload_dir, limit=200):
    """Fill NULL width/height on legacy GIF rows from the stored files.

    2026-10-04 upvote-stability: rows uploaded before dimension probing
    existed get their dims lazily so old feed media stops shifting too.
    Bounded (default 200 rows per call); rows whose files are missing or
    unreadable are left NULL. Returns the number of rows updated.
    """
    ensure_gif_schema(db)
    rows = db.db.execute(
        "SELECT id, stored_path FROM gif_uploads"
        " WHERE width IS NULL OR height IS NULL"
        " ORDER BY id DESC LIMIT ?", (max(1, int(limit or 200)),)).fetchall()
    updated = 0
    for r in rows:
        rid = r["id"]
        stored = r["stored_path"] or ""
        if not stored or ".." in stored:
            continue
        full = os.path.join(upload_dir, os.path.basename(stored))
        try:
            with open(full, "rb") as f:
                w, h = probe_gif_dims(f.read(MAX_GIF_BYTES + 1))
        except OSError:
            continue
        if w and h:
            db._exec("UPDATE gif_uploads SET width=?, height=? WHERE id=?",
                     (w, h, rid))
            updated += 1
    return updated
