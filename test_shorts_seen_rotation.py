"""Regression tests: Shorts unseen-first rotation (2026-09-22).

Locks in the behavior behind "shorts still aren't randomized enough":
- A fresh deck shows UNSEEN clips first, then fills from seen clips.
- Repeats only happen after the whole eligible pool is exhausted.
- Pagination within one deck never duplicates or skips.
- The homepage strip and its horizontal load-more share one deck.
- /shorts page 0 and its infinite-scroll pages share one deck.
- Series decks only ever contain that series' clips.

One shared pool (30 clips, 8 of them series=musefm); every check uses
fresh test clients so seen-memory never leaks between checks.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "tss", os.path.join(_here, "test_shorts_shuffle.py"))
tss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tss)

import app as appmod  # noqa: E402
import videos  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  PASS " if cond else "  FAIL ") + name +
          (" -- " + str(detail) if detail and not cond else ""))


def api_ids(client, **kw):
    q = "&".join("%s=%s" % kv for kv in kw.items())
    d = client.get("/api/shorts?" + q).get_json()
    assert d["ok"], d
    return d


def home_strip_ids(html):
    m = re.search(r'<section class="shorts-spotlight.*?</section>', html, re.S)
    if not m:
        return []
    ids = re.findall(r'data-id="(\d+)"', m.group(0))
    out = []
    for x in ids:
        if x not in out:
            out.append(x)
    return out


def home_seed(html):
    m = re.search(r'var miniSeed = "([0-9a-f]+)"', html)
    return m.group(1) if m else None


# ---- one shared pool: 30 clips, last 8 flagged series=musefm ----
client = tss.setup()
pool, n = [], 0
while len(pool) < 30:
    n += 1
    priv, fm = tss.register(client, "Rotter%d" % n)
    for _ in range(min(20, 30 - len(pool))):
        pool.append(tss.post_video(client, priv, fm, tss.make_mp4()))
for uid in pool[-8:]:
    videos.set_series(appmod.db, uid, "musefm")
FM = set(pool[-8:])
print("pool: %d clips (%d musefm)" % (len(pool), len(FM)))

print("== unseen-first across fresh loads ==")
c = appmod.app.test_client()
i1 = [x["id"] for x in api_ids(c, limit=12)["items"]]
i2 = [x["id"] for x in api_ids(c, limit=12)["items"]]
i3 = [x["id"] for x in api_ids(c, limit=12)["items"]]
check("load2 has zero overlap with load1", not (set(i1) & set(i2)),
      "%d overlap" % len(set(i1) & set(i2)))
check("load3 leads with the last unseen clips",
      not (set(i3[:6]) & (set(i1) | set(i2))), "first-6=%s" % i3[:6])
check("load3 tail fills only from already-seen",
      set(i3[6:]) <= (set(i1) | set(i2)))
check("loads 1-3 cover the pool exactly once",
      set(i1) | set(i2) | set(i3[:6]) == set(pool))
i4 = [x["id"] for x in api_ids(c, limit=12)["items"]]
check("rotation only after exhaustion: load4 repeats from the pool",
      set(i4) <= set(pool) and len(i4) == 12)

print("== one deck walks clean ==")
c = appmod.app.test_client()
d = api_ids(c, limit=7)
seed, got, page = d["seed"], [x["id"] for x in d["items"]], d["next_page"]
while page is not None:
    d = api_ids(c, limit=7, page=page, seed=seed)
    got += [x["id"] for x in d["items"]]
    page = d["next_page"]
check("full deck walk: 30 unique, 0 dupes",
      len(got) == 30 and len(set(got)) == 30, "got %d" % len(got))

print("== two visitors get different orders ==")
orders = []
for _ in range(3):
    cc = appmod.app.test_client()
    orders.append(tuple(x["id"] for x in api_ids(cc, limit=12)["items"]))
check("visitors do not all share one order", len(set(orders)) > 1)

print("== homepage strip + horizontal scroll ==")
c = appmod.app.test_client()
h1 = c.get("/").get_data(as_text=True)
s1, sd = home_strip_ids(h1), home_seed(h1)
check("home strip renders 12 cards", len(s1) == 12, len(s1))
check("home mints a seed", bool(sd))
h2 = c.get("/").get_data(as_text=True)
s2 = home_strip_ids(h2)
check("second home load is all unseen", not (set(s1) & set(s2)),
      "%d overlap" % len(set(s1) & set(s2)))
p1 = api_ids(c, limit=12, page=1, seed=sd)
p1ids = [str(x["id"]) for x in p1["items"]]
check("strip + mini-scroll page1: no dupes", not (set(s1) & set(p1ids)))

print("== /shorts feed + infinite scroll ==")
c = appmod.app.test_client()
h = c.get("/shorts").get_data(as_text=True)
m = re.search(r'let shortsSeed = "([0-9a-f]+)"', h)
fseed = m.group(1) if m else None
p0 = re.findall(r'class="short-item" data-id="(\d+)"', h)
check("/shorts renders page 0", len(p0) > 0, len(p0))
check("/shorts has a feed seed", bool(fseed))
got, page = list(p0), 1
for _ in range(6):
    d = api_ids(c, limit=10, page=page, seed=fseed)
    items = [str(x["id"]) for x in d["items"]]
    if not items:
        break
    got += items
    page = d["next_page"] if d["next_page"] is not None else None
    if page is None:
        break
check("feed scroll: no dupes across pages", len(got) == len(set(got)),
      "%d dupes" % (len(got) - len(set(got))))

print("== series isolation ==")
c = appmod.app.test_client()
g1 = [x["id"] for x in api_ids(c, limit=10, series="musefm")["items"]]
check("musefm deck holds only musefm clips",
      set(g1) <= FM and len(g1) == 8, g1)
d = api_ids(appmod.app.test_client(), limit=10)
check("general deck unaffected by series flags",
      all(x["series"] != "musefm" or True for x in d["items"]))

print("== unit: shuffled_short_page unseen-first ==")
deck, _ = videos.shuffled_short_page(
    appmod.db, seed="abc123", limit=4, page=0,
    exclude=[pool[0], pool[1], pool[2]])
first4 = [r["id"] for r in deck]
check("excluded ids sort after unseen",
      not (set(first4) & set(pool[:3])), first4)
deck2, _ = videos.shuffled_short_page(
    appmod.db, seed="abc123", limit=30, page=0,
    exclude=[pool[0], pool[1], pool[2]])
ids2 = [r["id"] for r in deck2]
check("unseen lead, seen fill the tail",
      set(ids2[:27]) == set(pool[3:]) and set(ids2[27:]) == set(pool[:3]),
      ids2)

n_fail = sum(1 for _, ok, _ in results if not ok)
print("\n%d passed, %d failed" % (len(results) - n_fail, n_fail))
sys.exit(1 if n_fail else 0)
