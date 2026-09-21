#!/usr/bin/env python3
"""
Tests for proxy Signal Shop purchases: a linked muse buys shop items for
its human — the HUMAN's spendable Signal is charged, the item lands on
the HUMAN's pet.

Run:  .venv/bin/python test_shop_proxy.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.

Covers:
  - happy path: linked muse buys accessory for human -> human charged,
    human's pet equipped, muse's own Signal untouched, audit row written
  - unlinked muse -> 403, nothing charged
  - wrong for_fm_id (cross-human) -> 403, nothing charged
  - human cannot call buy_proxy (humans use /api/shop/buy) -> 403
  - insufficient human funds -> 402, nothing charged
  - idempotent retry (same idempotency_key) -> no double charge
  - wrong action / bad signature -> 401
  - proxy_history shows the audit trail
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import shop
from db import (Database, ensure_human_auth_schema, ensure_linking_schema,
                now)
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-shop-proxy.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    ensure_linking_schema(appmod.db)
    appmod.AGENT_KEY = "test-agent-key"
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def reg(c, handle):
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": handle, "public_key": pub})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def make_human(c, handle):
    priv, fm_id = reg(c, handle)
    # Humans are identities with a password login.
    appmod.db.set_identity_password(fm_id, "scrypt:test-hash")
    return priv, fm_id


def link(db, human_fm_id, muse_fm_id):
    code, _ = db.create_link_code(human_fm_id)
    return db.consume_link_code(code, muse_fm_id)


def grant_signal(db, fm_id, points):
    db._exec("INSERT INTO rewards (fm_id, handle, points, reason, ref_type,"
             " ref_id, created_at) VALUES (?,?,?,?,?,?,?)",
             (fm_id, fm_id, points, "test", "test",
              f"test-{fm_id}-{points}-{now()}", now()))


def buy_proxy(c, priv, muse_fm_id, item, **extra):
    body = signed_body(priv, "shop_buy_proxy", muse_fm_id, item=item,
                       **extra)
    return c.post("/api/shop/buy_proxy", json=body)


def main():
    c = setup()
    db = appmod.db

    # --- actors ---
    h_priv, h_id = make_human(c, "human_alice")
    m_priv, m_id = reg(c, "muse_proxy")
    link(db, h_id, m_id)

    # Give the HUMAN signal; the muse gets none.
    grant_signal(db, h_id, 500)
    grant_signal(db, m_id, 0)

    before_h = shop.balance(db, h_id)
    before_m = shop.balance(db, m_id)
    check("human has 500 spendable", before_h["spendable"] == 500,
          str(before_h))
    check("muse has 0 spendable", before_m["spendable"] == 0,
          str(before_m))

    # --- happy path: muse buys a sailor hat for its human ---
    r = buy_proxy(c, m_priv, m_id, "acc:sailor_hat",
                  idempotency_key="proxy-test-1")
    d = r.get_json()
    check("proxy buy 200", r.status_code == 200 and d["ok"], str(d)[:200])
    check("charged 25", d["charged"] == 25, str(d)[:200])
    check("proxy echo names the human", d["proxy"]["human_fm_id"] == h_id)
    after_h = shop.balance(db, h_id)
    after_m = shop.balance(db, m_id)
    check("human spendable dropped by 25", after_h["spendable"] == 475,
          str(after_h))
    check("muse spendable untouched", after_m["spendable"] == 0,
          str(after_m))
    check("item owned by the HUMAN", shop.owns(db, h_id, "acc:sailor_hat"))
    check("item NOT owned by the muse",
          not shop.owns(db, m_id, "acc:sailor_hat"))
    check("equipped on the human's pet",
          shop.equipped_accessories(db, h_id) == ["acc:sailor_hat"])
    audit = shop.proxy_buys_for_human(db, h_id)
    check("audit row written", len(audit) == 1 and
          audit[0]["muse_fm_id"] == m_id and
          audit[0]["item"] == "acc:sailor_hat" and
          audit[0]["charged"] == 25, str(audit)[:300])

    # --- idempotent retry: same idempotency_key, no double charge ---
    r2 = buy_proxy(c, m_priv, m_id, "rename_token",
                   idempotency_key="proxy-test-token-1")
    d2 = r2.get_json()
    check("consumable proxy buy 200", r2.status_code == 200 and d2["ok"],
          str(d2)[:200])
    bal_mid = shop.balance(db, h_id)["spendable"]
    r3 = buy_proxy(c, m_priv, m_id, "rename_token",
                   idempotency_key="proxy-test-token-1")
    d3 = r3.get_json()
    check("retry is 200 + already_owned",
          r3.status_code == 200 and d3["ok"] and d3.get("already_owned"),
          str(d3)[:200])
    check("no double charge on retry",
          shop.balance(db, h_id)["spendable"] == bal_mid)

    # --- unlinked muse -> 403 ---
    l_priv, l_id = reg(c, "muse_lonely")
    r4 = buy_proxy(c, l_priv, l_id, "acc:sailor_hat")
    check("unlinked muse 403", r4.status_code == 403,
          str(r4.get_json())[:150])

    # --- cross-human: wrong for_fm_id -> 403, nothing charged ---
    h2_priv, h2_id = make_human(c, "human_bob")
    grant_signal(db, h2_id, 500)
    r5 = buy_proxy(c, m_priv, m_id, "acc:sailor_hat", for_fm_id=h2_id)
    check("cross-human 403", r5.status_code == 403,
          str(r5.get_json())[:150])
    check("bob not charged",
          shop.balance(db, h2_id)["spendable"] == 500)
    check("bob owns nothing",
          not shop.owns(db, h2_id, "acc:sailor_hat"))

    # --- human cannot use the proxy endpoint -> 403 ---
    body = signed_body(h_priv, "shop_buy_proxy", h_id,
                       item="acc:sailor_hat")
    r6 = c.post("/api/shop/buy_proxy", json=body)
    check("human on proxy endpoint 403", r6.status_code == 403,
          str(r6.get_json())[:150])

    # --- insufficient human funds -> 402 ---
    h3_priv, h3_id = make_human(c, "human_broke")
    m3_priv, m3_id = reg(c, "muse_broke_proxy")
    link(db, h3_id, m3_id)
    grant_signal(db, h3_id, 5)
    r7 = buy_proxy(c, m3_priv, m3_id, "acc:star_shades")
    check("insufficient funds 402", r7.status_code == 402,
          str(r7.get_json())[:150])

    # --- wrong action -> 401 ---
    body = signed_body(m_priv, "shop_buy", m_id, item="acc:sailor_hat")
    r8 = c.post("/api/shop/buy_proxy", json=body)
    check("wrong action 401", r8.status_code == 401,
          str(r8.get_json())[:150])

    # --- proxy_history: human sees what their agent bought ---
    hq = signed_body(h_priv, "shop_proxy_history", h_id)
    r9 = c.get("/api/shop/proxy_history",
               query_string={k: str(v) for k, v in hq.items()})
    d9 = r9.get_json()
    check("proxy_history 200", r9.status_code == 200 and d9["ok"],
          str(d9)[:200])
    check("proxy_history lists 2 buys",
          len(d9["proxy_buys"]) == 2, str(d9)[:300])

    # --- humans still buy for themselves via shop_buy ---
    body = signed_body(h_priv, "shop_buy", h_id, item="acc:star_shades")
    r10 = c.post("/api/shop/buy", json=body)
    check("human self-buy still works",
          r10.status_code == 200 and r10.get_json()["charged"] == 30,
          str(r10.get_json())[:150])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
