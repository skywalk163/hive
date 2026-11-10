"""T-0303 控制面 Leader 选举：带宽感知 / Waiting / 非抢占 / 心跳失效。

全部用注入时钟（FakeClock）驱动，不 sleep 真实秒数、不建任何 socket、
不依赖真实网卡；网络收发由 T-0310 装配，本测试只验证 payload 与状态机。
契约见 docs/控制面v0.5-实现约定.md §3.2 与 §4 D2/D10/D11/D12。
"""

from __future__ import annotations

import sys

import pytest

from hive_control.config import ControlConfig, Limits
from hive_control.election import BandwidthProbe, Election, SelfInfo


class FakeClock:
    """可单调推进的注入时钟；所有时间都来自它，测试里绝不 sleep。"""

    def __init__(self, start: float = 1000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def self_info(rid: int = 10, priority: int = 1, *,
             mbps: float | None = 1000.0, node: str | None = None,
             address: str | None = None) -> SelfInfo:
    return SelfInfo(
        router_id=rid,
        node=node or f"node-{rid}",
        address=address or f"10.0.0.{rid}",
        port=47899,
        priority=priority,
        interface_mbps=mbps,
    )


def make(clock: FakeClock, info: SelfInfo | None = None, *,
         limits: Limits | None = None,
         config: ControlConfig | None = None) -> Election:
    return Election(info or self_info(),
                    limits=limits or Limits(),
                    config=config or ControlConfig(),
                    clock=clock)


def hello(rid: int, priority: int = 1, *, mbps: float | None = 1000.0,
          declare: bool = False, epoch: int = 0,
          factor: float | None = None, node: str | None = None) -> dict:
    return {
        "type": "hello",
        "router_id": rid,
        "node": node or f"node-{rid}",
        "address": f"10.0.0.{rid}",
        "port": 47899,
        "priority": priority,
        "bandwidth_factor": factor,
        "interface_mbps": mbps,
        "leader_declaration": declare,
        "leader_epoch": epoch,
        "sent_at": 0,
    }


def heartbeat(rid: int, epoch: int, seq: int = 1) -> dict:
    return {"type": "heartbeat", "router_id": rid,
            "leader_epoch": epoch, "downlink_sequence": seq}


# --------------------------------------------------------------------------- #
# BandwidthProbe
# --------------------------------------------------------------------------- #

def test_bandwidth_factor_ratio():
    probe = BandwidthProbe(speed_mbps=100.0)
    assert probe.interface_mbps() == 100.0
    assert probe.factor(1000.0) == pytest.approx(0.1)
    assert probe.factor(50.0) == pytest.approx(2.0)


@pytest.mark.parametrize("bad", [None, 0, -1, "abc", float("nan"),
                                 float("inf"), True])
def test_bandwidth_factor_invalid_reference_falls_back_to_one(bad):
    probe = BandwidthProbe(speed_mbps=1000.0)
    assert probe.factor(bad) == 1.0


def test_bandwidth_unavailable_falls_back_to_one(caplog):
    probe = BandwidthProbe(speed_mbps=None)
    assert probe.interface_mbps() is None
    assert probe.factor(1000.0) == 1.0
    with caplog.at_level("WARNING"):
        assert probe.factor(1000.0) == 1.0
        assert probe.factor(1000.0) == 1.0
    assert len([r for r in caplog.records
                if "bandwidth_unavailable" in r.getMessage()]) == 1


def test_bandwidth_unavailable_on_non_linux(monkeypatch):
    # D10：Windows/FreeBSD 上 SIOCETHTOOL 不可用，interface_mbps 必须是 None。
    monkeypatch.setattr(sys, "platform", "win32")
    probe = BandwidthProbe()
    assert probe.interface_mbps() is None
    assert probe.factor(1000.0) == 1.0


def test_router_id_is_stable_deterministic_and_address_sensitive():
    probe = BandwidthProbe()
    a = probe.router_id("10.0.0.5", "alpha")
    assert a == probe.router_id("10.0.0.5", "alpha")
    assert 0 <= a <= 0xFFFFFFFF
    assert a != probe.router_id("10.0.0.6", "alpha")
    assert a != probe.router_id("10.0.0.5", "beta")
    assert probe.router_id(None, "") == probe.router_id(None, "")


def test_selfinfo_normalizes_fields():
    info = SelfInfo(router_id="7", node=1, address=None, port="47899",
                    priority="2", interface_mbps="1000")  # type: ignore[arg-type]
    assert info.router_id == 7 and info.node == "1"
    assert info.priority == 2 and info.interface_mbps == 1000.0
    assert SelfInfo(1, "n", "a", 1, 0, -5).interface_mbps is None


# --------------------------------------------------------------------------- #
# Hello / Heartbeat payload
# --------------------------------------------------------------------------- #

def test_hello_payload_shape_and_no_send():
    clock = FakeClock()
    election = make(clock, self_info(rid=42, priority=3, mbps=1000.0))
    payload = election.hello()
    assert payload["type"] == "hello"
    assert payload["router_id"] == 42
    assert payload["node"] == "node-42"
    assert payload["address"] == "10.0.0.42"
    assert payload["port"] == 47899
    assert payload["priority"] == 3
    assert payload["bandwidth_factor"] == pytest.approx(1.0)
    assert payload["interface_mbps"] == 1000.0
    assert payload["leader_declaration"] is False
    assert payload["leader_epoch"] == 0
    assert payload["sent_at"] == int(clock() * 1000)

    clock.advance(10)
    assert election.hello()["sent_at"] == int(clock() * 1000)


def test_heartbeat_payload_sequence_increments():
    clock = FakeClock()
    election = make(clock)
    a = election.heartbeat()
    b = election.heartbeat()
    assert a["type"] == "heartbeat"
    assert a["router_id"] == election.self_info.router_id
    assert b["downlink_sequence"] == a["downlink_sequence"] + 1


def test_status_due_flags_use_injected_clock():
    clock = FakeClock()
    election = make(clock)
    assert election.status()["hello_due"] is True
    election.hello()
    clock.advance(1.9)
    assert election.status()["hello_due"] is False
    clock.advance(0.2)
    assert election.status()["hello_due"] is True


# --------------------------------------------------------------------------- #
# Waiting
# --------------------------------------------------------------------------- #

def test_waiting_then_declare_then_become_leader():
    clock = FakeClock()
    election = make(clock, config=ControlConfig(waiting_interval=40.0))
    assert election.status()["phase"] == "waiting"
    assert election.tick() == "wait"
    assert election.is_leader is False
    assert election.leader_id is None

    clock.advance(39.9)
    assert election.tick() == "wait"
    clock.advance(0.2)
    assert election.tick() == "declare"
    assert election.hello()["leader_declaration"] is True
    assert election.tick() == "become_leader"
    assert election.is_leader is True
    assert election.leader_id == election.self_info.router_id


def test_waiting_leader_declaration_is_immediately_adopted():
    clock = FakeClock()
    election = make(clock, config=ControlConfig(waiting_interval=40.0))
    assert election.observe(hello(99, priority=5, declare=True,
                                  epoch=7)) == "adopted"
    assert election.leader_id == 99
    assert election.leader_epoch == 7
    assert election.is_leader is False
    # Waiting 提前结束；即便过了 waiting_interval，只要心跳有效也不竞选。
    for i in range(20):
        clock.advance(5.0)
        election.observe(heartbeat(99, epoch=7, seq=i + 1))
        assert election.tick() == "none"
    assert election.hello()["leader_declaration"] is False


def test_heartbeat_during_waiting_is_adopted():
    clock = FakeClock()
    election = make(clock)
    assert election.observe(heartbeat(77, epoch=3)) == "adopted"
    assert election.leader_id == 77
    assert election.leader_epoch == 3


# --------------------------------------------------------------------------- #
# 确定性排序：priority → bandwidth_factor → router_id
# --------------------------------------------------------------------------- #

def test_higher_priority_peer_blocks_own_declaration():
    clock = FakeClock()
    election = make(clock, self_info(rid=1, priority=1, mbps=1000.0),
                    config=ControlConfig(waiting_interval=0.001))
    election.observe(hello(2, priority=2, mbps=100.0))
    clock.advance(0.01)
    assert election.tick() == "none"  # 排名更高的邻居存在，不自封


def test_bandwidth_factor_breaks_priority_tie():
    clock = FakeClock()
    election = make(clock, self_info(rid=1, priority=1, mbps=1000.0),
                    config=ControlConfig(waiting_interval=0.001))
    # 邻居同 priority、同 router_id 权重低，但带宽只有 100 vs 自己 1000。
    election.observe(hello(2, priority=1, mbps=100.0))
    clock.advance(0.01)
    assert election.tick() == "declare"
    assert election.tick() == "become_leader"


def test_router_id_breaks_full_tie():
    clock = FakeClock()
    election = make(clock, self_info(rid=100, priority=1, mbps=1000.0),
                    config=ControlConfig(waiting_interval=0.001))
    election.observe(hello(99, priority=1, mbps=1000.0))
    clock.advance(0.01)
    assert election.tick() == "declare"

    lower = make(clock, self_info(rid=98, priority=1, mbps=1000.0),
                 config=ControlConfig(waiting_interval=0.001))
    lower.observe(hello(99, priority=1, mbps=1000.0))
    clock.advance(0.01)
    assert lower.tick() == "none"  # 全平局但 router_id 98 < 99，不自封


def test_reference_bandwidth_is_max_over_alive_peers_including_self():
    clock = FakeClock()
    election = make(clock, self_info(rid=1, priority=1, mbps=100.0))
    assert election.reference_mbps == 100.0
    election.observe(hello(2, priority=1, mbps=10000.0))
    assert election.reference_mbps == 10000.0
    assert election.status()["bandwidth_factor"] == pytest.approx(0.01)
    # 邻居过期后回到自身值（peer_timeout 6s，用 FakeClock 推进）。
    clock.advance(7.0)
    assert election.reference_mbps == 100.0
    assert len(election.peers()) == 0


def test_reference_bandwidth_config_override():
    clock = FakeClock()
    election = make(clock, self_info(rid=1, mbps=100.0),
                    config=ControlConfig(reference_bandwidth=200.0))
    assert election.reference_mbps == 200.0
    assert election.status()["bandwidth_factor"] == pytest.approx(0.5)
    election.observe(hello(2, mbps=10000.0))
    assert election.reference_mbps == 200.0  # 人工覆盖优先


def test_all_bandwidth_unknown_falls_back_to_one(caplog):
    clock = FakeClock()
    election = make(clock, self_info(rid=1, mbps=None))
    election.observe(hello(2, priority=1, mbps=None))
    assert election.reference_mbps is None
    with caplog.at_level("WARNING"):
        assert election.status()["bandwidth_factor"] == 1.0
        assert election.hello()["bandwidth_factor"] == 1.0
    assert len([r for r in caplog.records
                if "bandwidth_unavailable" in r.getMessage()]) == 1


def test_priority_zero_does_not_run_but_can_be_adopted():
    clock = FakeClock()
    election = make(clock, self_info(rid=1, priority=0, mbps=1000.0),
                    config=ControlConfig(waiting_interval=0.001))
    clock.advance(0.01)
    assert election.tick() == "none"
    assert election.is_leader is False
    # priority=0 仍可承认别人当 Leader。
    assert election.observe(hello(9, declare=True, epoch=4)) == "adopted"
    assert election.leader_id == 9


# --------------------------------------------------------------------------- #
# 非抢占
# --------------------------------------------------------------------------- #

def test_no_preemption_while_incumbent_heartbeat_valid():
    clock = FakeClock()
    # 自己 priority 更高（10 vs 1），但现任 Leader 心跳有效时不发起。
    election = make(clock, self_info(rid=1, priority=10, mbps=10000.0),
                    config=ControlConfig(waiting_interval=0.001))
    election.observe(hello(2, priority=1, declare=True, epoch=1))
    for _ in range(10):
        clock.advance(1.0)
        election.observe(heartbeat(2, epoch=1))
        assert election.tick() == "none"
        assert election.is_leader is False
        assert election.leader_id == 2
        assert election.hello()["leader_declaration"] is False


def test_no_preemption_peer_hello_declaration_ignored_while_leader_valid():
    clock = FakeClock()
    election = make(clock, config=ControlConfig(waiting_interval=0.001))
    election.observe(hello(2, declare=True, epoch=5))
    # 另一个节点也自称 Leader，但现任心跳有效 → 不改主。
    assert election.observe(hello(3, declare=True, epoch=5)) == "hello"
    assert election.leader_id == 2
    assert election.tick() == "none"


# --------------------------------------------------------------------------- #
# 心跳失效 / epoch 单调
# --------------------------------------------------------------------------- #

def test_heartbeat_timeout_returns_leader_lost_then_new_epoch():
    clock = FakeClock()
    election = make(clock, config=ControlConfig(waiting_interval=0.001))
    election.observe(hello(2, declare=True, epoch=9))
    assert election.leader_epoch == 9
    clock.advance(6.1)  # heartbeat_timeout=6s，FakeClock 推进，无 sleep
    assert election.tick() == "leader_lost"
    assert election.leader_id is None
    assert election.is_leader is False
    assert election.tick() == "declare"
    assert election.tick() == "become_leader"
    # 新 Leader epoch = 已知最大 epoch + 1
    assert election.leader_epoch == 10
    assert election.heartbeat()["leader_epoch"] == 10


def test_heartbeat_refreshes_leader_lease_and_neighbor_liveness():
    clock = FakeClock()
    election = make(clock, config=ControlConfig(waiting_interval=0.001))
    election.observe(hello(2, declare=True, epoch=1))
    for i in range(12):
        clock.advance(1.0)
        election.observe(heartbeat(2, epoch=1, seq=i + 1))
        assert election.tick() == "none"
        assert election.leader_id == 2
    assert election.status()["leader_age"] == pytest.approx(0.0)


def test_higher_epoch_replaces_incumbent():
    clock = FakeClock()
    election = make(clock, config=ControlConfig(waiting_interval=0.001))
    election.observe(hello(2, declare=True, epoch=1))
    assert election.observe(heartbeat(3, epoch=4)) == "leader_lost"
    assert election.leader_id == 3
    assert election.leader_epoch == 4
    # 老 Leader 的低 epoch 心跳不再能翻盘。
    clock.advance(1)
    assert election.observe(heartbeat(2, epoch=1)) == "ignored"
    assert election.leader_id == 3


def test_higher_epoch_dethrones_self_leader():
    clock = FakeClock()
    election = make(clock, config=ControlConfig(waiting_interval=0.001))
    clock.advance(0.01)
    assert election.tick() == "declare"
    assert election.tick() == "become_leader"
    assert election.is_leader is True
    assert election.observe(heartbeat(99, epoch=50)) == "leader_lost"
    assert election.is_leader is False
    assert election.leader_id == 99
    assert election.leader_epoch == 50


def test_downranked_peer_declaration_suppresses_own_declaration():
    clock = FakeClock()
    election = make(clock, self_info(rid=100, priority=5, mbps=1000.0),
                    config=ControlConfig(waiting_interval=0.001))
    clock.advance(0.01)
    assert election.tick() == "declare"
    # 同 epoch、排名更高的节点也声明 → 撤回本轮，承认它。
    assert election.observe(hello(200, priority=5, mbps=1000.0,
                                   declare=True, epoch=1)) == "leader_lost"
    assert election.is_leader is False
    assert election.leader_id == 200
    clock.advance(1)
    assert election.tick() == "none"


# --------------------------------------------------------------------------- #
# 邻居过期 / 畸形报文 / 回环
# --------------------------------------------------------------------------- #

def test_peer_expires_after_peer_timeout_and_can_declare():
    clock = FakeClock()
    election = make(clock, self_info(rid=1, priority=1, mbps=1000.0),
                    config=ControlConfig(waiting_interval=0.001))
    election.observe(hello(200, priority=5, mbps=1000.0))
    assert len(election.peers()) == 1
    clock.advance(0.01)
    assert election.tick() == "none"   # 高排名邻居还在，不自封
    clock.advance(6.1)               # peer_timeout=6s，邻居过期
    assert len(election.peers()) == 0
    assert election.tick() == "declare"
    assert election.tick() == "become_leader"


def test_peer_fields_exposed_for_unicast_target():
    clock = FakeClock()
    election = make(clock)
    election.observe(hello(7, priority=2, mbps=100.0), source=("10.9.9.9", 1234))
    peers = election.peers()
    assert len(peers) == 1
    assert peers[0]["router_id"] == 7
    assert peers[0]["address"] == "10.0.0.7"
    assert peers[0]["port"] == 47899
    assert peers[0]["source"] == ["10.9.9.9", 1234]
    assert peers[0]["priority"] == 2
    assert peers[0]["interface_mbps"] == 100.0


def test_ignored_messages_do_not_change_state():
    clock = FakeClock()
    election = make(clock, config=ControlConfig(waiting_interval=0.001))
    assert election.observe({"type": "entry", "project": "p"}) == "ignored"
    assert election.observe({"type": "hello"}) == "ignored"        # 缺 router_id
    assert election.observe({"type": "heartbeat"}) == "ignored"
    assert election.observe("not-a-dict") == "ignored"              # type: ignore[arg-type]
    assert election.leader_id is None
    assert election.is_leader is False
    assert election.peers() == []


def test_own_loopback_hello_is_ignored():
    clock = FakeClock()
    election = make(clock, self_info(rid=42))
    assert election.observe(hello(42, declare=True, epoch=3)) == "ignored"
    assert election.leader_id is None
    assert election.is_leader is False
    assert election.peers() == []


def test_heartbeat_from_self_is_ignored():
    clock = FakeClock()
    election = make(clock, self_info(rid=42))
    assert election.observe(heartbeat(42, epoch=9)) == "ignored"
    assert election.leader_id is None


def test_status_snapshot_is_complete_and_json_friendly():
    clock = FakeClock()
    election = make(clock, self_info(rid=5, priority=2, mbps=100.0))
    election.observe(hello(6, priority=1, mbps=50.0),
                     source=("10.0.0.6", 47899))
    snap = election.status()
    for key in ("phase", "router_id", "priority", "interface_mbps",
                "bandwidth_factor", "reference_mbps", "leader_id",
                "leader_epoch", "max_epoch", "is_leader", "declared",
                "waiting", "peer_count", "hello_due", "heartbeat_due",
                "heartbeat_timeout", "peer_timeout"):
        assert key in snap
    assert snap["phase"] == "waiting"
    assert snap["peer_count"] == 1
    assert snap["reference_mbps"] == 100.0
    assert snap["bandwidth_factor"] == pytest.approx(1.0)
    import json
    json.dumps(snap)  # 不含不可序列化的对象（source 已转 list）


@pytest.mark.parametrize("priority,rid,mbps,expected", [
    (5, 1, 1000.0, True),     # priority 5 > 3 胜
    (3, 1, 1000.0, True),     # priority 平，factor 1.0 > 0.5 胜
    (3, 100, 500.0, True),    # 全平，router_id 100 > 50 胜
    (3, 1, 500.0, False),     # 全平，router_id 1 < 50 败
    (1, 100, 1000.0, False),  # priority 1 < 3 败（带宽再高也不抢）
])
def test_rank_order_priority_bandwidth_router_id(priority, rid, mbps, expected):
    clock = FakeClock()
    # 参考带宽钉死 1000Mbps：邻居固定排名键 (3, 0.5, 50)
    info = self_info(rid=rid, priority=priority, mbps=mbps)
    election = make(clock, info,
                    config=ControlConfig(waiting_interval=0.001,
                                         reference_bandwidth=1000.0))
    election.observe(hello(50, priority=3, mbps=500.0))
    clock.advance(0.01)
    own = (priority, (mbps or 0) / 1000.0, rid)
    peer = (3, 0.5, 50)
    assert (own > peer) is expected
    assert (election.tick() == "declare") is expected
