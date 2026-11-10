"""T-0310：控制面编排器（线程装配 / 切主 / DNA / 缺口拉取 / 人工拉取队列）单测。

约定（契约 §7）：注入时钟 + 假 transport + 临时端口，不依赖真实组播、
不依赖外网、不 ``sleep`` 长于 2s。所有时序断言都通过 ``tick()`` + 假时钟
推进，不用真实秒数等待。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from hive_control.config import ControlConfig, Limits
from hive_control.model import StateEntry
from hive_control.plane import (
    PULL_DONE,
    PULL_ERROR,
    PULL_REJECTED,
    ControlPlane,
)
from hive_control.pull import PullRetry
from hive_control.view import DownlinkQueue


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

# Waiting 期必须 > 0（ControlConfig 自己会校验），单测取极短值靠假时钟推进。
WAITING = 0.001

HAS_AF_UNIX = hasattr(socket, "AF_UNIX")
requires_af_unix = pytest.mark.skipif(
    not HAS_AF_UNIX, reason="平台不支持 AF_UNIX")


class Clock:
    """可控时钟：``advance()`` 推进，``__call__`` 供各模块注入。"""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += float(delta)


class FakeTransport:
    """记录所有发送；``feed()`` 把报文塞进接收回调，不碰真实 socket。"""

    def __init__(self):
        self.multicast: list[bytes] = []
        self.unicast: list[tuple[tuple[str, int], bytes]] = []
        self.callback = None
        self.starts = 0
        self.stops = 0

    # Transport 协议 ---------------------------------------------------- #
    def send_multicast(self, data: bytes) -> None:
        self.multicast.append(data)

    def send_unicast(self, addr: tuple[str, int], data: bytes) -> None:
        self.unicast.append((addr, data))

    def start_receiver(self, callback) -> None:
        self.callback = callback
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    # 测试辅助 ---------------------------------------------------------- #
    def feed(self, data: bytes, addr=("10.0.0.9", 47899), ttl: int = 1) -> None:
        assert self.callback is not None, "接收器还没启动"
        self.callback(data, addr, ttl)

    def feed_messages(self, *messages, **kwargs) -> None:
        payload = b"".join(
            (m.to_line() if isinstance(m, StateEntry) else str(m)).encode("utf-8")
            + b"\n"
            for m in messages)
        self.feed(payload, **kwargs)

    def unicast_to(self, addr: tuple[str, int]) -> list[bytes]:
        return [data for target, data in self.unicast if target == addr]

    def multicast_lines(self) -> list[dict]:
        return _split_lines(self.multicast)

    def unicast_lines(self, addr: tuple[str, int]) -> list[dict]:
        return _split_lines(self.unicast_to(addr))


def _split_lines(packets: list[bytes]) -> list[dict]:
    out: list[dict] = []
    for packet in packets:
        for raw in packet.split(b"\n"):
            line = raw.strip()
            if line:
                out.append(json.loads(line.decode("utf-8")))
    return out


class FixedIncarnation:
    """不写盘的 incarnation 存储：单测要确定性，也不污染全局计数文件。"""

    def __init__(self, value: int):
        self.value = int(value)

    def next(self) -> int:
        self.value += 1
        return self.value

    def current(self) -> int:
        return self.value


@pytest.fixture()
def short_sock():
    """短路径 socket 文件名（AF_UNIX 路径有 100+ 字节限制，tmp_path 常常过长）。

    没有 AF_UNIX 的平台照样给路径：Worker socket 起不来本来就是控制面必须
    容忍的情况（``start()`` 记账不抛），只有真要真 socket 的用例才跳过。
    """
    base = Path(tempfile.mkdtemp(prefix="hv-"))
    path = base / "c.sock"
    if len(str(path).encode()) > 100:
        pytest.skip("临时目录过长，AF_UNIX 路径不可用")
    yield str(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        os.rmdir(base)
    except OSError:
        pass


def entry(
    tid: str = "T-1",
    *,
    project: str = "p",
    version: int = 1,
    incarnation: int = 1,
    state: str = "claimed",
    dna: bool = False,
    owner: str = "alice",
    urgent: bool = False,
) -> StateEntry:
    return StateEntry(
        project=project, tid=tid, version=version, incarnation=incarnation,
        owner_agent=owner, state=state, dna=dna, updated_at=0, urgent=urgent)


def make_plane(
    *,
    sock_path: str,
    clock: Clock | None = None,
    incarnation: int = 7,
    node: str = "n1",
    address: str = "127.0.0.1",
    listen_port: int = 0,
    start_threads: bool = False,
    config: ControlConfig | None = None,
    peer_provider=None,
    local_agents=None,
    **limit_changes,
) -> tuple[ControlPlane, FakeTransport, Clock]:
    """装一个不启动后台线程、不碰真实端口的控制面。"""
    clock = clock or Clock()
    limits = Limits(socket_path=sock_path, multicast_port=47899,
                    **limit_changes)
    transport = FakeTransport()
    plane = ControlPlane(
        node=node,
        address=address,
        listen_port=listen_port,
        config=config or ControlConfig(waiting_interval=WAITING),
        limits=limits,
        transport=transport,
        clock=clock,
        peer_provider=peer_provider,
        local_agents=local_agents,
        start_threads=start_threads,
        incarnation_store=FixedIncarnation(incarnation),
    )
    return plane, transport, clock


def become_leader(plane: ControlPlane, clock: Clock) -> None:
    """让单节点控制面走完 Waiting → declare → become_leader（靠假时钟推进）。"""
    clock.advance(0.01)
    plane.tick()          # declare
    clock.advance(0.1)
    plane.tick()          # become_leader


def adopt(plane: ControlPlane, *, router_id: int = 77,
          address: str = "10.0.0.2", port: int = 8788,
          priority: int = 9, epoch: int = 1) -> str:
    """让本机承认一个远端 Leader（Waiting 期间收到 leader_declaration）。"""
    return plane.election.observe({
        "type": "hello", "router_id": router_id, "node": "n2",
        "address": address, "port": port, "priority": priority,
        "leader_declaration": True, "leader_epoch": epoch,
    })


class RecordingPullClient:
    """记录拉取调用；可注入返回值或异常，不碰网络。"""

    def __init__(self, entries=None, error: Exception | None = None):
        self.calls: list[tuple[str, int, dict]] = []
        self._entries = list(entries or [])
        self._error = error
        self.bad_lines = 0
        self.pages = 0

    def fetch(self, host: str, port: int, request: dict):
        self.calls.append((host, int(port), dict(request)))
        if self._error is not None:
            raise self._error
        return list(self._entries)


def wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


# --------------------------------------------------------------------------- #
# 1. 装配
# --------------------------------------------------------------------------- #

def test_constructor_assembles_all_subsystems_without_network(short_sock):
    plane, transport, _clock = make_plane(sock_path=short_sock)
    try:
        assert plane.view is not None
        assert isinstance(plane.downlink_queue, DownlinkQueue)
        assert plane.election is not None
        assert plane.lease is not None
        assert plane.uplink_queue is not None
        assert plane.aggregator is not None
        assert plane.uplink_sender is not None
        assert plane.tcp_server is not None
        assert plane.downlink_sender is not None
        assert plane.downlink_receiver is not None
        assert plane.worker_server is not None
        assert plane.pull_service is not None
        assert plane.pull_client is not None
        # 没 start 就不该有任何网络动作
        assert transport.starts == 0
        assert transport.multicast == []
        assert plane.status()["started"] is False
    finally:
        plane.stop()


def test_start_bumps_incarnation_and_is_idempotent(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock, incarnation=7)
    try:
        plane.start()
        first = plane.status()["incarnation"]
        plane.start()
        second = plane.status()["incarnation"]
        # 启动时读/递增一次（D5）；重复 start 不重复递增。
        assert first == 8
        assert second == 8
        assert plane.status()["started"] is True
    finally:
        plane.stop()


def test_stop_is_idempotent_and_stops_every_child(short_sock):
    plane, transport, _clock = make_plane(sock_path=short_sock)
    plane.start()
    plane.stop()
    plane.stop()
    assert transport.stops >= 1
    assert plane.status()["started"] is False


@requires_af_unix
def test_start_tolerates_busy_worker_socket(short_sock):
    """socket/端口起不来要记账，而不是把 serve 一起拖死。"""
    blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    blocker.bind(short_sock)
    blocker.listen(1)
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()  # 不抛
        status = plane.status()
        assert status["started"] is True
        assert any("worker-sock" in item for item in status["errors"])
    finally:
        plane.stop()
        blocker.close()


def test_report_entry_before_start_still_installs(short_sock):
    """report_entry 只做内存操作（I6）：没 start 也能安装进视图。"""
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.report_entry(entry("T-early"))
        assert plane.view.get("p", "T-early") is not None
        assert plane.status()["counters"]["reported"] == 1
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 2. D5 盖号
# --------------------------------------------------------------------------- #

def test_local_report_stamps_incarnation_and_increments_version(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock, incarnation=7)
    try:
        plane.start()
        # Worker 报 version=0 / incarnation=0（D5：serve 统一盖号）
        plane.report_entry(entry("T-1", version=0, incarnation=0))
        first = plane.view.get("p", "T-1")
        assert first is not None
        assert first.incarnation == 8          # 本 serve 的 incarnation
        assert first.version == 1              # 至少 1

        plane.report_entry(entry("T-1", version=0, incarnation=0,
                                 state="running"))
        second = plane.view.get("p", "T-1")
        assert second is not None
        assert second.version == 2             # 本地更新严格 +1
        assert second.state == "running"
        assert second.incarnation == 8
    finally:
        plane.stop()


def test_local_report_never_rolls_version_back(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        plane.report_entry(entry("T-1", version=50))
        assert plane.view.get("p", "T-1").version == 50
        # Worker 传一个更小的号，不能把视图版本拉回去
        plane.report_entry(entry("T-1", version=3))
        assert plane.view.get("p", "T-1").version == 51
    finally:
        plane.stop()


def test_report_entry_rejects_non_entry(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        with pytest.raises(TypeError):
            plane.report_entry({"tid": "T-1"})  # type: ignore[arg-type]
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 3. Leader 路径：本地条目 → 待下行队列 / 瞬态事件包
# --------------------------------------------------------------------------- #

def test_leader_local_steady_entry_goes_to_downlink_queue(short_sock):
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        assert plane.election.is_leader

        plane.report_entry(entry("T-1", state="steady"))
        assert len(plane.downlink_queue) == 1
        # steady 不是瞬态：不发事件包
        assert plane.status()["counters"]["events"] == 0
    finally:
        plane.stop()


@pytest.mark.parametrize("state", ["claimed", "releasing"])
def test_leader_transient_entry_sends_immediate_event(short_sock, state):
    plane, transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        before = len(transport.multicast)

        plane.report_entry(entry("T-tx", state=state))
        # 瞬态：立刻多一个事件包（只装这一条）
        assert len(transport.multicast) == before + 1
        assert plane.status()["counters"]["events"] == 1
        payload = transport.multicast[-1].strip()
        assert b'"tid":"T-tx"' in payload
        assert payload.count(b"\n") == 0
    finally:
        plane.stop()


def test_leader_urgent_entry_sends_event(short_sock):
    plane, transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        before = len(transport.multicast)
        plane.report_entry(entry("T-u", state="steady", urgent=True))
        assert len(transport.multicast) == before + 1
    finally:
        plane.stop()


def test_non_leader_local_entry_goes_uplink_via_aggregator(short_sock):
    """非 Leader：本地条目进聚合器 → 上行队列，不进下行队列。"""
    plane, _transport, clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=40.0))
    try:
        plane.start()
        assert not plane.election.is_leader     # 还在 Waiting
        plane.report_entry(entry("T-1", state="steady"))
        assert len(plane.downlink_queue) == 0

        clock.advance(1.0)
        plane.tick()                            # 聚合窗口已过 → flush
        assert plane.uplink_queue.qsize() == 1
        assert plane.status()["counters"]["uplink_queued"] == 1
    finally:
        plane.stop()


def test_aggregator_keeps_only_latest_version_per_key(short_sock):
    plane, _transport, clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=40.0))
    try:
        plane.start()
        plane.report_entry(entry("T-1", state="running"))
        plane.report_entry(entry("T-1", state="steady"))
        clock.advance(1.0)
        plane.tick()
        assert plane.uplink_queue.qsize() == 1
        queued = plane.uplink_queue.get(timeout=0)
        assert queued.state == "steady"
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 4. Hello / 心跳 / 切主
# --------------------------------------------------------------------------- #

def test_leader_sends_heartbeat_on_each_interval(short_sock):
    plane, transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        # 成为 Leader 的首轮已经发过一次心跳
        first = plane.status()["counters"]["heartbeats"]
        assert first >= 1

        clock.advance(2.5)
        plane.tick()
        assert plane.status()["counters"]["heartbeats"] == first + 1
        assert transport.multicast_lines()[-1]["type"] == "heartbeat"
    finally:
        plane.stop()


def test_hello_is_unicast_and_follower_never_multicasts(short_sock):
    """I3 + D2：Hello 走单播；非 Leader 不发组播。"""
    seeds = [{"address": "10.0.0.2", "node": "n2", "priority": 1},
             {"address": "10.0.0.3", "node": "n3", "priority": 1}]
    plane, transport, clock = make_plane(
        sock_path=short_sock, peer_provider=lambda: list(seeds))
    try:
        plane.start()
        # 先让本机 declare，Hello 里才会带 leader_declaration=true
        clock.advance(0.01)
        plane.tick()
        assert plane.election.status()["declared"] is True

        targets = {addr for addr, _data in transport.unicast}
        assert ("10.0.0.2", 47899) in targets
        assert ("10.0.0.3", 47899) in targets
        # 自己的地址不给自己发
        assert ("127.0.0.1", 47899) not in targets
        assert transport.multicast == []      # I3：非 Leader 不发组播
        # Hello payload 形状（§2.2）
        hello = transport.unicast_lines(("10.0.0.2", 47899))[-1]
        assert hello["type"] == "hello"
        assert hello["node"] == "n1"
        assert hello["address"] == "127.0.0.1"
        assert hello["leader_declaration"] is True
    finally:
        plane.stop()


def test_hello_interval_throttles_unicast(short_sock):
    seeds = [{"address": "10.0.0.2", "node": "n2"}]
    plane, transport, clock = make_plane(
        sock_path=short_sock,
        peer_provider=lambda: list(seeds),
        # priority=0 不参选，稳定停在非 Leader，避免选举事件干扰计数
        config=ControlConfig(waiting_interval=WAITING, leader_priority=0))
    try:
        plane.start()
        after_start = len(transport.unicast)
        plane.tick()
        plane.tick()
        assert len(transport.unicast) == after_start      # 2s 内不再发

        clock.advance(2.5)
        plane.tick()
        assert len(transport.unicast) == after_start + 1
    finally:
        plane.stop()


def test_adopting_a_declared_leader_makes_us_follower(short_sock):
    plane, _transport, clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=40.0))
    try:
        plane.start()
        assert plane.election.leader_id is None
        # 别人的声明（priority 更高）→ Waiting 期间立刻承认，不竞选
        assert adopt(plane, router_id=424242, priority=9, epoch=3) == "adopted"
        clock.advance(0.1)
        plane.tick()
        assert plane.election.is_leader is False
        assert plane.election.leader_id == 424242
        status = plane.status()
        assert status["control_leader"] == 424242
        assert status["control_epoch"] == 3
        # 上行与拉取目标都指向 Leader（拉取端口 = port + 1）
        assert plane._leader_endpoint() == ("10.0.0.2", 8788)
        assert plane._pull_endpoint() == ("10.0.0.2", 8789)
        assert status["uplink_target"] == ["10.0.0.2", 8788]
    finally:
        plane.stop()


def test_inbound_hello_reaches_election_state_machine(short_sock):
    """装配：入站 ``hello`` 必须喂给选举状态机。

    ``DownlinkReceiver`` 按契约只分发 entry/heartbeat/expiring，Hello 会被它
    丢掉；编排器负责在传输层把 Hello 截出来，否则多节点永远看不到彼此。
    """
    plane, transport, clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=40.0))
    try:
        plane.start()
        assert plane.election.peers() == []

        # 邻居 Hello（不声明 Leader）：只建邻居表，不改主
        transport.feed(
            b'{"type":"hello","router_id":555,"node":"n2",'
            b'"address":"10.0.0.2","port":8788,"priority":5,'
            b'"leader_declaration":false,"leader_epoch":0}\n',
            addr=("10.0.0.2", 47899))
        peers = plane.election.peers()
        assert [p["router_id"] for p in peers] == [555]
        assert plane.election.leader_id is None

        # 邻居声明 Leader：Waiting 期间立刻承认，不竞选
        transport.feed(
            b'{"type":"hello","router_id":777,"node":"n3",'
            b'"address":"10.0.0.3","port":8788,"priority":9,'
            b'"leader_declaration":true,"leader_epoch":4}\n',
            addr=("10.0.0.3", 47899))
        clock.advance(0.1)
        plane.tick()
        assert plane.election.is_leader is False
        assert plane.election.leader_id == 777
        assert plane.status()["counters"]["leader_changes"] >= 1
    finally:
        plane.stop()


def test_heartbeat_refreshes_leader_and_prevents_reelection(short_sock):
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        adopt(plane, router_id=99, priority=9, epoch=1)
        assert plane.election.leader_id == 99

        # 心跳在 heartbeat_timeout 内 → 一直跟随，不称主
        for _ in range(4):
            clock.advance(2.0)
            plane._on_heartbeat({"type": "heartbeat", "router_id": 99,
                                 "leader_epoch": 1, "downlink_sequence": 1})
            plane.tick()
            assert plane.election.is_leader is False

        # 超过 heartbeat_timeout → leader_lost，随后单节点可以自己称主
        clock.advance(10.0)
        plane.tick()
        assert plane.election.leader_id is None
        clock.advance(0.1)
        plane.tick()
        clock.advance(0.1)
        plane.tick()
        assert plane.election.is_leader is True
        assert plane.status()["counters"]["leader_changes"] >= 1
    finally:
        plane.stop()


def test_leader_change_sets_lease_grace(short_sock):
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        lease_status = plane.lease.status()
        # 成为 Leader 时调用了 on_leader_change()：宽限期生效
        assert lease_status["grace_remaining"] > 0
        assert lease_status["grace_period"] == 30.0
    finally:
        plane.stop()


def test_becoming_leader_resends_existing_view_entries(short_sock):
    """切主后要把自己视图里的条目重新灌进下行队列（否则新 Leader 静默）。"""
    plane, _transport, clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=WAITING))
    try:
        plane.start()
        # 先在非 Leader 状态装两条（走聚合器，不进下行队列）
        plane.election._phase = "waiting"
        plane.election._waiting_until = clock.now + 40.0
        plane.report_entry(entry("T-1", state="steady"))
        plane.report_entry(entry("T-2", state="steady"))
        assert len(plane.downlink_queue) == 0

        plane.election._waiting_until = 0.0
        become_leader(plane, clock)
        assert len(plane.downlink_queue) == 2
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 5. 下行入站 / 安装 / 通知
# --------------------------------------------------------------------------- #

def test_downlink_entry_is_installed_and_counted(short_sock):
    plane, transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        transport.feed_messages(
            entry("T-r", version=5, incarnation=3, state="steady"))
        installed = plane.view.get("p", "T-r")
        assert installed is not None
        assert installed.version == 5
        assert plane.status()["counters"]["downlink_entries"] == 1
        assert plane.status()["counters"]["installed"] == 1
    finally:
        plane.stop()


def test_stale_downlink_entry_is_not_installed(short_sock):
    """I5：旧版本不安装。"""
    plane, transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        plane.view.install(entry("T-r", version=9, incarnation=3))
        transport.feed_messages(entry("T-r", version=4, incarnation=3))
        assert plane.view.get("p", "T-r").version == 9
        assert plane.status()["counters"]["stale"] == 1
    finally:
        plane.stop()


def test_downlink_entry_is_not_echoed_back_to_downlink_queue(short_sock):
    """下行安装不能再进下行队列，否则组播会自激放大。"""
    plane, transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        plane.downlink_queue.drain()          # 清掉切主时灌入的
        transport.feed_messages(entry("T-r", version=2, incarnation=3))
        assert len(plane.downlink_queue) == 0
    finally:
        plane.stop()


def test_install_notification_goes_to_entry_owner(short_sock):
    plane, transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        sent: list[tuple[str, dict]] = []
        plane.worker_server.notify = (  # type: ignore[assignment]
            lambda agent, msg: sent.append((agent, msg)) or 1)

        transport.feed_messages(entry("T-r", owner="remote-agent", version=2))
        assert sent and sent[0][0] == "remote-agent"
        assert sent[0][1]["type"] == "install"
        assert sent[0][1]["entry"]["tid"] == "T-r"
        assert plane.status()["counters"]["notify"] == 1
    finally:
        plane.stop()


def test_ttl_violation_packet_is_dropped(short_sock):
    """I4：TTL≠1 丢弃，编排器照样不安装。"""
    plane, transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        transport.feed_messages(entry("T-x"), ttl=3)
        assert plane.view.get("p", "T-x") is None
        assert plane.status()["downlink_rx"]["ttl_violations"] == 1
    finally:
        plane.stop()


def test_expiring_hint_is_not_installed_and_notifies_owner(short_sock):
    """D4：expiring 只是提示，不进视图。"""
    plane, transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        notified: list[tuple[str, dict]] = []
        plane.worker_server.notify = (  # type: ignore[assignment]
            lambda agent, msg: notified.append((agent, msg)) or 1)
        plane._local_agents = lambda: {"alice"}

        transport.feed(b'{"type":"expiring","project":"p","tid":"T-e",'
                       b'"owner_agent":"alice","incarnation":3,"version":8}\n')

        assert plane.view.get("p", "T-e") is None
        assert plane.status()["counters"]["expiring_received"] == 1
        assert notified and notified[0][0] == "alice"
        assert notified[0][1]["type"] == "expiring"
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 6. DNA
# --------------------------------------------------------------------------- #

def test_quiet_steady_entry_becomes_dna_and_leaves_downlink_queue(short_sock):
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        plane.report_entry(entry("T-q", state="steady"))
        assert len(plane.downlink_queue) == 1

        # 3 个周期 × 2s 之后仍然没有变化 → 置 DNA
        for _ in range(3):
            clock.advance(2.1)
            plane.tick()
        installed = plane.view.get("p", "T-q")
        assert installed.dna is True
        assert plane.status()["counters"]["dna_marked"] == 1
        # DNA 不进下行队列（D6），但仍留在视图里可被拉取
        assert len(plane.downlink_queue) == 0
        assert plane.view.get("p", "T-q") is not None
    finally:
        plane.stop()


def test_version_change_resets_dna(short_sock):
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        plane.report_entry(entry("T-q", state="steady"))
        for _ in range(3):
            clock.advance(2.1)
            plane.tick()
        assert plane.view.get("p", "T-q").dna is True

        plane.report_entry(entry("T-q", state="steady"))   # 版本推进
        assert plane.view.get("p", "T-q").dna is False
    finally:
        plane.stop()


def test_non_steady_entry_never_gets_dna(short_sock):
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        plane.report_entry(entry("T-run", state="running"))
        for _ in range(4):
            clock.advance(2.1)
            plane.tick()
        assert plane.view.get("p", "T-run").dna is False
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 7. 水位缺口拉取
# --------------------------------------------------------------------------- #

def feed_heartbeat(plane: ControlPlane, router_id: int = 77,
                   epoch: int = 1) -> None:
    """喂一条 Leader 心跳（邻居 6s 过期，跨周期的用例要按时喂）。"""
    plane._on_heartbeat({"type": "heartbeat", "router_id": router_id,
                         "leader_epoch": epoch, "downlink_sequence": 1})


def test_gap_pull_triggers_after_three_barren_intervals(short_sock):
    """母文档 §6.2：连续 3 个周期“收到下行但 0 安装”且本机有 claimed 条目。"""
    plane, transport, clock = make_plane(sock_path=short_sock)
    stub = RecordingPullClient([
        entry("T-gap", version=11, incarnation=3, state="running")])
    try:
        # 前置安装放在 start() 之前：缺口统计只看“周期增量”，启动时
        # 会把已有安装计入基线，否则第一轮会被误判为“有安装”。
        plane.report_entry(entry("T-local", state="claimed"))
        plane.view.install(entry("T-local", version=99, incarnation=4))
        plane.start()
        adopt(plane)
        plane.pull_client = stub

        # 每个周期都收到一条“旧”下行 → 收到但 0 安装
        stale_line = entry("T-local", version=1, incarnation=3).to_line()
        for _ in range(3):
            clock.advance(2.1)
            feed_heartbeat(plane)            # 保住 Leader（邻居 6s 过期）
            transport.feed_messages(stale_line)
            plane.tick()
            # 前两轮还没到阈值，第三轮满足 gap_intervals 才触发
            if plane.status()["gap"]["pulls"]:
                break

        # 拉取在独立守护线程池里跑：等它真正调用到 client
        assert wait_for(lambda: bool(stub.calls)), "3 个无安装周期后应触发缺口拉取"
        host, port, request = stub.calls[0]
        assert host == "10.0.0.2"
        assert port == 8789                     # Leader HTTP 端口 + 1
        assert request["mode"] == "incremental"
        assert request["project"] == "p"
        assert "watermarks" in request
        assert plane.status()["gap"]["pulls"] == 1
    finally:
        plane.stop()


def test_gap_pull_does_not_trigger_without_local_transient(short_sock):
    plane, transport, clock = make_plane(sock_path=short_sock)
    stub = RecordingPullClient()
    try:
        # 只有 steady 条目：不满足“本机有 claimed/releasing”的触发条件
        plane.view.install(entry("T-local", version=99, incarnation=4,
                                 state="steady"))
        plane.start()
        adopt(plane)
        plane.pull_client = stub
        for _ in range(5):
            clock.advance(2.1)
            feed_heartbeat(plane)
            transport.feed_messages(
                entry("T-local", version=1, incarnation=3).to_line())
            plane.tick()
        assert stub.calls == []
    finally:
        plane.stop()


def test_gap_pull_resets_streak_when_install_happens(short_sock):
    plane, transport, clock = make_plane(sock_path=short_sock)
    stub = RecordingPullClient()
    try:
        plane.report_entry(entry("T-local", state="claimed"))
        plane.start()
        adopt(plane)
        plane.pull_client = stub

        # 两次无安装（streak 1→2），第三次安装成功 → streak 归零，不触发拉取
        for _ in range(2):
            clock.advance(2.1)
            feed_heartbeat(plane)
            transport.feed_messages(
                entry("T-local", version=1, incarnation=3).to_line())
            plane.tick()
        assert plane.status()["gap"]["streak"] == 2

        clock.advance(2.1)
        feed_heartbeat(plane)
        transport.feed_messages(entry("T-fresh", version=5, incarnation=3,
                                      state="running").to_line())
        plane.tick()

        assert stub.calls == []
        assert plane.status()["gap"]["streak"] == 0
    finally:
        plane.stop()


def test_gap_pull_installs_fetched_entries(short_sock):
    plane, transport, clock = make_plane(sock_path=short_sock)
    stub = RecordingPullClient([
        entry("T-gap", version=11, incarnation=3, state="running")])
    try:
        plane.report_entry(entry("T-local", state="claimed"))
        plane.view.install(entry("T-local", version=99, incarnation=4))
        plane.start()
        adopt(plane)
        plane.pull_client = stub

        for _ in range(3):
            clock.advance(2.1)
            feed_heartbeat(plane)
            transport.feed_messages(
                entry("T-local", version=1, incarnation=3).to_line())
            plane.tick()

        assert wait_for(lambda: plane.view.get("p", "T-gap") is not None)
        installed = plane.view.get("p", "T-gap")
        assert installed.version == 11
        # 缺口拉取结果不触发下行（与 G5 同族）
        assert len(plane.downlink_queue) == 0
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 8. 人工拉取（I7 / D15 / G5）
# --------------------------------------------------------------------------- #

def test_request_pull_returns_id_and_records_state(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        request_id = plane.request_pull("p", "incremental")
        record = plane.pull_result(request_id)
        assert record is not None
        assert record["request_id"] == request_id
        assert record["project"] == "p"
        assert record["mode"] == "incremental"
        # 还没跑：queued（后台消费可能很快，允许 running/error）
        assert record["state"] in ("queued", "running", "error")
        assert plane.pull_result("nope") is None
    finally:
        plane.stop()


def test_request_pull_validates_arguments_without_raising(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        empty = plane.request_pull("", "incremental")
        assert plane.pull_result(empty)["state"] == PULL_ERROR

        bad_mode = plane.request_pull("p", "sideways")
        assert plane.pull_result(bad_mode)["state"] == PULL_ERROR
        assert "未知拉取模式" in plane.pull_result(bad_mode)["error"]
    finally:
        plane.stop()


def test_request_pull_rejects_when_queue_full(short_sock):
    """队列容量 = pull_queue_capacity：满了给 rejected，不阻塞调用方。"""
    plane, _transport, _clock = make_plane(
        sock_path=short_sock, pull_queue_capacity=1)
    try:
        plane.start()
        # 没有消费者（start_threads=False）→ 队列会真的满
        first = plane.request_pull("p", "incremental")
        second = plane.request_pull("p", "incremental")
        assert plane.pull_result(first)["state"] == "queued"
        assert plane.pull_result(second)["state"] == PULL_REJECTED
        assert plane.status()["pull"]["rejected"] == 1
    finally:
        plane.stop()


def test_pull_runs_and_merges_results(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    stub = RecordingPullClient([
        entry("T-a", version=3, incarnation=3, state="running"),
        entry("T-b", version=4, incarnation=3, state="steady"),
    ])
    try:
        plane.start()
        adopt(plane)
        plane.pull_client = stub
        request_id = plane.request_pull("p", "full")
        plane._run_pull(request_id)

        record = plane.pull_result(request_id)
        assert record["state"] == PULL_DONE
        assert record["fetched"] == 2
        assert record["installed"] == 2
        assert plane.view.get("p", "T-a").state == "running"
        assert plane.view.get("p", "T-b").state == "steady"
        assert stub.calls[0][2]["mode"] == "full"
    finally:
        plane.stop()


def test_manual_pull_never_triggers_downlink(short_sock):
    """G5：人工拉取合并状态，但不触发下行。

    这里用“远端 Leader”身份走 TCP 路径（注入假 client），确认拉回来的
    条目**只合并进视图**，一条都不进下行队列。
    """
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        adopt(plane)                    # 远端 Leader：拉取走 TCP 路径
        plane.view.install(entry("T-a", version=2, incarnation=3,
                                 state="claimed"))
        stub = RecordingPullClient([
            entry("T-a", version=3, incarnation=3, state="claimed"),
            entry("T-b", version=4, incarnation=3, state="steady"),
        ])
        plane.pull_client = stub

        request_id = plane.request_pull("p", "full")
        plane._run_pull(request_id)

        record = plane.pull_result(request_id)
        assert record["state"] == PULL_DONE
        assert record["installed"] == 2
        assert plane.view.get("p", "T-a").version == 3
        assert plane.view.get("p", "T-b") is not None
        # G5：拉取结果一条都不进下行队列
        assert len(plane.downlink_queue) == 0
    finally:
        plane.stop()


def test_leader_manual_pull_uses_local_pull_service(short_sock):
    """本机是 Leader：人工拉取直接问自己的 PullService，不绕一次 TCP 自连。"""
    plane, _transport, clock = make_plane(sock_path=short_sock)
    stub = RecordingPullClient()          # 若走了 TCP 路径会被记录下来
    try:
        plane.start()
        plane.view.install(entry("T-a", version=3, incarnation=3,
                                 state="steady"))
        become_leader(plane, clock)
        plane.pull_client = stub
        plane.downlink_queue.drain()

        request_id = plane.request_pull("p", "full")
        plane._run_pull(request_id)

        record = plane.pull_result(request_id)
        assert record["state"] == PULL_DONE
        assert record["fetched"] == 1        # 自己的视图里那一条
        assert stub.calls == []              # 没有走网络
        assert len(plane.downlink_queue) == 0
    finally:
        plane.stop()


def test_manual_pull_records_pull_retry(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    stub = RecordingPullClient(error=PullRetry(250))
    try:
        plane.start()
        adopt(plane)
        plane.pull_client = stub
        request_id = plane.request_pull("p", "full")
        plane._run_pull(request_id)

        record = plane.pull_result(request_id)
        assert record["state"] == PULL_ERROR
        assert record["retry_after_ms"] == 250
        assert "配额" in record["error"]
        assert plane.status()["counters"]["pull_failed"] == 1
    finally:
        plane.stop()


def test_manual_pull_without_leader_endpoint_reports_error(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        # 没有 Leader → 没有拉取端点
        request_id = plane.request_pull("p", "incremental")
        plane._run_pull(request_id)
        record = plane.pull_result(request_id)
        assert record["state"] == PULL_ERROR
        assert "端点" in record["error"]
    finally:
        plane.stop()


def test_pull_queue_is_independent_of_downlink_and_election(short_sock):
    """I7：拉取有独立队列与线程池，不占下行定时器。"""
    plane, _transport, _clock = make_plane(
        sock_path=short_sock, pull_queue_capacity=2)
    try:
        plane.start()
        assert plane._pull_queue.maxsize == 2
        assert plane.status()["pull"]["queued"] == 0
        plane.request_pull("p", "incremental")
        # 人工拉取不影响下行队列
        assert len(plane.downlink_queue) == 0
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 9. 上行 / Worker socket
# --------------------------------------------------------------------------- #

def test_uplink_entries_are_installed_and_redistributed_by_leader(short_sock):
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        plane.downlink_queue.drain()

        plane._on_uplink_entries([entry("T-up", version=6, incarnation=2,
                                        state="steady", owner="bob")])
        assert plane.view.get("p", "T-up").version == 6
        # Leader 收上行后要向外扩散
        assert len(plane.downlink_queue) == 1
        assert plane.status()["counters"]["uplink_entries"] == 1
    finally:
        plane.stop()


def test_follower_does_not_redistribute_uplink_entries(short_sock):
    plane, _transport, _clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=40.0))
    try:
        plane.start()
        assert not plane.election.is_leader
        plane._on_uplink_entries([entry("T-up", version=6, incarnation=2)])
        assert plane.view.get("p", "T-up") is not None
        assert len(plane.downlink_queue) == 0
    finally:
        plane.stop()


def test_worker_socket_entry_is_routed_like_a_local_report(short_sock):
    """Worker socket 上报按本地条目处理（盖号 + 路由）。"""
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        plane._on_worker_entry(entry("T-w", version=0, incarnation=0,
                                     owner=""), agent="alice")
        installed = plane.view.get("p", "T-w")
        assert installed is not None
        assert installed.owner_agent == "alice"
        assert installed.incarnation == plane.status()["incarnation"]
        assert installed.version == 1
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 10. 租约
# --------------------------------------------------------------------------- #

def test_leader_sends_expiring_hint_after_ttl(short_sock):
    plane, transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        # 远端 agent 的 claimed 条目建租约（本地条目会先走续约）
        plane._on_uplink_entries([entry("T-lease", version=2, incarnation=3,
                                        state="claimed", owner="bob")])
        before = len(transport.multicast)

        clock.advance(95.0)          # > lease_ttl(90) 且 > grace_period(30)
        plane.tick()

        assert plane.status()["counters"]["expiring_sent"] >= 1
        hints = [m for m in transport.multicast_lines()
                 if m.get("type") == "expiring"]
        assert hints, "超过 lease_ttl 应下发 expiring 提示"
        assert hints[0]["tid"] == "T-lease"
        assert hints[0]["version"] == 2      # D4：版本不变
        # 提示不安装进视图
        assert plane.view.get("p", "T-lease").state == "claimed"
        assert len(transport.multicast) > before
    finally:
        plane.stop()


def test_follower_reports_renewals_uplink(short_sock):
    """本地租约到点 → 续约副本走上行（非 Leader 不直接下行）。"""
    plane, _transport, clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=40.0))
    try:
        plane.start()
        plane.report_entry(entry("T-loc", state="claimed"))
        # 续约间隔 = lease_ttl/3 = 30s
        clock.advance(31.0)
        plane.tick()
        assert plane.lease.status()["renewals_due"] == 0, "续约后应清零"
        assert len(plane.downlink_queue) == 0
        # 续约副本已交给聚合器，窗口过后进上行队列
        clock.advance(1.0)
        plane.tick()
        assert plane.uplink_queue.qsize() >= 1
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 11. 线程 / status / 性能
# --------------------------------------------------------------------------- #

def test_started_plane_runs_named_daemon_threads(short_sock):
    plane, _transport, _clock = make_plane(
        sock_path=short_sock, start_threads=True)
    try:
        plane.start()
        names = set(plane.status()["threads"])
        assert {"control-election", "control-downlink-tx",
                "control-lease", "control-pull"} <= names
        alive = {t.name for t in threading.enumerate() if t.daemon}
        assert names <= alive
    finally:
        plane.stop()
    leftover = {t.name for t in threading.enumerate()}
    assert "control-election" not in leftover
    assert "control-lease" not in leftover


def test_status_exposes_adapter_contract_fields(short_sock):
    plane, _transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        status = plane.status()
        # 契约 §3.10：adapter 要往 /api/cluster/status 里塞这些
        for key in ("multicast_mode", "control_leader", "control_epoch",
                    "downlink_queue", "uplink_queue", "counters",
                    "pull", "gap", "errors", "incarnation"):
            assert key in status, key
        assert status["enabled"] is True
        assert status["multicast_mode"] == "multicast"
        assert status["counters"]["reported"] == 0
        # 切主后 control_leader 变成自己
        become_leader(plane, clock)
        assert plane.status()["control_leader"] == plane.status()["router_id"]
    finally:
        plane.stop()


def test_status_role_follows_election_state(short_sock):
    """卡面要求 ``status()`` 带 role：waiting → candidate/follower → leader。"""
    plane, _transport, clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=40.0))
    try:
        plane.start()
        # Waiting 期间没有 Leader
        assert plane.status()["role"] == "waiting"

        # 承认远端 Leader → follower，并且 leader_id / epoch 并列可读
        adopt(plane, router_id=88, priority=9, epoch=5)
        clock.advance(0.1)
        plane.tick()
        status = plane.status()
        assert status["role"] == "follower"
        assert status["leader_id"] == 88
        assert status["epoch"] == 5
        assert status["is_leader"] is False

        # Leader 心跳失效 → 自己称主 → leader
        clock.advance(10.0)
        plane.tick()
        clock.advance(0.1)
        plane.tick()          # declare
        clock.advance(0.1)
        plane.tick()          # become_leader
        assert plane.election.is_leader is True
        assert plane.status()["role"] == "leader"
    finally:
        plane.stop()


def test_leader_change_stops_uplink_and_clears_target(short_sock):
    """卡面：切主时停上行、清 target；下一个 Leader 出现后重定向且不丢未发。"""
    plane, _transport, clock = make_plane(
        sock_path=short_sock, config=ControlConfig(waiting_interval=40.0))
    try:
        plane.start()
        # follower：指向远端 Leader，上行线程在跑
        adopt(plane, router_id=88, priority=9, epoch=1)
        clock.advance(0.1)
        plane.tick()
        assert plane.status()["role"] == "follower"
        assert plane.uplink_sender.status()["target"] == ["10.0.0.2", 8788]
        assert plane.status()["uplink"]["running"] is True

        # 先放一条待发条目，切主后它不能丢
        plane.report_entry(entry("T-keep", state="steady"))
        clock.advance(1.0)
        plane.tick()
        assert plane.uplink_queue.qsize() == 1

        # 本机成为 Leader：停上行 + 清目标
        plane.election._waiting_until = 0.0
        adopt(plane, router_id=88, priority=9, epoch=1)
        clock.advance(10.0)                    # 心跳超时 → leader_lost
        plane.tick()
        clock.advance(0.1)
        plane.tick()                           # declare
        clock.advance(0.1)
        plane.tick()                           # become_leader
        assert plane.election.is_leader is True
        uplink = plane.uplink_sender.status()
        assert uplink["target"] is None, "成为 Leader 后应清掉上行目标"
        assert uplink["running"] is False, "成为 Leader 后应停掉上行线程"
        # 未发条目还在（在队列或发送器待发缓冲里，没有凭空消失）
        assert uplink["queued"] + uplink["pending"] >= 1
    finally:
        plane.stop()


def test_grace_period_suppresses_expiring_right_after_leader_change(short_sock):
    """卡面：切主宽限内不产生 expiring。"""
    plane, transport, clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        become_leader(plane, clock)
        # 一条远端租约，先让它正常建立
        plane._on_uplink_entries([entry("T-g", version=2, incarnation=3,
                                        state="claimed", owner="bob")])
        # 把租约时间推过期，但宽限期内（grace_period=30）不该发 expiring
        clock.advance(95.0)
        plane.lease.on_leader_change()        # 刚切主：宽限 30s 内压制
        plane.tick()
        assert plane.status()["counters"]["expiring_sent"] == 0
        hints = [m for m in transport.multicast_lines()
                 if m.get("type") == "expiring"]
        assert hints == []
        # 宽限期过后才允许提示
        clock.advance(31.0)
        plane.tick()
        assert plane.status()["counters"]["expiring_sent"] >= 1
    finally:
        plane.stop()


def test_tick_requires_start(short_sock):
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    plane.tick()            # 不抛
    assert plane.status()["started"] is False
    plane.stop()


def test_report_entry_is_fast(short_sock):
    """I6 / G1：Worker 上报路径只做内存操作，<1ms/次（宽松上界）。"""
    plane, _transport, _clock = make_plane(sock_path=short_sock)
    try:
        plane.start()
        count = 500
        started = time.monotonic()
        for i in range(count):
            plane.report_entry(entry(f"T-{i}", state="steady"))
        elapsed = time.monotonic() - started
        assert elapsed / count < 0.001, f"平均 {elapsed / count * 1000:.3f}ms/次"
    finally:
        plane.stop()


# --------------------------------------------------------------------------- #
# 12. 真端口装配（跨两个控制面实例的上下行）
# --------------------------------------------------------------------------- #

def test_real_tcp_uplink_and_pull_between_two_planes(short_sock):
    """两个控制面实例走真 TCP：子节点上行 → Leader 安装；子节点拉取 → 收到。

    这是装配的端到端确认：``ControlTcpServer`` + ``PullService`` +
    ``PullClient`` 被 plane 正确接线（组播仍是假 transport，不碰真网络）。
    """
    clock = Clock()
    # Leader 绑一个临时端口（listen_port=0 → 系统分配）
    leader, leader_t, _ = make_plane(
        sock_path=short_sock + ".leader", clock=clock, node="leader",
        address="127.0.0.1")
    follower, _follower_t, _ = make_plane(
        sock_path=short_sock + ".follower", clock=clock, node="follower",
        address="127.0.0.1")
    try:
        leader.start()
        follower.start()
        control_port = leader.status()["control_port"]
        assert control_port > 0

        # 子节点承认 Leader（address=127.0.0.1，port 用 Leader 的真实控制端口，
        # 让 ``port + offset`` 正好落在监听口上）
        follower.election.observe({
            "type": "hello", "router_id": 4242, "node": "leader",
            "address": "127.0.0.1",
            "port": control_port - int(follower.limits.uplink_port_offset),
            "priority": 9, "leader_declaration": True, "leader_epoch": 1,
        })
        clock.advance(0.1)
        follower.tick()
        assert follower.election.leader_id == 4242

        # 上行：子节点把自己的条目发出去，Leader 应安装
        follower.report_entry(entry("T-e2e", state="steady", owner="bob"))
        clock.advance(1.0)
        follower.tick()                      # 聚合 → 上行队列
        assert follower.uplink_queue.qsize() == 1
        assert follower.uplink_sender.send_once(), "上行 TCP 发送应成功"

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and leader.view.get("p", "T-e2e") is None:
            time.sleep(0.01)
        installed = leader.view.get("p", "T-e2e")
        assert installed is not None, "Leader 没收到子节点上行"
        assert installed.state == "steady"

        # 拉取：子节点经真 TCP 从 Leader 增量拉取
        got = follower.pull_client.fetch(
            "127.0.0.1", control_port,
            {"type": "pull", "project": "p", "mode": "full", "from": None})
        assert [e.tid for e in got] == ["T-e2e"]
    finally:
        follower.stop()
        leader.stop()
