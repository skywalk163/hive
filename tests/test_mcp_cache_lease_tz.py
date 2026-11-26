"""T-0519 回归：store 模式下 ReadCache 的 lease_until 必须是 tz-aware。

2026-11-26 实测：mcp_cache 的 store 分支手写 datetime.fromtimestamp(epoch)
忘了 tz，产出 naive ISO 串，mcp_server._rows() 里 `parse_iso(x) < now()`
（aware）直接 TypeError，hived HTTP 的所有读工具（含 hive_whoami）全挂。
stdio 模式（cache=None）不触发，所以 CLI 一直没暴露。

本测试用 stub store 喂一个 epoch 租约，断言缓存产出的 lease_until
能和 now() 安全比较。
"""
from __future__ import annotations

import json

import pytest

import mcp_cache
from hive import parse_iso, store_lease_active, now
from mcp_cache import ReadCache


class _StubStore:
    """最小 store 桩：task() + claim_info()，租约给 epoch 秒（ REAL）。"""

    FUTURE_EPOCH = 4102444800.0  # 2100-01-01，足够「未过期」

    def task(self, tid):
        return {"id": tid, "state": "assigned", "owner": "fb", "retries": 0}

    def claim_info(self, tid):
        return {
            "owner": "fb",
            "state": "active",
            "lease_until": self.FUTURE_EPOCH,
            "lease_seq": 1,
        }

    def result_info(self, tid):
        return None


@pytest.fixture()
def hive_repo_with_card(world):
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001", repo="hive")
    return world.hive1


def test_store_lease_in_cache_is_tz_aware(hive_repo_with_card, monkeypatch):
    monkeypatch.setattr(mcp_cache, "claim_backend", lambda: "store")
    monkeypatch.setattr(mcp_cache, "local_store", lambda: _StubStore())

    cache = ReadCache(hive_repo_with_card, interval=0.5, git_interval=1.0)
    cache.refresh()

    claim = cache.claim_info("T-0001")
    assert claim, "stub store 的租约必须进缓存"
    lu = claim.get("lease_until")
    assert lu, "lease_until 不该为空"

    # 修复前这里直接抛 TypeError：naive < aware（拿未来租约验证「不炸」即可）
    assert parse_iso(lu) > now(), "stub 给的是未来租约，应当 active"

    # store_lease_active 对 store 原始 epoch 租约也应正常判定
    assert store_lease_active(_StubStore().claim_info("T-0001"))


def test_refresh_does_not_crash_with_store_claims(hive_repo_with_card,
                                                  monkeypatch):
    """端到端小闭环：refresh 全程不抛异常，last_error 干净。"""
    monkeypatch.setattr(mcp_cache, "claim_backend", lambda: "store")
    monkeypatch.setattr(mcp_cache, "local_store", lambda: _StubStore())

    cache = ReadCache(hive_repo_with_card, interval=0.5, git_interval=1.0)
    cache.refresh()  # 修复前：TypeError 从 refresh 里冒出来
    assert cache.status().get("cards"), "至少应缓存到 T-0001 这张卡"
