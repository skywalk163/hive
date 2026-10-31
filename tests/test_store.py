"""P2a：SQLite store 的 CAS、幂等、复制回放。"""
from __future__ import annotations

import json
import sqlite3

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
    assert s.task("T-0001")["state"] == "running"
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
    assert b.task("T-0001")["state"] == "running"


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
    assert cache.cards()["T-0001"]["state"] == "running"
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
                           store=s, role="leader", term=1)
    fake.handle_store = lambda action, body: (
        200, s.mark_archived(body["task"], body.get("sha", ""), term=1))
    n = ArchiveAgent(fake).tick()
    assert n == 1
    assert world.refs("refs/heads/res/T-0001")
    res = s.result_info("T-0001")
    assert res["archived_at"]


def test_compact_snapshot_import(tmp_path):
    a = Store(tmp_path / "snap-a.db", "a")
    a.ingest_cards({"T-0001": _card()})
    a.claim("T-0001", "A", term=1, lease_minutes=10)
    snap = a.snapshot()
    assert snap["commit_seq"] == 1
    assert a.compact(keep=0) == 1
    assert a.ops_since(0) == []

    b = Store(tmp_path / "snap-b.db", "b")
    assert b.import_snapshot(snap) == 1
    assert b.commit_seq() == 1
    assert b.claim_info("T-0001")["owner"] == "A"
    assert b.task("T-0001")["state"] == "running"


def test_snapshot_without_ops_still_imports_tasks(tmp_path):
    a = Store(tmp_path / "snap-zero-a.db", "a")
    a.ingest_cards({"T-0001": _card()})
    snap = a.snapshot()
    assert snap["commit_seq"] == 0
    b = Store(tmp_path / "snap-zero-b.db", "b")
    b.import_snapshot(snap)
    assert b.task("T-0001")["title"] == "t"


def test_retry_clears_result_and_claim(tmp_path):
    s = Store(tmp_path / "retry.db", "n")
    s.ingest_cards({"T-0001": _card()})
    c = s.claim("T-0001", "A", term=1, lease_minutes=10)
    s.result("T-0001", "A", c["claim"]["lease_id"], term=1,
             payload={"status": "failed"})
    s.retry("T-0001", "A", term=1)
    assert s.result_info("T-0001") is None
    assert s.claim_info("T-0001") is None
    assert s.task("T-0001")["state"] == "assigned"
    assert s.task("T-0001")["owner"] == "A"


# --------------------------------------------------------------------------- #
# T-0219：schema v2（task_summary）+ 迁移幂等
# --------------------------------------------------------------------------- #

def _schema_snapshot(path) -> tuple[int, list[tuple[str, str, str]]]:
    """(user_version, [(type, name, sql)]) —— 结构比对的唯一口径。"""
    conn = sqlite3.connect(str(path))
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        rows = [tuple(r) for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")]
    finally:
        conn.close()
    return version, rows


def test_migration_v2_is_idempotent(tmp_path):
    """连跑两次迁移：user_version 与整库结构必须逐字一致（判据 1）。"""
    path = tmp_path / "v2.db"
    first = Store(path, "n")
    assert first.schema_version() == 2
    assert first._meta("schema_version") == "2"     # 兼容旧读者，与 user_version 同步
    first.close_conn()
    v1, dump1 = _schema_snapshot(path)
    assert v1 == 2

    second = Store(path, "n")                        # 第二次打开 = 第二次跑迁移
    assert second.schema_version() == 2
    second.close_conn()
    v2, dump2 = _schema_snapshot(path)

    assert (v1, dump1) == (v2, dump2)
    names = {name for _, name, _ in dump2}
    assert "task_summary" in names
    assert "idx_task_summary_state_finished" in names
    assert "idx_task_summary_repo_state" in names


def test_legacy_v1_db_upgrades_in_place(tmp_path):
    """老库（只有 5 张表、user_version=0）打开后升到 v2，且原有数据不丢。"""
    path = tmp_path / "legacy.db"
    seed = Store(path, "n")
    seed.ingest_cards({"T-0001": _card(title="老卡")})
    seed.close_conn()

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DROP TABLE task_summary")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()
    version, names = _schema_snapshot(path)[0], {r[1] for r in _schema_snapshot(path)[1]}
    assert version == 0 and "task_summary" not in names     # 确认真的退回 v1 形态

    upgraded = Store(path, "n")
    assert upgraded.schema_version() == 2
    assert upgraded.task("T-0001")["title"] == "老卡"        # 既有数据不丢
    # 摘要表首次为空，refresh 从既有 tasks/results 灌一遍
    assert upgraded.refresh_summaries() == 1
    rows = upgraded.summary_rows()
    assert [r["id"] for r in rows] == ["T-0001"]
    assert rows[0]["title"] == "老卡"
    upgraded.close_conn()


def test_summary_follows_task_lifecycle(tmp_path):
    """摘要行跟着 claim/result/close 走：state、finished_at、summary_head 都对得上。"""
    s = Store(tmp_path / "life.db", "n")
    s.ingest_cards({"T-0001": _card(created="2026-10-01T00:00:00+08:00",
                                    priority="P1", title="摘要卡")})
    row = s.summary_rows()[0]
    assert row["state"] == "ready"
    assert row["repo"] == "light" and row["priority"] == "P1"
    assert row["created_at"] == "2026-10-01T00:00:00+08:00"
    assert row["finished_at"] == "" and row["summary_head"] == ""
    assert row["archived"] is False

    c = s.claim("T-0001", "A", term=1, lease_minutes=10)
    assert s.summary_rows()[0]["state"] == "running"

    s.result("T-0001", "A", c["claim"]["lease_id"], term=1,
             payload={"status": "done", "finished_at": "2026-10-31T10:00:00+08:00",
                      "summary": "第一行结论\n第二行细节不该进摘要"})
    row = s.summary_rows()[0]
    assert row["state"] == "submitted"
    assert row["finished_at"] == "2026-10-31T10:00:00+08:00"
    assert row["summary_head"] == "第一行结论"      # 只取首行，不带细节

    s.close("T-0001", done=True, note="land")
    assert s.summary_rows()[0]["state"] == "done"
    s.close_conn()


def test_summary_archived_flag_survives_later_writes(tmp_path):
    """`archived` 是生命周期标记，不能被后续 claim/result 顺手抹掉。"""
    s = Store(tmp_path / "arch.db", "n")
    s.ingest_cards({"T-0001": _card(), "T-0002": _card(tid="T-0002")})
    assert s.set_summary_archived("T-0001", True) is True
    assert s.set_summary_archived("T-9999", True) is False   # 没 ingest 过的卡

    assert [r["id"] for r in s.summary_rows()] == ["T-0002"]           # 默认只热卡
    full = s.summary_rows(include_archived=True)
    assert [r["id"] for r in full] == ["T-0001", "T-0002"]
    assert full[0]["archived"] is True

    # 归档后又跑了一轮 claim/result，标记必须还在
    s.claim("T-0001", "A", term=1, lease_minutes=10)
    s.ingest_cards({"T-0001": _card(state="done")})
    assert s.summaries()["T-0001"]["archived"] is True

    s.set_summary_archived("T-0001", False)
    assert s.summaries()["T-0001"]["archived"] is False
    assert s.summary_rows()[0]["id"] == "T-0001"
    s.close_conn()


def test_summary_survives_snapshot_roundtrip(tmp_path):
    """快照复制到新节点后，冷卡的 archived 标记不能变回热卡。"""
    a = Store(tmp_path / "snap-a.db", "a")
    a.ingest_cards({"T-0001": _card(state="done")})
    a.set_summary_archived("T-0001", True)
    snap = a.snapshot()
    assert snap["summaries"]["T-0001"]["archived"] is True

    b = Store(tmp_path / "snap-b.db", "b")
    b.import_snapshot(snap)
    assert b.summaries()["T-0001"]["archived"] is True
    assert b.summary_rows() == []                    # 冷卡不进默认列表
    assert b.summary()["tasks_archived"] == 1
    a.close_conn()
    b.close_conn()
