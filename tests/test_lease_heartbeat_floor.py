"""租约必须显著长于空闲心跳间隔，否则「租约过期」与心跳同频撞车。

2026-11-24/25 现场：`DEFAULT_LEADER_LEASE_S = 5.0` 与
`DEFAULT_IDLE_HEARTBEAT_S = 5.0` 相等 —— 空闲时主控每 5 秒才发一次心跳，
跟随者的租约恰好 5 秒到期。网络晚几十毫秒，维护循环就先判
「主节点租约过期」，清空 leader_id，下一拍心跳到了再重新采纳。

后果（都是同一根因）：
  * state.json 里 55 次 `leader_lost(主节点租约过期)` 与 54 次
    `leader_seen` 一一配对 —— 主控明明活着，却被反复"判死又复活"；
  * cluster.log 里「采纳主控」刷屏，旧日志累计 44692 次；
  * 清空走 `_emit_locked` **不写文本日志**，只看 cluster.log 完全看不出
    是谁把 leader_id 清掉的，排查方向被带偏到 git / 内存上。

覆盖：
- 默认配置下租约 ≥ 空闲心跳 × 3（留两个心跳的抖动余量）；
- 显式传入的 `leader_lease` 不被改写（测试和运维需要短租约）；
- 调大空闲心跳时，租约跟着放大；
- 活在干活时（心跳 1s）租约仍然够用。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "hive_cluster_for_leasefloor", ROOT / "hive_cluster.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hive_cluster_for_leasefloor"] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load()

SAFETY_FACTOR = 3.0


def _cluster(tmp_path, **kw):
    kw.setdefault("name", "leasefloor")
    kw.setdefault("port", 18821)
    kw.setdefault("seeds", [])
    kw.setdefault("token", "t")
    kw.setdefault("discovery_port", 0)
    return hc.Cluster(hc.Config(state_dir=str(tmp_path), **kw))


def test_default_lease_exceeds_idle_heartbeat(tmp_path, monkeypatch):
    """默认配置：租约必须比空闲心跳宽松，否则每轮都撞车。"""
    for var in ("HIVE_CLUSTER_LEADER_LEASE", "HIVE_CLUSTER_IDLE_HEARTBEAT",
                "HIVE_CLUSTER_HEARTBEAT"):
        monkeypatch.delenv(var, raising=False)
    c = _cluster(tmp_path)
    lease = c.config.leader_lease
    idle = c.config.idle_heartbeat_interval
    assert idle > 0
    assert lease >= idle * SAFETY_FACTOR, (
        f"租约 {lease}s 相对空闲心跳 {idle}s 太紧，"
        f"至少要是 {SAFETY_FACTOR} 倍，否则每轮都判过期")
    assert lease > idle, "租约必须严格长于空闲心跳"


def test_explicit_lease_is_not_overridden(tmp_path):
    """显式给 leader_lease 的调用方（含测试）保持自己的值。

    很多用例故意用 1.0s 的短租约来快速验证过期/选举，抬它会把用例
    的时序前提改掉。
    """
    c = _cluster(tmp_path, leader_lease=1.0)
    assert c.config.leader_lease == pytest.approx(1.0)


def test_lease_scales_with_idle_heartbeat(tmp_path, monkeypatch):
    """把空闲心跳调大，租约要跟着放大（不能固定死一个常数）。"""
    monkeypatch.delenv("HIVE_CLUSTER_LEADER_LEASE", raising=False)
    monkeypatch.delenv("HIVE_CLUSTER_IDLE_HEARTBEAT", raising=False)
    c = _cluster(tmp_path, idle_heartbeat_interval=10.0)
    assert c.config.leader_lease >= 10.0 * SAFETY_FACTOR


def test_lease_covers_active_heartbeat(tmp_path, monkeypatch):
    """有活干时心跳回到 1s，租约当然要更宽松。"""
    monkeypatch.delenv("HIVE_CLUSTER_LEADER_LEASE", raising=False)
    c = _cluster(tmp_path, heartbeat_interval=1.0)
    assert c.config.leader_lease >= c.config.heartbeat_interval * SAFETY_FACTOR


def test_expiry_does_not_fire_when_heartbeat_on_schedule(tmp_path, monkeypatch):
    """回归：按空闲心跳节奏喂心跳，不得被判租约过期。

    这条直接钉住现场症状 —— 主控活着、心跳准时，就不该出现
    `leader_lost(主节点租约过期)`。
    """
    monkeypatch.delenv("HIVE_CLUSTER_LEADER_LEASE", raising=False)
    monkeypatch.delenv("HIVE_CLUSTER_IDLE_HEARTBEAT", raising=False)
    c = _cluster(tmp_path)

    lid = "deadbeef" * 5
    rec = {"id": lid, "name": "L", "url": "http://127.0.0.1:18822",
           "incarnation": 1}
    c.term = 7
    with c.lock:
        c._update_node_locked(dict(rec), touch=True)
        c._adopt_leader_locked(dict(rec), 7, refresh_lease=True)

    # 模拟「心跳按空闲间隔准时到达」：把时间往前推一个空闲间隔，
    # 心跳正好在租约到期前进站，不应触发过期。
    lost = []
    with c.lock:
        horizon = hc._now() + c.config.idle_heartbeat_interval * 0.95
        if c.leader_lease_until and horizon > c.leader_lease_until:
            lost.append("租约会在下一次空闲心跳前就过期")
    assert not lost, (
        f"空闲心跳 {c.config.idle_heartbeat_interval}s / 租约 "
        f"{c.config.leader_lease}s —— 心跳还没到租约就过期了")
