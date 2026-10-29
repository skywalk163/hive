"""P2a：SQLite store 的 CAS、幂等、复制回放。"""
from __future__ import annotations

import pytest

from hive_store import Store, StoreError


def _card(tid="T-0001", state="ready", **kw):
    data = {"id": tid, "title": "t", "state": state, "owner": "", "repo": "light",
            "lease_minutes": 10, "updated": "2026-10-29T00:00:00+08:00"}
    data.update(kw)
    return data


def test_claim_cas_and_idempotent(tmp_path):
    s = Store(tmp_path / "a.db", "node-a")
    s.ingest_cards({"T-0001": _card()})

    out = s.claim("T-0001", "agent-a", term=3, lease_minutes=10)
    assert out["ok"] and out["claim"]["owner"] == "agent-a"
    assert out["claim"]["lease_seq"] == 1
    assert s.task("T-0001")["state"] == "claimed"
    assert s.task("T-0001")["title"] == "t"
    assert s.task("T-0001")["repo"] == "light"
    assert s.last_seq() == 1

    again = s.claim("T-0001", "agent-a", term=3, lease_minutes=10)
    assert again["idempotent"] is True
    assert again["claim"]["lease_id"] == out["claim"]["lease_id"]
    assert s.last_seq() == 1

    with pytest.raises(StoreError):
        s.claim("T-0001", "agent-b", term=3, lease_minutes=10)


def test_renew_result_release(tmp_path):
    s = Store(tmp_path / "b.db", "node-a")
    s.ingest_cards({"T-0001": _card()})
    c = s.claim("T-0001", "agent-a", term=2, lease_minutes=10)
    lease = c["claim"]["lease_id"]

    r = s.renew("T-0001", "agent-a", lease, term=2, minutes=5)
    assert r["claim"]["lease_until"] > c["claim"]["lease_until"]

    res = s.result("T-0001", "agent-a", lease, term=2,
                   payload={"status": "done", "summary": "ok"})
    assert s.result_info("T-0001")["status"] == "done"
    assert s.task("T-0001")["state"] == "submitted"
    assert res["seq"] == s.last_seq()

    s.close("T-0001", done=True, note="land")
    assert s.task("T-0001")["state"] == "done"
    assert s.claim_info("T-0001") is None


def test_release_requeues(tmp_path):
    s = Store(tmp_path / "c.db", "node-a")
    s.ingest_cards({"T-0001": _card()})
    c = s.claim("T-0001", "agent-a", term=1, lease_minutes=10)
    s.release("T-0001", "agent-a", c["claim"]["lease_id"], term=1, requeue=True)
    assert s.task("T-0001")["state"] == "ready"
    assert s.claim_info("T-0001") is None


def test_ops_replicate_to_second_store(tmp_path):
    a = Store(tmp_path / "leader.db", "node-a")
    b = Store(tmp_path / "follower.db", "node-b")
    cards = {"T-0001": _card()}
    a.ingest_cards(cards)
    c = a.claim("T-0001", "agent-a", term=4, lease_minutes=10)

    for op in a.ops_since(0):
        b.apply_op(op)
    assert b.last_seq() == a.last_seq()
    cb = b.claim_info("T-0001")
    assert cb and cb["owner"] == "agent-a"
    assert cb["lease_id"] == c["claim"]["lease_id"]
    assert b.task("T-0001")["state"] == "claimed"
