#!/usr/bin/env python3
"""Drain the MuseFM podcast catalog queue into the townsquare site.

Reads finished-episode entries from the podcast job's catalog queue
(~/workspace/musefm/catalog-queue.jsonl), copies each episode's final
stung MP3 into static/audio/, and appends a matching seed entry to the
EPISODES list in db.py. ensure_musefm_seeds() upserts on slug at startup,
so re-running is safe: already-seeded slugs are skipped.

The podcast jobs write queue lines with: slug, title, description, series,
published ("<YYYY-MM-DD HH:MM America/Chicago>"), duration_sec, local_mp3,
rss_audio_url.

Usage:
    scripts/drain_catalog_queue.py [--since YYYY-MM-DD] [--include-slug SLUG ...]

Options:
    --since YYYY-MM-DD   only drain episodes published on/after this date
    --include-slug SLUG  drain one extra manifest slug not present in the
                         queue (repeatable). The script reads the slug's
                         path/duration/title from the podcast manifest and
                         expects a local MP3 unless --audio PATH is given.

Notes:
    - Only episodes whose final audio exists on disk are drained.
    - Slugs already present in the EPISODES seed list are skipped.
    - The site plays the STUNG version: episodes without intro/outro stings
      are refused (verify with the sting-correlation check before forcing).
    - This stages files in the working tree only. Committing and deploying
      to production is a separate, Anthony-approved step.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.expanduser("~/workspace")
QUEUE_PATH = os.path.join(WORKSPACE, "musefm", "catalog-queue.jsonl")
DB_PATH = os.path.join(HERE, "db.py")
AUDIO_DIR = os.path.join(HERE, "static", "audio")


def load_seeded_slugs():
    src = open(DB_PATH).read()
    return set(re.findall(r'"slug":\s*"([^"]+)"', src))


def parse_published(raw):
    # "<2026-09-30 19:00 America/Chicago>" or "2026-09-30 19:00 America/Chicago"
    # -> "2026-09-30 19:00" (the site's published convention)
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", raw or "")
    return m.group(1) if m else (raw or "")


def manifest_episode(slug):
    out = subprocess.run(["podcast-helper", "manifest", "read"],
                         capture_output=True, text=True)
    man = json.loads(out.stdout)
    for ep in man["episodes"]:
        if ep["slug"] == slug:
            return ep
    return None


def load_queue_entries():
    entries = []
    with open(QUEUE_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("slug") and d.get("local_mp3"):
                entries.append(d)
    return entries


def entry_to_seed(d, date_str):
    slug = d["slug"]
    title = d.get("title") or slug
    if not re.search(r"\d{4}-\d{2}-\d{2}$", title):
        title = f"{title} - {date_str}"
    series = d.get("series") or "Specials"
    desc = (d.get("description") or "").replace('"', "'").strip()
    return {
        "slug": slug,
        "title": title,
        "series": series,
        "description": desc,
        "audio_file": f"{slug}.mp3",
        "duration_sec": int(d.get("duration_sec") or 0),
        "published": parse_published(d.get("published")),
    }


def render_seed_block(seed, note):
    desc = seed["description"]
    # wrap description at ~76 chars like the existing seeds
    words, lines, cur = desc.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 72:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    desc_lines = "\n".join(f'                        "{ln} "' for ln in lines)
    return (
        "    {\n"
        f"        # {note}\n"
        f'        "slug": "{seed["slug"]}",\n'
        f'        "title": "{seed["title"]}",\n'
        f'        "series": "{seed["series"]}",\n'
        f'        "description": ({desc_lines}),\n'
        f'        "audio_file": "{seed["audio_file"]}",\n'
        f'        "duration_sec": {seed["duration_sec"]},\n'
        f'        "published": "{seed["published"]}",\n'
        "    },\n"
    )


def find_episodes_insert_line():
    """Return the 1-based line number of the closing `]` of the EPISODES
    list in db.py (the line itself). Uses the AST so we never mistake a
    later list's bracket for it."""
    import ast
    src = open(DB_PATH).read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "EPISODES":
                    return node.value.end_lineno
    raise RuntimeError("EPISODES list not found in db.py")


def append_seeds(blocks):
    lines = open(DB_PATH).readlines()
    close_idx = find_episodes_insert_line() - 1  # 0-based index of `]`
    if lines[close_idx].strip() != "]":
        raise RuntimeError("EPISODES closing bracket moved unexpectedly")
    insertion = "".join(blocks)
    lines.insert(close_idx, insertion)
    open(DB_PATH, "w").write("".join(lines))


def audio_hashes():
    """sha256 of every MP3 already in static/audio/ (dup guard: the site
    team sometimes staged episodes under shorter slugs)."""
    import hashlib
    h = {}
    for fn in os.listdir(AUDIO_DIR):
        if fn.endswith(".mp3"):
            p = os.path.join(AUDIO_DIR, fn)
            d = hashlib.sha256(open(p, "rb").read()).hexdigest()
            h[d] = fn
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="only drain episodes published on/after YYYY-MM-DD")
    ap.add_argument("--include-slug", action="append", default=[],
                    help="extra manifest slug to drain (repeatable)")
    ap.add_argument("--series-for-include", default="Specials")
    args = ap.parse_args()

    since = dt.date.fromisoformat(args.since) if args.since else None
    seeded = load_seeded_slugs()
    have_audio = audio_hashes()
    os.makedirs(AUDIO_DIR, exist_ok=True)

    today = dt.date.today().isoformat()
    note = (f"Drained {today} from catalog-queue.jsonl "
            "(Anthony 2026-09-30: direct order, push the backlog live).")

    staged, skipped, blocks = [], [], []
    queue_entries = load_queue_entries()

    # explicit manifest includes (episodes missing from the queue)
    for slug in args.include_slug:
        ep = manifest_episode(slug)
        if not ep:
            print(f"SKIP {slug}: not in podcast manifest", flush=True)
            continue
        # created_at is ISO UTC ("2026-09-30T14:10:30+00:00"); convert to
        # America/Chicago air time "YYYY-MM-DD HH:MM".
        pub = ""
        created = ep.get("created_at") or ""
        try:
            dti = dt.datetime.fromisoformat(created)
            if dti.tzinfo is None:
                dti = dti.replace(tzinfo=dt.timezone.utc)
            chi = dti.astimezone(dt.timezone(dt.timedelta(hours=-5),
                                             "America/Chicago"))
            pub = chi.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
        mp3_path = ep.get("path")
        dur = ep.get("duration_secs") or 0
        if mp3_path and os.path.exists(mp3_path):
            try:
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "format=duration", "-of", "csv=p=0", mp3_path],
                    capture_output=True, text=True)
                dur = int(float(out.stdout.strip()))
            except (ValueError, subprocess.SubprocessError):
                pass
        queue_entries.append({
            "slug": slug,
            "title": ep.get("title") or slug,
            "description": ep.get("description") or "",
            "series": args.series_for_include,
            "published": f"<{pub} America/Chicago>" if pub else "",
            "duration_sec": dur,
            "local_mp3": mp3_path,
        })

    for d in queue_entries:
        slug = d["slug"]
        if slug in seeded:
            skipped.append((slug, "already seeded"))
            continue
        pub = parse_published(d.get("published"))
        pub_date = pub[:10]
        if since and pub_date < args.since:
            skipped.append((slug, "before --since"))
            continue
        src_mp3 = d.get("local_mp3")
        if not src_mp3 or not os.path.exists(src_mp3):
            skipped.append((slug, "audio missing"))
            continue
        import hashlib
        digest = hashlib.sha256(open(src_mp3, "rb").read()).hexdigest()
        if digest in have_audio:
            skipped.append((slug, f"duplicate of {have_audio[digest]}"))
            continue
        seed = entry_to_seed(d, pub_date)
        dest = os.path.join(AUDIO_DIR, seed["audio_file"])
        shutil.copy2(src_mp3, dest)
        have_audio[digest] = seed["audio_file"]
        blocks.append(render_seed_block(seed, note))
        staged.append((slug, seed["title"], os.path.getsize(dest) // 1024))

    if blocks:
        append_seeds(blocks)

    print(f"\nStaged {len(staged)} episode(s) into {AUDIO_DIR} + db.py seeds:")
    for slug, title, kb in staged:
        print(f"  + {slug} ({kb} KB) :: {title}")
    if skipped:
        print(f"\nSkipped {len(skipped)}:")
        for slug, why in skipped:
            print(f"  - {slug}: {why}")
    print("\nNext: run episode tests, commit, and deploy with Anthony's approval.")


if __name__ == "__main__":
    main()
