"""T-0312：控制面端到端不变量 —— I3/I4/I5、G1/G3/G5、切主、拉取不下行。

契约 §7：注入时钟 + 假 transport + 临时端口，不依赖真实组播 / 网卡 / 外网，
不 ``sleep`` 长于 2s。本文件用一条**内存总线 ``Bus``** 把 3 个真实的
``ControlPlane`` 连起来，只把「会碰网卡的那一层」换成内存实现：

* 组播 / Hello 单播  → ``BusTransport``（实现 ``Transport``，记录每次调用的
  kind / 字节 / TTL；广播给所有其他 plane，单播按地址定点投递）；
* 上行 TCP           → ``BusUplinkSender``（同总线投递到 Leader 的
  ``_on_uplink_entries``，切主期间保留未发条目）；
* 拉取 TCP           → ``BusPullClient``（同总线打到 Leader 的
  ``PullService.handle``，full 分页照真实协议收全）；
* Worker socket      → ``MemoryWorkerServer``（只记通知，不开 socket）。

选举、视图单调、租约、聚合窗口、下行打包、TTL 校验、分页、缺口检测全部是
产品代码。所有时序由注入的 ``Clock`` 推进（``tick()``），单段真实 sleep
不超过 0.05s，整套用例远小于 30s。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from hive_control.config import ControlConfig, Limits
from hive_control.election import BandwidthProbe
from hive_control.model import StateEntry
from hive_control.plane import PULL_DONE, ControlPlane
from hive_control.pull import PullRetry

# 只累计**本文件**各用例（含夹具 setup/teardown）的耗时，不用「模块导入到
# 最后一条用例」的墙钟时间 —— 那样在别人跑全量 pytest 会话时会假红。
_ELAPSED = 0.0

MULTICAST_PORT = 47899
ADDRESSES = ("10.0.0.11", "10.0.0.12", "10.0.0.13")
PRIORITIES = (1, 2, 3)          # 排名键 (priority, factor, router_id) 大者胜 → 3 当选
LISTEN_PORTS = (18801, 18802, 18803)   # 通告端口有效，但地址不是本机 → 不产生真实监听
LOCAL_AGENTS = {"w1", "w2", "w3"}
STEP = 0.06


# --------------------------------------------------------------------------- #
# 注入时钟 / incarnation
# --------------------------------------------------------------------------- #

class Clock:
    """可控时钟：``advance()`` 推进，``__call__`` 供所有子系统注入。"""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += float(delta)


class FixedIncarnation:
    """不写盘的 incarnation 存储：保证确定性，不污染全局计数文件。"""

    def __init__(self, value: int):
        self.value = int(value)

    def next(self) -> int:
        self.value += 1
        return self.value

    def current(self) -> int:
        return self.value


def make_entry(
    tid: str,
    *,
    project: str = "p",
    version: int = 1,
    incarnation: int = 50,
    state: str = "claimed",
    owner: str = "w1",
    dna: bool = False,
    urgent: bool = False,
) -> StateEntry:
    return StateEntry(
        project=project, tid=tid, version=version, incarnation=incarnation,
        owner_agent=owner, state=state, dna=dna, updated_at=0, urgent=urgent)


def line_of(entry: StateEntry) -> bytes:
    """一条 JSON 行报文（与 ``pack`` / ``wire.encode_line`` 同格式）。"""
    return entry.to_line().encode("utf-8") + b"\n"


# --------------------------------------------------------------------------- #
# 内存总线：把 3 个 ControlPlane 连起来
# --------------------------------------------------------------------------- #

class Bus:
    """共享内存总线。``outbound_ttl`` 可注入，用来伪造链路上的 TTL 篡改。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._members: dict[str, BusTransport] = {}
        self.records: list[dict] = []
        self.outbound_ttl = 1

    def attach(self, transport: "BusTransport") -> None:
        with self._lock:
            self._members[transport.address] = transport

    def member(self, address: str) -> "BusTransport | None":
        with self._lock:
            return self._members.get(str(address))

    def record(self, entry: dict) -> None:
        with self._lock:
            self.records.append(entry)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.records)

    def multicast_calls(self) -> list[dict]:
        return [r for r in self.snapshot() if r["kind"] == "multicast"]

    def broadcast(self, sender: "BusTransport", data: bytes, ttl: int) -> int:
        """组播：发给**所有其他**已启动的 plane（不回环给自己）。"""
        with self._lock:
            targets = [t for t in self._members.values()
                       if t is not sender and t.active and t.callback is not None]
        for target in targets:
            target.deliver(data, (sender.address, sender.port), ttl)
        return len(targets)

    def unicast(self, sender: "BusTransport", addr, data: bytes, ttl: int) -> bool:
        """单播：按目标 host 定点投递（Hello / 回退下行都走这里）。"""
        host = addr[0] if isinstance(addr, (tuple, list)) else str(addr)
        target = self.member(host)
        if target is None or not target.active or target.callback is None:
            return False
        return target.deliver(data, (sender.address, sender.port), ttl)

    def inject(self, address: str, data: bytes, *, ttl: int = 1,
               source: str = "10.0.0.99") -> bool:
        """测试注入：绕过发送方，直接把一包以指定 TTL 喂给某个 plane。"""
        target = self.member(address)
        if target is None:
            return False
        return target.deliver(bytes(data), (source, MULTICAST_PORT), int(ttl))


class BusTransport:
    """``Transport`` 的内存实现：记录每次调用的 kind / 字节 / TTL。"""

    multicast_mode = "multicast"   # 让包装层透传时也能拿到确定值

    def __init__(self, bus: Bus, address: str, port: int = MULTICAST_PORT):
        self.bus = bus
        self.address = address
        self.port = port
        self.callback = None
        self.active = True
        self.starts = 0
        self.stops = 0
        self.calls: list[dict] = []
        self.plane: ControlPlane | None = None
        bus.attach(self)

    # -- Transport 协议 ------------------------------------------------ #
    def send_multicast(self, data: bytes) -> None:
        payload = bytes(data)
        self._record("multicast", None, payload)
        self.bus.broadcast(self, payload, self.bus.outbound_ttl)

    def send_unicast(self, addr, data: bytes) -> None:
        payload = bytes(data)
        self._record("unicast", addr, payload)
        self.bus.unicast(self, addr, payload, self.bus.outbound_ttl)

    def start_receiver(self, callback) -> None:
        self.callback = callback
        self.starts += 1
        self.active = True

    def stop(self) -> None:
        self.stops += 1
        self.active = False

    # -- 接收 / 统计 --------------------------------------------------- #
    def deliver(self, data: bytes, addr, ttl: int) -> bool:
        callback = self.callback
        if callback is None or not self.active:
            return False
        callback(data, addr, ttl)
        return True

    @property
    def multicast_calls(self) -> int:
        return sum(1 for c in self.calls if c["kind"] == "multicast")

    @property
    def unicast_calls(self) -> int:
        return sum(1 for c in self.calls if c["kind"] == "unicast")

    def _record(self, kind: str, addr, payload: bytes) -> None:
        record = {
            "from": self.address,
            "kind": kind,
            "addr": tuple(addr) if isinstance(addr, (tuple, list)) else addr,
            "bytes": payload,
            "nbytes": len(payload),
            "ttl": self.bus.outbound_ttl,
        }
        with self.bus._lock:
            self.calls.append(record)
            self.bus.records.append(record)


class MemoryWorkerServer:
    """不开 socket 的 Worker 服务端：只把通知记下来供断言。"""

    def __init__(self):
        self.notifications: list[tuple[str, dict]] = []
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def notify(self, owner_agent: str, message: dict) -> int:
        self.notifications.append((str(owner_agent), dict(message)))
        return 1

    def status(self) -> dict:
        return {"notifications": len(self.notifications), "started": self.starts > 0}


class MemoryTcpServer:
    """不开监听的 TCP 服务端：上行与拉取改由总线替身承担。"""

    def __init__(self, port: int = 0):
        self.port = int(port)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def status(self) -> dict:
        return {"connections": 0, "port": self.port}


class BusUplinkSender:
    """上行发送器的内存替身：从 ``uplink_queue`` 取批次，经总线交给 Leader。

    ``pending`` 是「已取走但还没发出去」的条目：切主时 ``clear_target()``
    只清目标，不清缓冲，所以未发上行不会丢（G1/切主判据）。
    """

    def __init__(self, plane: ControlPlane, bus: Bus):
        self.plane = plane
        self.bus = bus
        self.queue = plane.uplink_queue
        self.target: tuple[str, int] | None = None
        self.pending: list[StateEntry] = []
        self.sent_batches: list[list[StateEntry]] = []
        self.running = False

    def set_target(self, host: str, port: int) -> None:
        self.target = (str(host), int(port))

    def clear_target(self) -> None:
        self.target = None

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def status(self) -> dict:
        return {
            "target": list(self.target) if self.target else None,
            "pending": len(self.pending),
            "sent": sum(len(b) for b in self.sent_batches),
            "running": self.running,
        }

    def send_once(self) -> bool:
        while True:
            item = self.queue.get(timeout=0)
            if item is None:
                break
            self.pending.append(item)
        if not self.pending or self.target is None:
            return False
        leader = self.bus.member(self.target[0])
        if leader is None or leader.plane is None or not leader.active:
            return False        # Leader 不可达：pending 原样保留
        batch = list(self.pending)
        leader.plane._on_uplink_entries(batch)
        self.pending.clear()
        self.sent_batches.append(batch)
        return True


class BusPullClient:
    """拉取客户端的内存替身：把请求打到 Leader 的 ``PullService``。

    真实解析 ``page`` / ``entry`` / ``retry`` 行，所以 full 分页是产品协议，
    不是硬编码的假数据。
    """

    def __init__(self, bus: Bus):
        self.bus = bus
        self.calls: list[tuple[str, int, dict]] = []
        self.pages = 0

    def fetch(self, host: str, port: int, request: dict) -> list[StateEntry]:
        req = dict(request)
        self.calls.append((str(host), int(port), req))
        leader = self.bus.member(host)
        if leader is None or leader.plane is None:
            return []
        rows = leader.plane.pull_service.handle(req)
        if rows and rows[0].get("type") == "retry":
            raise PullRetry(int(rows[0].get("after_ms") or 1))
        entries: list[StateEntry] = []
        for row in rows:
            kind = row.get("type")
            if kind == "page":
                self.pages += 1
            elif kind == "entry":
                body = row.get("entry")
                if isinstance(body, dict):
                    entries.append(StateEntry.from_dict(body))
        return entries


# --------------------------------------------------------------------------- #
# 三节点集群夹具
# --------------------------------------------------------------------------- #

class Cluster:
    def __init__(self, nodes: list[SimpleNamespace], bus: Bus, clock: Clock):
        self.nodes = nodes
        self.bus = bus
        self.clock = clock

    @property
    def planes(self) -> list[ControlPlane]:
        return [n.plane for n in self.nodes]

    def advance(self, step: float = STEP, rounds: int = 1,
                flush: bool = True) -> None:
        for _ in range(rounds):
            self.clock.advance(step)
            for plane in self.planes:
                plane.tick()        # stop() 过的 plane 内部会直接 return
            if flush:
                self.flush_uplinks()

    def flush_uplinks(self) -> None:
        for plane in self.planes:
            plane.uplink_sender.send_once()

    def leader(self) -> SimpleNamespace:
        # stop() 掉的旧 Leader 不会自动清 election.is_leader，但它已经不再
        # tick / 不再收发包，必须排除在「当前 Leader」之外。
        leaders = [n for n in self.nodes
                   if n.plane.status()["started"] and n.plane.election.is_leader]
        assert len(leaders) == 1, f"期望唯一 Leader，实际 {len(leaders)} 个"
        return leaders[0]

    def followers(self) -> list[SimpleNamespace]:
        leader = self.leader()
        return [n for n in self.nodes if n is not leader]

    def converge(self, rounds: int = 6, step: float = STEP) -> None:
        """0.05s 级虚拟等待后收敛；随后短暂真实等待让守护线程池稳定。"""
        self.advance(step, rounds)
        time.sleep(0.05)
        self.leader()


def wait_for(pred, timeout: float = 3.0) -> bool:
    """轮询后台线程池；单段 sleep 0.01s，总上限 3s。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


@pytest.fixture(autouse=True)
def _accumulate_duration():
    global _ELAPSED
    start = time.monotonic()
    yield
    _ELAPSED += time.monotonic() - start


@pytest.fixture()
def cluster(tmp_path) -> Cluster:
    clock = Clock()
    bus = Bus()
    nodes: list[SimpleNamespace] = []

    for index, (address, priority, listen_port) in enumerate(
            zip(ADDRESSES, PRIORITIES, LISTEN_PORTS), start=1):
        node = f"n{index}"
        seeds = [{"address": other, "node": f"n{j + 1}"}
                 for j, other in enumerate(ADDRESSES) if other != address]
        limits = Limits(
            socket_path=str(tmp_path / f"{node}.sock"),   # 内存替身，不会真开 socket
            multicast_port=MULTICAST_PORT,
            hello_interval=0.05,
            heartbeat_interval=0.05,
            heartbeat_timeout=0.30,
            peer_timeout=0.25,
            aggregation_window=0.05,
            uplink_queue_capacity=3,
            pull_page_size=2,
            pull_full_per_minute=100,
            pull_incremental_per_sec=100,
            gap_intervals=3,
        )
        config = ControlConfig(
            leader_priority=priority,
            waiting_interval=0.001,
            lease_ttl=5.0,
            grace_period=1.0,
            rxmt_interval=0.1,
            leader_multicast_interval=0.05,
            project_tag="p",
        )
        transport = BusTransport(bus, address)
        plane = ControlPlane(
            node=node,
            address=address,
            listen_port=listen_port,
            config=config,
            limits=limits,
            peer_provider=lambda s=seeds: list(s),
            local_agents=lambda: set(LOCAL_AGENTS),
            transport=transport,
            clock=clock,
            # 注入同速率：排名完全由 priority 决定，不依赖「网卡速率探测不到」
            # 这条平台相关回落路径（§7 跨平台要求）。
            bandwidth_probe=BandwidthProbe(speed_mbps=1000.0),
            start_threads=False,           # 由 tick() 确定性驱动
            incarnation_store=FixedIncarnation(100 + index),
        )
        worker = MemoryWorkerServer()
        sender = BusUplinkSender(plane, bus)
        puller = BusPullClient(bus)
        # 只替换「会碰网卡的那一层」，其余全是产品代码
        plane.worker_server = worker
        plane.tcp_server = MemoryTcpServer(listen_port + 1)
        plane.uplink_sender = sender
        plane.pull_client = puller
        transport.plane = plane
        nodes.append(SimpleNamespace(
            node=node, address=address, priority=priority, plane=plane,
            transport=transport, worker=worker, sender=sender, puller=puller))

    # 先把所有接收回调注册好，再逐个 start()：先启动的节点发 Hello 时
    # 其他节点的接收器已经就位，不会丢首批邻居通告。
    for n in nodes:
        n.plane.downlink_receiver.start()
    for n in nodes:
        n.plane.start()

    world = Cluster(nodes, bus, clock)
    try:
        yield world
    finally:
        for n in nodes:
            n.plane.stop()


# --------------------------------------------------------------------------- #
# 1. 三节点收敛到唯一 Leader（G1/I3 的底座）
# --------------------------------------------------------------------------- #

def test_three_nodes_converge_to_one_leader(cluster):
    cluster.converge()

    leader = cluster.leader()
    assert leader.priority == max(PRIORITIES), "排名最高的节点应当选"

    ids = {n.plane.status()["leader_id"] for n in cluster.nodes}
    assert None not in ids
    assert ids == {leader.plane.status()["router_id"]}, "所有节点 leader_id 必须一致"
    assert leader.plane.status()["role"] == "leader"
    assert all(f.plane.status()["role"] == "follower" for f in cluster.followers())

    # 总线确实传过包，否则后面的 I3/I4 判据没有判别力
    assert cluster.bus.snapshot(), "内存总线上必须真的出现过报文"
    assert cluster.bus.multicast_calls(), "Leader 必须真的发过组播"


# --------------------------------------------------------------------------- #
# 2. I3：非 Leader 不发组播
# --------------------------------------------------------------------------- #

def test_i3_only_leader_ever_multicasts(cluster):
    cluster.converge()
    leader = cluster.leader()

    multicasts = cluster.bus.multicast_calls()
    assert multicasts, "没有组播包 → 判据失去判别力"
    assert {record["from"] for record in multicasts} == {leader.transport.address}
    assert leader.transport.multicast_calls > 0
    for follower in cluster.followers():
        assert follower.transport.multicast_calls == 0
        assert follower.plane.downlink_sender.multicast_mode == "multicast"


# --------------------------------------------------------------------------- #
# 3. Leader 本地 claim → follower 安装并收到 Worker 通知
# --------------------------------------------------------------------------- #

def test_leader_local_claim_reaches_followers_and_notifies_worker(cluster):
    cluster.converge()
    leader = cluster.leader()

    leader.plane.report_entry(make_entry("T-lead", owner="w1", version=3))
    cluster.advance(STEP, 1)

    for follower in cluster.followers():
        got = follower.plane.view.get("p", "T-lead")
        assert got is not None, "follower 必须安装 Leader 下发的条目"
        assert got.owner_agent == "w1"
        installs = [message for agent, message in follower.worker.notifications
                    if message.get("type") == "install"
                    and (message.get("entry") or {}).get("tid") == "T-lead"]
        assert len(installs) == 1, "同一版本只通知一次（I5 也在这条路径上生效）"
        assert installs[0]["entry"]["owner_agent"] == "w1"


# --------------------------------------------------------------------------- #
# 4. follower 上报 claimed → Leader 视图 + 下行队列；旧版本不回传
# --------------------------------------------------------------------------- #

def test_follower_uplink_reaches_leader_and_old_version_never_returns(cluster):
    cluster.converge()
    leader = cluster.leader()
    follower = cluster.followers()[0]
    leader.plane.downlink_queue.drain()

    before_calls = len(follower.transport.calls)
    follower.plane.report_entry(make_entry("T-up", owner="w2", version=4))
    # G1：report() 只在内存里走，调用线程不碰 transport
    assert len(follower.transport.calls) == before_calls

    follower.plane.aggregator.flush(force=True)
    assert follower.plane.uplink_queue.qsize() >= 1
    cluster.flush_uplinks()

    assert leader.plane.view.get("p", "T-up") is not None, "Leader 视图应出现上报条目"
    queued = [e.key for e in leader.plane.downlink_queue.drain()]
    assert ("p", "T-up") in queued, "Leader 收到上行后必须向外扩散"

    current = follower.plane.view.get("p", "T-up")
    stale = make_entry("T-up", owner="w2", version=1, incarnation=1)
    before_queue = follower.plane.uplink_queue.qsize()
    before_calls_2 = len(follower.transport.calls)
    cluster.bus.inject(follower.transport.address, line_of(stale), ttl=1)

    assert follower.plane.view.get("p", "T-up") is current, "旧版本不得覆盖视图"
    assert follower.plane.uplink_queue.qsize() == before_queue, "旧版本不得触发上行回传"
    assert len(follower.transport.calls) == before_calls_2, "旧版本不得触发任何发包"


# --------------------------------------------------------------------------- #
# 5. I4：TTL=2 的包被丢弃并计数（含 TTL=1 的 A/B 对照）
# --------------------------------------------------------------------------- #

def test_i4_ttl_violation_is_dropped_and_counted(cluster):
    cluster.converge()
    leader = cluster.leader()
    follower = cluster.followers()[0]

    entry = make_entry("T-ttl", owner="w1", version=7, incarnation=90)
    before_violations = follower.plane.status()["downlink_rx"]["ttl_violations"]
    before_notify = len(follower.worker.notifications)

    # 端到端注入：让 Leader 发出的包在链路上带 TTL=2
    cluster.bus.outbound_ttl = 2
    try:
        leader.plane.report_entry(entry)
        cluster.advance(STEP, 1)
    finally:
        cluster.bus.outbound_ttl = 1

    after = follower.plane.status()["downlink_rx"]
    assert after["ttl_violations"] > before_violations, "TTL≠1 必须被丢弃并计数"
    assert follower.plane.view.get("p", "T-ttl") is None, "TTL≠1 不得安装进视图"
    assert len(follower.worker.notifications) == before_notify, "TTL≠1 不得通知 Worker"

    # A/B 对照：同一条目以 TTL=1 注入，必须正常安装并通知
    assert cluster.bus.inject(follower.transport.address, line_of(entry), ttl=1)
    assert follower.plane.view.get("p", "T-ttl") is not None
    assert len(follower.worker.notifications) == before_notify + 1


# --------------------------------------------------------------------------- #
# 6. I5：同 (incarnation, version) 不重复通知；旧版本不安装
# --------------------------------------------------------------------------- #

def test_i5_duplicate_and_older_packets_notify_once(cluster):
    cluster.converge()
    follower = cluster.followers()[0]

    fresh = make_entry("T-dup", owner="w1", version=5, incarnation=90)
    duplicate = make_entry("T-dup", owner="w1", version=5, incarnation=90)
    older = make_entry("T-dup", owner="w1", version=4, incarnation=90)
    before = len(follower.worker.notifications)

    assert cluster.bus.inject(follower.transport.address, line_of(fresh), ttl=1)
    assert len(follower.worker.notifications) == before + 1

    assert cluster.bus.inject(follower.transport.address, line_of(duplicate), ttl=1)
    assert cluster.bus.inject(follower.transport.address, line_of(older), ttl=1)

    assert len(follower.worker.notifications) == before + 1, "重复/旧版本不得重复通知"
    assert follower.plane.view.get("p", "T-dup").version == 5
    assert follower.plane.status()["counters"]["stale"] >= 2
    assert follower.plane.status()["counters"]["installed"] >= 1


# --------------------------------------------------------------------------- #
# 7. 切主：心跳超时窗口内产生新 Leader，未发上行不丢
# --------------------------------------------------------------------------- #

def test_failover_elects_new_leader_without_losing_pending_uplink(cluster):
    cluster.converge()
    old_leader = cluster.leader()
    follower = cluster.followers()[0]        # priority 最低，切主后仍是 follower
    old_epoch = old_leader.plane.election.leader_epoch

    # 制造「已聚合但还没发出去」的上行，并模拟 Leader 失联（目标清空）
    follower.plane.report_entry(make_entry("T-pending", owner="w1"))
    follower.plane.aggregator.flush(force=True)
    follower.sender.clear_target()
    assert follower.sender.send_once() is False
    pending_before = len(follower.sender.pending)
    assert pending_before >= 1

    old_leader.plane.stop()

    # 推进过心跳超时窗口（0.30s）：先 leader_lost，再靠 Hello 重建邻居后重新竞选。
    # 这几轮刻意不 flush 上行，用来验证切主期间缓冲不被丢弃。
    for step in (0.35, STEP, STEP, STEP):
        cluster.clock.advance(step)
        for plane in cluster.planes:
            plane.tick()

    new_leader = cluster.leader()
    assert new_leader.address != old_leader.address
    assert new_leader.priority == sorted(PRIORITIES)[-2], "次高优先级者接任"
    assert new_leader.plane.election.leader_epoch > old_epoch, "epoch 必须单调增大"
    ids = {n.plane.election.leader_id for n in cluster.nodes
           if n.plane.status()["started"]}
    assert len(ids) == 1 and None not in ids

    assert len(follower.sender.pending) == pending_before, "切主期间未发上行不得丢"
    cluster.flush_uplinks()
    assert new_leader.plane.view.get("p", "T-pending") is not None, (
        "新 Leader 出现后，缓冲里的上行应继续投递")


# --------------------------------------------------------------------------- #
# 8. G5：人工 full 拉取分页收全，且不产生任何下行事件
# --------------------------------------------------------------------------- #

def test_manual_full_pull_paginates_and_never_emits_downlink(cluster):
    cluster.converge()
    leader = cluster.leader()
    follower = cluster.followers()[0]

    for index in range(5):
        leader.plane.view.install(make_entry(
            f"T-full-{index}", owner="w1", state="steady",
            version=index + 1, incarnation=50))

    before_multicast = len(cluster.bus.multicast_calls())
    before_queue = len(follower.plane.downlink_queue)

    request_id = follower.plane.request_pull("p", "full")
    follower.plane._run_pull(request_id)     # start_threads=False 时手动消费队列
    record = follower.plane.pull_result(request_id)

    assert record["state"] == PULL_DONE
    assert record["fetched"] == 5, "full 必须把 5 条全部收回来"
    assert record["installed"] == 5
    assert follower.puller.pages == 3, "page_size=2 → 5 条应为 3 页"
    for index in range(5):
        assert follower.plane.view.get("p", f"T-full-{index}") is not None
    assert follower.puller.calls[0][2]["mode"] == "full"

    # G5：拉取只合并状态，绝不触发下行
    assert len(cluster.bus.multicast_calls()) == before_multicast
    assert len(follower.plane.downlink_queue) == before_queue
    assert len(leader.plane.downlink_queue) == 0


# --------------------------------------------------------------------------- #
# 9. 水位缺口：手动制造 gap 时产生增量拉取请求
# --------------------------------------------------------------------------- #

def test_gap_produces_incremental_pull_request(cluster):
    cluster.converge()
    leader = cluster.leader()
    follower = cluster.followers()[0]

    # 本机持有 claimed 条目，是缺口拉取的触发条件
    follower.plane.report_entry(make_entry("T-local", owner="w1", state="claimed"))
    follower.plane.aggregator.flush(force=True)
    leader.plane.downlink_queue.drain()      # 别让 Leader 的周期包干扰周期统计

    stale = line_of(make_entry("T-local", owner="w1", version=1, incarnation=1))
    # 先跑一个「基线对齐」周期：report_entry 的安装会被算成上一周期的增量，
    # _check_gap 见到 installed > 0 会把 streak 归零（既有单测也提醒过这点）。
    cluster.advance(STEP, 1)
    for _ in range(3):
        leader.plane.downlink_queue.drain()   # 别让 Leader 的周期包混入统计
        assert cluster.bus.inject(follower.transport.address, stale, ttl=1)
        cluster.advance(STEP, 1)

    assert wait_for(
        lambda: any(call[2].get("mode") == "incremental"
                    for call in follower.puller.calls), timeout=3.0), (
        f"连续 3 个『收到下行但 0 安装』周期后应发起增量拉取；"
        f"gap={follower.plane.status()['gap']}, calls={follower.puller.calls}")
    incremental = [c for c in follower.puller.calls
                   if c[2].get("mode") == "incremental"]
    assert incremental[0][0] == leader.transport.address
    assert "watermarks" in incremental[0][2]
    assert follower.plane.status()["gap"]["pulls"] >= 1
    # 缺口拉取结果同样不触发下行（G5 同族）
    assert len(follower.plane.downlink_queue) == 0


# --------------------------------------------------------------------------- #
# 10. G1 / G3：上报只入内存；队列满优先保留瞬态
# --------------------------------------------------------------------------- #

def test_g1_report_is_memory_only_and_g3_queue_keeps_transient(cluster):
    cluster.converge()
    follower = cluster.followers()[0]

    before_records = len(cluster.bus.records)
    follower.plane.report_entry(make_entry("T-g1", owner="w1", state="claimed"))
    assert len(cluster.bus.records) == before_records, (
        "G1/I6：report() 调用线程不得做任何网络 I/O")
    follower.plane.aggregator.flush(force=True)
    assert follower.plane.uplink_queue.qsize() >= 1, "条目应只落在内存队列里"

    queue = follower.plane.uplink_queue
    while queue.get(timeout=0) is not None:
        pass
    for index in range(queue.capacity):
        queue.put(make_entry(f"T-steady-{index}", state="steady"))
    queue.put(make_entry("T-transient", state="claimed"))

    stats = queue.stats()
    assert stats["dropped_steady"] >= 1, "队列满应优先淘汰最旧的 steady"
    assert stats["dropped_transient"] == 0, "瞬态条目不得被优先丢弃"

    drained = []
    while True:
        item = queue.get(timeout=0)
        if item is None:
            break
        drained.append(item)
    assert any(item.tid == "T-transient" for item in drained)


# --------------------------------------------------------------------------- #
# 11. 契约：整套用例总时长 < 30s
# --------------------------------------------------------------------------- #

def test_suite_completes_within_thirty_seconds():
    assert _ELAPSED < 30.0, (
        f"本文件用例累计耗时 {_ELAPSED:.2f}s，必须 < 30s")
