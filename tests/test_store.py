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


def test_read_cache_overlays_store_runtime(world, tmp_path, monkeypatch):
    """P2b：hived 只读缓存以 store 的 claim/state 覆盖 Git 卡面。"""
    from mcp_cache import ReadCache
    from hive import Hive

    world.add_task("T-0001", repo="light")
    store_path = tmp_path / "cache-store.db"
    s = Store(store_path, "n")
    s.ingest_cards(Hive(str(world.hive1)).cards())
    s.claim("T-0001", "agent-a", term=1, lease_minutes=10)

    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "store")
    monkeypatch.setenv("HIVE_STORE_PATH", str(store_path))
    cache = ReadCache(world.hive2)
    cache.refresh()
    assert cache.cards()["T-0001"]["state"] == "claimed"
    assert cache.cards()["T-0001"]["owner"] == "agent-a"
    claim = cache.claim_info("T-0001")
    assert claim and claim["agent"] == "agent-a"


def test_stage_commit_truncate(tmp_path):
    s = Store(tmp_path / "two-phase.db", "n")
    s.ingest_cards({"T-0001": _card()})
    out = s.claim("T-0001", "A", term=1, lease_minutes=10, commit=False)
    assert out["seq"] == 1
    assert s.last_seq() == 1 and s.commit_seq() == 0
    assert s.claim_info("T-0001") is None  # staged 不改变状态

    s.commit_op(1)
    assert s.commit_seq() == 1
    assert s.claim_info("T-0001")["owner"] == "A"

    s2 = Store(tmp_path / "two-phase-2.db", "n2")
    s2.ingest_cards({"T-0001": _card()})
    o = s2.claim("T-0001", "B", term=1, lease_minutes=10, commit=False)
    assert s2.commit_seq() == 0
    assert s2.truncate_uncommitted() == 0
    assert s2.last_seq() == 0
    assert s2.claim_info("T-0001") is None


def test_archive_agent_writes_git_and_marks(world, tmp_path):
    from types import SimpleNamespace
    from hive_cluster import ArchiveAgent
    from hive import Hive

    world.add_agent("fb", mode="manual")
    world.add_task("T-0001", repo="light")
    s = Store(tmp_path / "archive.db", "n")
    s.ingest_cards(Hive(str(world.hive1)).cards())
    c = s.claim("T-0001", "fb", term=1, lease_minutes=10)
    s.result("T-0001", "fb", c["claim"]["lease_id"], term=1,
             payload={"task": "T-0001", "status": "done", "agent": "fb",
                      "head": "abc", "summary": "ok"})
    fake = SimpleNamespace(config=SimpleNamespace(hive_dir=world.hive1),
                           store=s, role="leader")
    n = ArchiveAgent(fake).tick()
    assert n == 1
    assert world.refs("refs/heads/res/T-0001")
    res = s.result_info("T-0001")
    assert res["archived_at"]
