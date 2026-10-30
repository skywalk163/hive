"""store 覆盖 Git 卡面：只有「有效租约」才允许覆盖。

回归 2026-10-30（现场实例）：0.88 的看板把 5 张早已 done 的卡显示成
`ready / assigned / submitted`（T-0001、T-0201~T-0204），两台机器同一个
commit、同一份卡、同一份 results，面板却不一致。

根因：`_overlay_store_state` 无条件用 store 的运行时 state 覆盖卡面，而这些
卡的 `close / land` 是在**另一台机器**（`claim_backend=git`）的 Git 侧完成的，
store 永远不知道 → 陈旧 state 每次把终态翻回去。

规则：租约是唯一可信的运行时硬证据。有未过期租约 = 有 worker 正持有这张卡，
store 更新鲜；没有租约 = Git 卡面是权威。
"""
from __future__ import annotations

import sqlite3
import subprocess

import pytest

from hive import Hive, parse_card, store_lease_active, write_card
from hive_store import Store


def _mark_done(hive_dir, tid: str) -> None:
    """模拟「另一台机器在 Git 侧 close 了这张卡」。"""
    p = hive_dir / "tasks" / f"{tid}.md"
    data, body = parse_card(p)
    data["state"] = "done"
    write_card(p, data, body)


def _push_done(world, tid: str) -> None:
    """把 done 落到 origin/main：ReadCache 会 reset 到 origin，未提交的改动会被冲掉。"""
    _mark_done(world.hive1, tid)
    for cmd in (["add", "-A"], ["commit", "-m", f"close {tid}"],
                ["push", "origin", "main"]):
        subprocess.run(["git", "-C", str(world.hive1), *cmd], check=True,
                       env=world.git_env, capture_output=True)


def _expire_lease(db_path, tid: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE claims SET lease_until=1.0 WHERE task_id=?", (tid,))
        conn.commit()
    finally:
        conn.close()


def _use_store(monkeypatch, db_path) -> None:
    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "store")
    monkeypatch.setenv("HIVE_STORE_PATH", str(db_path))


def test_stale_store_does_not_flip_done_card(world, tmp_path, monkeypatch):
    """卡面 done + store 仍 assigned（无租约）→ 必须以卡面为准。"""
    world.add_task("T-0001", repo="light")
    db = tmp_path / "stale.db"
    s = Store(db, "n")
    s.ingest_cards(Hive(str(world.hive1)).cards())
    s.assign("T-0001", "agent-x", term=1)      # store 认为它还在 assigned

    _mark_done(world.hive1, "T-0001")          # 另一台机器在 Git 侧 close

    _use_store(monkeypatch, db)
    cards = Hive(str(world.hive1)).cards()
    assert cards["T-0001"]["state"] == "done", "陈旧 store 不得把终态翻回去"


def test_stale_store_does_not_flip_done_card_in_single_card_read(
        world, tmp_path, monkeypatch):
    """`card()` 单卡读取走同一条规则（close/land 都用它）。"""
    world.add_task("T-0002", repo="light")
    db = tmp_path / "stale2.db"
    s = Store(db, "n")
    s.ingest_cards(Hive(str(world.hive1)).cards())
    s.assign("T-0002", "agent-y", term=1)
    _mark_done(world.hive1, "T-0002")

    _use_store(monkeypatch, db)
    assert Hive(str(world.hive1)).card("T-0002")["state"] == "done"


def test_active_lease_still_overlays(world, tmp_path, monkeypatch):
    """有未过期租约时 store 仍然覆盖（原语义不能丢）。"""
    world.add_task("T-0003", repo="light")
    db = tmp_path / "active.db"
    s = Store(db, "n")
    s.ingest_cards(Hive(str(world.hive1)).cards())
    s.claim("T-0003", "agent-a", term=1, lease_minutes=10)

    _use_store(monkeypatch, db)
    card = Hive(str(world.hive1)).cards()["T-0003"]
    assert card["state"] == "running"
    assert card["owner"] == "agent-a"


def test_expired_lease_does_not_overlay(world, tmp_path, monkeypatch):
    """租约过期（worker 已死）→ 退回 Git 卡面，交给 watchdog 处理。"""
    world.add_task("T-0004", repo="light")
    db = tmp_path / "expired.db"
    s = Store(db, "n")
    s.ingest_cards(Hive(str(world.hive1)).cards())
    s.claim("T-0004", "agent-b", term=1, lease_minutes=10)
    _expire_lease(db, "T-0004")

    _use_store(monkeypatch, db)
    card = Hive(str(world.hive1)).cards()["T-0004"]
    assert card["state"] == "ready", "过期租约不该继续把卡按在 running"
    assert not card.get("owner")


@pytest.mark.parametrize("claim", [None, {}, {"lease_until": ""},
                                   {"lease_until": "not-a-number"},
                                   {"lease_until": 1.0}])
def test_store_lease_active_rejects_garbage(claim):
    """认不出的租约一律当无效——宁可退回卡面，也不拿脏数据覆盖。"""
    assert store_lease_active(claim) is False


def test_read_cache_uses_same_rule(world, tmp_path, monkeypatch):
    """hived 只读缓存必须与 cards() 同规则（原先是第二份重复实现）。"""
    from mcp_cache import ReadCache

    world.add_task("T-0005", repo="light")
    db = tmp_path / "cache.db"
    s = Store(db, "n")
    s.ingest_cards(Hive(str(world.hive1)).cards())
    s.assign("T-0005", "agent-z", term=1)
    _push_done(world, "T-0005")

    _use_store(monkeypatch, db)
    cache = ReadCache(world.hive1)
    cache.refresh()
    assert cache.cards()["T-0005"]["state"] == "done"
