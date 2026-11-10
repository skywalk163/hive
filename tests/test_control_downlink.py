"""T-0307：控制面下行（组播 TTL=1/LOOP=0/1400B 分包/单播回退/接收 TTL 校验）单测。

用 FakeTransport 记录发送，不依赖真实组播、不 sleep 长于 2s。
"""

from __future__ import annotations

import json

import pytest

from hive_control.config import Limits
from hive_control.downlink import (
    DownlinkReceiver,
    DownlinkSender,
    SocketTransport,
    pack,
)
from hive_control.model import StateEntry
from hive_control.view import DownlinkQueue


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

def entry(tid: str, *, project: str = "p", version: int = 1,
          incarnation: int = 1, state: str = "claimed",
          dna: bool = False, urgent: bool = False,
          owner: str = "agent") -> StateEntry:
    return StateEntry(
        project=project, tid=tid, version=version, incarnation=incarnation,
        owner_agent=owner, state=state, dna=dna, updated_at=0, urgent=urgent)


class FakeTransport:
    """记录所有发送；不做真实网络。"""

    def __init__(self):
        self.multicast: list[bytes] = []
        self.unicast: list[tuple[tuple[str, int], bytes]] = []
        self.receiver_callback = None
        self.stopped = False

    def send_multicast(self, data: bytes) -> None:
        self.multicast.append(data)

    def send_unicast(self, addr: tuple[str, int], data: bytes) -> None:
        self.unicast.append((addr, data))

    def start_receiver(self, callback) -> None:
        self.receiver_callback = callback

    def stop(self) -> None:
        self.stopped = True

    def feed(self, data: bytes, addr=("127.0.0.1", 47899), ttl: int = 1) -> None:
        if self.receiver_callback is not None:
            self.receiver_callback(data, addr, ttl)


def make_sender(**limit_changes) -> tuple[DownlinkSender, FakeTransport, DownlinkQueue]:
    q = DownlinkQueue()
    limits = Limits(**limit_changes)
    t = FakeTransport()
    s = DownlinkSender(q, limits=limits, transport=t)
    return s, t, q


# --------------------------------------------------------------------------- #
# pack()
# --------------------------------------------------------------------------- #

class TestPack:
    def test_single_entry_fits(self):
        e = entry("T-1")
        packets, oversize = pack([e], max_bytes=1400)
        assert len(packets) == 1
        assert oversize == 0
        # 行以 \n 结尾
        assert packets[0].endswith(b"\n")
        # 能解析回条目
        line = packets[0].decode("utf-8").strip()
        data = json.loads(line)
        assert data["tid"] == "T-1"

    def test_multiple_entries_same_packet(self):
        e1 = entry("T-1")
        e2 = entry("T-2")
        packets, oversize = pack([e1, e2], max_bytes=1400)
        assert len(packets) == 1
        assert oversize == 0
        lines = packets[0].strip().split(b"\n")
        assert len(lines) == 2

    def test_split_at_boundary(self):
        # 每条约 120B，max_bytes=200 → 至少拆成 2 包
        e1 = entry("T-0001", project="a" * 30)
        e2 = entry("T-0002", project="b" * 30)
        e3 = entry("T-0003", project="c" * 30)
        packets, oversize = pack([e1, e2, e3], max_bytes=200)
        assert len(packets) >= 2
        assert oversize == 0
        # 每包不超 max_bytes
        for p in packets:
            assert len(p) <= 200

    def test_oversize_entry_own_packet(self):
        # 构造一条 >1400B 的条目
        big = entry("T-big", project="x" * 2000)
        packets, oversize = pack([big], max_bytes=1400)
        assert len(packets) == 1
        assert oversize == 1
        # 不截断：包内能解析回完整条目
        line = packets[0].decode("utf-8").strip()
        data = json.loads(line)
        assert data["tid"] == "T-big"
        assert data["project"] == "x" * 2000

    def test_oversize_with_existing_packet(self):
        e1 = entry("T-1")
        big = entry("T-big", project="y" * 2000)
        e3 = entry("T-3")
        packets, oversize = pack([e1, big, e3], max_bytes=1400)
        # e1 一包，big 一包，e3 可能和 big 之后攒
        assert oversize == 1
        assert len(packets) >= 2

    def test_empty(self):
        packets, oversize = pack([], max_bytes=1400)
        assert packets == []
        assert oversize == 0


# --------------------------------------------------------------------------- #
# DownlinkSender
# --------------------------------------------------------------------------- #

class TestDownlinkSender:
    def test_send_periodic_drains_queue(self):
        s, t, q = make_sender()
        q.put(entry("T-1"))
        q.put(entry("T-2"))
        n = s.send_periodic()
        assert n >= 1
        assert len(t.multicast) >= 1
        # drain 后队列空
        assert len(q) == 0

    def test_send_periodic_empty_queue(self):
        s, t, q = make_sender()
        n = s.send_periodic()
        assert n == 0
        assert t.multicast == []

    def test_send_event_single_entry(self):
        s, t, q = make_sender()
        e = entry("T-ev", state="urgent" if False else "claimed", urgent=True)
        n = s.send_event(e)
        assert n == 1
        assert len(t.multicast) == 1
        # 只装了一条
        lines = t.multicast[0].strip().split(b"\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["tid"] == "T-ev"

    def test_send_heartbeat_separate_packet(self):
        s, t, q = make_sender()
        s.send_heartbeat({"type": "heartbeat", "router_id": 1,
                          "leader_epoch": 3, "downlink_sequence": 10})
        assert len(t.multicast) == 1
        data = json.loads(t.multicast[0])
        assert data["type"] == "heartbeat"
        assert data["leader_epoch"] == 3

    def test_interval_dynamic(self):
        s, t, q = make_sender()
        assert s.interval(0) == 2.0
        assert s.interval(500) == 2.0
        assert s.interval(501) == 1.0
        assert s.interval(2000) == 1.0
        assert s.interval(2001) == 0.5

    def test_fallback_mode_sends_unicast(self):
        s, t, q = make_sender()
        s.set_peers([("10.0.0.1", 47899), ("10.0.0.2", 47899)])
        s.set_fallback(True)
        q.put(entry("T-1"))
        n = s.send_periodic()
        # 回退模式：每个 peer 各发一份
        assert len(t.unicast) == 2
        assert t.multicast == []
        addrs = {a for a, _ in t.unicast}
        assert addrs == {("10.0.0.1", 47899), ("10.0.0.2", 47899)}

    def test_fallback_no_peers_noop(self):
        s, t, q = make_sender()
        s.set_fallback(True)
        q.put(entry("T-1"))
        n = s.send_periodic()
        assert n == 0
        assert t.multicast == []
        assert t.unicast == []

    def test_status_tracks_stats(self):
        s, t, q = make_sender()
        q.put(entry("T-1"))
        s.send_periodic()
        s.send_event(entry("T-ev"))
        s.send_heartbeat({"type": "heartbeat"})
        st = s.status()
        assert st["periodic_cycles"] == 1
        assert st["events_sent"] == 1
        assert st["heartbeats_sent"] == 1
        assert st["packets_sent"] >= 3
        assert st["multicast_mode"] == "multicast"

    def test_oversize_counted_in_status(self):
        s, t, q = make_sender()
        big = entry("T-big", project="z" * 2000)
        s.send_event(big)
        st = s.status()
        assert st["oversize_packets"] == 1


# --------------------------------------------------------------------------- #
# DownlinkReceiver
# --------------------------------------------------------------------------- #

class TestDownlinkReceiver:
    def _make_receiver(self):
        received_entries = []
        received_heartbeats = []
        received_expirings = []
        t = FakeTransport()
        limits = Limits()
        r = DownlinkReceiver(
            on_entry=received_entries.append,
            on_heartbeat=received_heartbeats.append,
            on_expiring=received_expirings.append,
            limits=limits, transport=t)
        return r, t, received_entries, received_heartbeats, received_expirings

    def test_entry_dispatch(self):
        r, t, entries, hbs, exps = self._make_receiver()
        e = entry("T-1")
        r.start()
        t.feed(e.to_line().encode("utf-8") + b"\n", ttl=1)
        assert len(entries) == 1
        assert entries[0].tid == "T-1"
        assert hbs == []
        assert exps == []

    def test_heartbeat_dispatch(self):
        r, t, entries, hbs, exps = self._make_receiver()
        r.start()
        hb = {"type": "heartbeat", "router_id": 1, "leader_epoch": 2,
              "downlink_sequence": 5}
        t.feed(json.dumps(hb).encode("utf-8") + b"\n", ttl=1)
        assert len(hbs) == 1
        assert hbs[0]["leader_epoch"] == 2
        assert entries == []

    def test_expiring_dispatch(self):
        r, t, entries, hbs, exps = self._make_receiver()
        r.start()
        ex = {"type": "expiring", "project": "p", "tid": "T-9",
              "owner_agent": "w", "incarnation": 1, "version": 3}
        t.feed(json.dumps(ex).encode("utf-8") + b"\n", ttl=1)
        assert len(exps) == 1
        assert exps[0]["tid"] == "T-9"
        # expiring 不安装进 view（D4）
        assert entries == []

    def test_ttl_violation_drops_packet(self):
        r, t, entries, hbs, exps = self._make_receiver()
        r.start()
        e = entry("T-1")
        # TTL=2 应被丢弃（I4）
        t.feed(e.to_line().encode("utf-8") + b"\n", ttl=2)
        assert entries == []
        st = r.status()
        assert st["ttl_violations"] == 1
        assert st["entries_installed"] == 0

    def test_multiple_lines_in_one_packet(self):
        r, t, entries, hbs, exps = self._make_receiver()
        r.start()
        e1 = entry("T-1")
        e2 = entry("T-2")
        payload = e1.to_line().encode("utf-8") + b"\n" + e2.to_line().encode("utf-8") + b"\n"
        t.feed(payload, ttl=1)
        assert len(entries) == 2

    def test_bad_line_skipped(self):
        r, t, entries, hbs, exps = self._make_receiver()
        r.start()
        t.feed(b"not json\n", ttl=1)
        assert entries == []
        st = r.status()
        assert st["parse_errors"] == 1

    def test_status(self):
        r, t, entries, hbs, exps = self._make_receiver()
        r.start()
        t.feed(entry("T-1").to_line().encode("utf-8") + b"\n", ttl=1)
        st = r.status()
        assert st["packets_received"] == 1
        assert st["entries_installed"] == 1
        assert st["ttl_violations"] == 0


# --------------------------------------------------------------------------- #
# 不变量
# --------------------------------------------------------------------------- #

class TestInvariants:
    def test_i4_ttl_not_1_dropped(self):
        """I4：TTL≠1 丢弃。"""
        entries = []
        t = FakeTransport()
        r = DownlinkReceiver(
            on_entry=entries.append, on_heartbeat=lambda m: None,
            on_expiring=lambda m: None, limits=Limits(), transport=t)
        r.start()
        t.feed(entry("T-x").to_line().encode("utf-8") + b"\n", ttl=0)
        t.feed(entry("T-y").to_line().encode("utf-8") + b"\n", ttl=2)
        t.feed(entry("T-z").to_line().encode("utf-8") + b"\n", ttl=1)
        assert len(entries) == 1
        assert entries[0].tid == "T-z"

    def test_event_packet_single_entry(self):
        """事件包只装触发它的那一条。"""
        s, t, q = make_sender()
        s.send_event(entry("T-urgent", urgent=True, state="claimed"))
        lines = t.multicast[0].strip().split(b"\n")
        assert len(lines) == 1

    def test_heartbeat_not_merged_with_entries(self):
        """心跳单独一包，不与条目合并。"""
        s, t, q = make_sender()
        q.put(entry("T-1"))
        s.send_periodic()
        s.send_heartbeat({"type": "heartbeat"})
        # 最后一个包是心跳，不含 entry 行
        last = t.multicast[-1]
        data = json.loads(last)
        assert data.get("type") == "heartbeat"
