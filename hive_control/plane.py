"""控制面编排器：线程装配、切主、DNA、水位缺口拉取、人工拉取队列。

对应 ``docs/控制面v0.5-实现约定.md`` §3.9（签名）、§6（线程模型）与母文档
§5 / §6 / §7（线程与非阻塞要求）。本模块把 T-0302…T-0309 装配成可运行的
控制面，本身**只做编排**：

- 不实现选举、不实现下行/上行/拉取协议、不碰 Git —— 这些都在各自的模块里；
- 所有时间来自注入的 ``clock``（默认 ``time.monotonic``），所以 ``tick()``
  是确定性的测试入口，单测不需要 ``sleep`` 等真实秒数；
- 任何网络 I/O 都在后台线程里发生，Worker 上报路径（``report_entry``）
  只做内存操作（I6 / G1）。

装配规则（契约 §3.9）：

* 启动时读/递增 incarnation（``IncarnationStore``，D5）；
* 本地 Worker 条目：盖 ``incarnation``/``version`` → ``view.install`` →
  Leader 则入 ``DownlinkQueue``（瞬态/urgent 另发一条立即事件包），
  否则入 ``Aggregator`` → ``UplinkQueue``；
* 收到上行 / 下行 / 拉取安装：``view.install`` 成功才 ``LeaseBook.note``
  与 ``WorkerServer.notify``；上行条目在 Leader 侧还要进下行队列向外扩散；
* Leader 每 ``heartbeat_interval`` 发心跳、每动态 interval 发周期下行；
  切主时 ``LeaseBook.on_leader_change()``（``grace_period`` 内不判过期）；
* 水位缺口：连续 ``gap_intervals`` 个周期**收到下行条目但 0 安装**且本机有
  ``claimed``/``releasing`` 条目 → 后台增量拉取（不阻塞任何线程）；
* 人工拉取：独立有界队列（``pull_queue_capacity``，默认 64）+ 独立线程池；
  结果按安装规则合并并通知 Worker，**不触发下行**（G5）。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from .config import ControlConfig, Limits, dynamic_multicast_interval
from .downlink import DownlinkReceiver, DownlinkSender, SocketTransport
from .election import BandwidthProbe, Election, SelfInfo
from .lease import LeaseBook
from .model import IncarnationStore, StateEntry
from .pull import ControlTcpServer, PullClient, PullRetry, PullService
from .uplink import UplinkSender
from .uplink_queue import Aggregator, UplinkQueue
from .view import DownlinkQueue, StateView
from .wire import encode_line
from .worker_sock import WorkerServer

__all__ = ["ControlPlane", "PULL_STATES"]

logger = logging.getLogger(__name__)

# 人工拉取请求的状态（D15：HTTP 侧只回 202 + request_id，状态在这里查）。
PULL_QUEUED = "queued"
PULL_RUNNING = "running"
PULL_DONE = "done"
PULL_ERROR = "error"
PULL_REJECTED = "rejected"
PULL_STATES = (PULL_QUEUED, PULL_RUNNING, PULL_DONE, PULL_ERROR, PULL_REJECTED)

# 拉取结果保留条数（避免长期运行把内存撑大；只留最近的）。
_PULL_RESULT_KEEP = 512
# 本地 agent 集合的缓存时长（``local_agents`` 可能来自集群节点表）。
_LOCAL_AGENT_TTL = 1.0
# 后台调度线程的轮询粒度：只决定“多久看一次到期没有”，不影响各周期本身。
_TICK_POLL = 0.05
# 错误环最多留多少条（status 里给排障看，不是日志替身）。
_MAX_ERRORS = 20


class _DaemonPool:
    """极小的守护线程池：只跑无返回值任务，退出时不阻塞解释器。

    ``concurrent.futures.ThreadPoolExecutor`` 在 3.9 里是非守护线程，脚本退出
    会被 atexit 的 join 拖住；控制面这套后台任务全部可丢弃，用守护线程更合适。
    """

    def __init__(self, size: int, name: str):
        self.name = name
        self._size = max(1, int(size))
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._started = False
        self._stop = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            for index in range(self._size):
                thread = threading.Thread(
                    target=self._run, name=f"{self.name}-{index}", daemon=True)
                self._threads.append(thread)
            threads = list(self._threads)
        for thread in threads:
            thread.start()

    def submit(self, task: Callable[[], None]) -> bool:
        """投递任务；池未启动时返回 False（调用方自行降级）。"""
        if not self._started or self._stop.is_set():
            return False
        try:
            self._queue.put_nowait(task)
        except queue.Full:  # pragma: no cover - 无界队列不会满
            return False
        return True

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        with self._lock:
            threads = list(self._threads)
            self._threads.clear()
            self._started = False
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                task()
            except Exception:  # noqa: BLE001  单个任务失败不能带走线程
                logger.exception("control-plane 后台任务失败")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _address_of(row: Mapping[str, Any]) -> str:
    """从各种来源的节点描述里取地址（适配器给的是 url 的 host）。"""
    for key in ("address", "host", "ip", "addr"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


class _HelloSplittingTransport:
    """装配用的传输包装：把 ``hello`` 从下行收包里摘出来交给选举状态机。

    ``DownlinkReceiver``（T-0307）按契约 §3.6 只分发 ``entry`` / ``heartbeat`` /
    ``expiring``，``hello`` 会被它静默丢弃；但选举（T-0303）必须看到邻居的
    Hello 才能建邻居表、才能比较排名。两头都是别人的卡，所以由编排器在
    中间加一层：收到包先挑出 ``hello`` 行送 ``on_hello``，**原包照旧整份**
    转给接收器（它自己会忽略 hello，其余行行为不变）。
    """

    def __init__(self, inner: Any, on_hello: Callable[[dict, Any], None]):
        self._inner = inner
        self._on_hello = on_hello

    # 发送侧：完全透传 ------------------------------------------------ #
    def send_multicast(self, data: bytes) -> None:
        self._inner.send_multicast(data)

    def send_unicast(self, addr: tuple[str, int], data: bytes) -> None:
        self._inner.send_unicast(addr, data)

    # 接收侧：摘出 hello，再整包透传 ---------------------------------- #
    def start_receiver(self, callback: Callable[..., None]) -> None:
        def wrapped(data: bytes, addr, ttl: int) -> None:
            for message in _iter_messages(data):
                if message.get("type") == "hello":
                    try:
                        self._on_hello(message, addr)
                    except Exception:  # noqa: BLE001  坏 Hello 不能打断收包
                        logger.debug("hello 处理失败", exc_info=True)
            callback(data, addr, ttl)

        self._inner.start_receiver(wrapped)

    def stop(self) -> None:
        self._inner.stop()

    def __getattr__(self, name: str) -> Any:
        """其余属性（``multicast_mode`` 等）透传给真实 transport。"""
        return getattr(self._inner, name)


def _iter_messages(data: bytes) -> list[dict]:
    """把一个 UDP 包按行解析成报文列表；坏行跳过（收包路径不能抛）。"""
    out: list[dict] = []
    for raw in data.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(message, dict):
            out.append(message)
    return out


class ControlPlane:
    """控制面编排器：一个节点上一个实例，负责装配并驱动全部子系统。

    公开属性（适配器 ``adapter.py`` 与单测都直接读）：``view``、
    ``downlink_queue``、``election``、``lease``、``uplink_queue``、
    ``aggregator``、``uplink_sender``、``tcp_server``、``downlink_sender``、
    ``downlink_receiver``、``worker_server``、``pull_service``、``pull_client``。
    """

    def __init__(
        self,
        *,
        node: str,
        address: str,
        listen_port: int,
        config: ControlConfig,
        limits: Limits | None = None,
        peer_provider: Callable[[], list[dict]] | None = None,
        local_agents: Callable[[], set[str]] | None = None,
        transport: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        bandwidth_probe: BandwidthProbe | None = None,
        incarnation_store: IncarnationStore | None = None,
        start_threads: bool = True,
    ):
        self.node = str(node)
        self.address = str(address)
        self.listen_port = int(listen_port)
        self.config = config
        self.limits = limits or Limits()
        self.clock = clock
        self._peer_provider = peer_provider
        self._local_agents = local_agents
        # ``start_threads=False`` 只装不跑：单测用它把后台调度关掉，改由 tick() 驱动。
        self._start_threads = bool(start_threads)

        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._stop = threading.Event()
        self._started = False
        self._threads: list[threading.Thread] = []
        self._errors: list[str] = []

        # -- 身份 ------------------------------------------------------- #
        self._probe = bandwidth_probe or BandwidthProbe()
        self._self_rid = BandwidthProbe.router_id(self.address, self.node)
        self._self_info = SelfInfo(
            router_id=self._self_rid,
            node=self.node,
            address=self.address,
            # 通告的 ``port`` 是 HTTP 端口：上行/拉取都按 ``port + offset`` 连（§2.4）。
            port=self.listen_port,
            priority=int(config.leader_priority),
            interface_mbps=self._probe.interface_mbps(),
        )
        # incarnation 存储：start() 时递增（D5）。
        self._incarnation_store = incarnation_store or IncarnationStore(
            self.limits.incarnation_path)
        self._incarnation = self._incarnation_store.current()

        # -- 纯内存组件（__init__ 建好，report_entry 未 start 也能用）---- #
        self.view = StateView(self.limits, clock=self.clock)
        self.downlink_queue = DownlinkQueue()
        self.election = Election(
            self._self_info, limits=self.limits, config=self.config,
            clock=self.clock)
        self.lease = LeaseBook(
            limits=self.limits, clock=self.clock,
            lease_ttl=self.config.lease_ttl,
            grace_period=self.config.grace_period)
        self.uplink_queue = UplinkQueue(self.limits.uplink_queue_capacity)
        self.aggregator = Aggregator(
            self._on_aggregated, window=self.limits.aggregation_window,
            clock=self.clock)
        self.uplink_sender = UplinkSender(
            self.uplink_queue, self._leader_endpoint, limits=self.limits,
            clock=self.clock, initial_backoff=self.config.rxmt_interval)

        # -- 传输层与网络组件（start() 才启动）--------------------------- #
        self._transport = transport or SocketTransport(
            group=self.limits.multicast_group, port=self.limits.multicast_port)
        # Hello 由接收器忽略，编排器在这里截出来喂选举（见包装类说明）。
        self._rx = _HelloSplittingTransport(self._transport, self._on_hello)
        self.downlink_sender = DownlinkSender(
            self.downlink_queue, limits=self.limits,
            transport=self._rx, clock=self.clock)
        self.downlink_receiver = DownlinkReceiver(
            on_entry=self._on_downlink_entry,
            on_heartbeat=self._on_heartbeat,
            on_expiring=self._on_expiring,
            limits=self.limits,
            transport=self._rx)
        self.pull_service = PullService(
            lambda: self.view, limits=self.limits, clock=self.clock)
        self.tcp_server = ControlTcpServer(
            self.address or "0.0.0.0", self._control_port(),
            on_entries=self._on_uplink_entries,
            pull=self.pull_service,
            limits=self.limits)
        self.worker_server: WorkerServer = WorkerServer(
            self.limits.socket_path, self._on_worker_entry, limits=self.limits)
        self.pull_client = PullClient(limits=self.limits, clock=self.clock)

        # -- 人工拉取：有界队列 + 独立守护线程池（I7 / D15）------------- #
        self._pull_queue: queue.Queue = queue.Queue(
            maxsize=max(1, int(self.limits.pull_queue_capacity)))
        self._pull_records: dict[str, dict] = {}
        self._pull_order: list[str] = []
        self._pull_seq = 0
        self._pull_pool = _DaemonPool(4, "control-pull")
        self._gap_pool = _DaemonPool(2, "control-gap-pull")
        self._gap_inflight = False

        # -- 计时与计数 -------------------------------------------------- #
        self._hello_at: float | None = None
        self._heartbeat_at: float | None = None
        self._period_at: float | None = None
        self._lease_at: float | None = None
        self._downlink_received = 0
        self._installed_total = 0
        self._gap_rx_base = 0
        self._gap_install_base = 0
        self._gap_streak = 0
        self._leader_key: tuple[int, int] | None = None
        self._local_agent_cache: set[str] = set()
        self._local_agent_at = float("-inf")
        self._counters: dict[str, int] = {
            "reported": 0,
            "installed": 0,
            "stale": 0,
            "notify": 0,
            "notify_dropped": 0,
            "downlink_entries": 0,
            "uplink_entries": 0,
            "uplink_queued": 0,
            "hellos": 0,
            "heartbeats": 0,
            "events": 0,
            "expiring_sent": 0,
            "expiring_received": 0,
            "dna_marked": 0,
            "leader_changes": 0,
            "gap_pulls": 0,
            "pull_requests": 0,
            "pull_done": 0,
            "pull_failed": 0,
            "pull_rejected": 0,
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """递增 incarnation、启动网络组件与后台线程；重复调用幂等。"""
        with self._start_lock:
            if self._started:
                return
            self._stop.clear()
            # §3.9：启动时读/递增 incarnation（D5）。
            self._incarnation = self._incarnation_store.next()
            if self._self_info.interface_mbps is None:
                # 探测不到带宽是正常路径（D10）；再探一次只为拿到后补的速率，
                # 仍拿不到就保持 None（系数回落 1.0）。
                self._self_info = SelfInfo(
                    router_id=self._self_info.router_id,
                    node=self._self_info.node,
                    address=self._self_info.address,
                    port=self._self_info.port,
                    priority=self._self_info.priority,
                    interface_mbps=self._probe.interface_mbps(),
                )
            # 网络组件按“能起就起、起不来记账”处理：单个端口被占不应该让
            # 整个 serve 起不来（原 HTTP 面还要继续跑）。
            # Worker socket 的失败面更宽（Windows 上没有 AF_UNIX 时是
            # AttributeError/OSError），单独兜住。
            try:
                self.worker_server.start()
            except (OSError, AttributeError, NotImplementedError) as exc:
                self._note_error(f"worker-sock: {exc}")
            for label, action in (
                ("tcp", self.tcp_server.start),
                ("downlink-rx", self.downlink_receiver.start),
            ):
                try:
                    action()
                except OSError as exc:
                    self._note_error(f"{label}: {exc}")
            self.aggregator.start()
            self.uplink_sender.start()
            self._pull_pool.start()
            self._gap_pool.start()
            self._started = True
            self._hello_at = None
            self._heartbeat_at = None
            self._period_at = self._clock()
            self._lease_at = None
            # 缺口统计的基线在这里落定：``_check_gap`` 只看“本周期相对上一周期
            # 的增量”，启动前发生的安装不能算进第一个周期。
            self._gap_rx_base = self._downlink_received
            self._gap_install_base = self._installed_total
            self._gap_streak = 0
            if self._start_threads:
                self._spawn_threads()
            # 首轮：发一次 Hello，让邻居尽快认识自己（D2 单播）。
            self._tick_election()

    def stop(self) -> None:
        """停掉所有后台线程与监听；幂等，可在任何状态下调用。"""
        with self._start_lock:
            if not self._started:
                return
            self._started = False
            self._stop.set()
            with self._lock:
                threads = list(self._threads)
                self._threads.clear()
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=2.0)
        # 先把手里的上报冲出去，再依次停组件（顺序与启动相反）。
        try:
            self.aggregator.flush(force=True)
        except Exception:  # noqa: BLE001  停机路径不能让异常逃出去
            logger.exception("aggregator flush 失败")
        self.aggregator.stop()
        self.uplink_sender.stop()
        self._pull_pool.stop()
        self._gap_pool.stop()
        for action in (self.tcp_server.stop, self.downlink_receiver.stop,
                       self.worker_server.stop):
            try:
                action()
            except Exception:  # noqa: BLE001
                logger.exception("控制面组件停止失败")

    # ------------------------------------------------------------------ #
    # Worker 入口（I6：只做内存操作）
    # ------------------------------------------------------------------ #
    def report_entry(self, entry: StateEntry) -> None:
        """Worker 上报入口：盖号 → 安装 → 按角色路由（永不阻塞、永不抛网络错）。"""
        if not isinstance(entry, StateEntry):
            raise TypeError("report_entry 只接受 StateEntry")
        self._counters["reported"] += 1
        stamped = self._stamp(entry)
        installed = self._install(stamped, source="local", notify=False)
        if not installed:
            return
        self.lease.note(stamped, local=True)
        self._route_local(stamped)

    # ------------------------------------------------------------------ #
    # 人工拉取（I7 / D15 / G5）
    # ------------------------------------------------------------------ #
    def request_pull(self, project: str, mode: str) -> str:
        """登记一次人工拉取，返回 ``request_id``；结果用 ``pull_result`` 查。

        队列满 / 参数非法时不抛异常：照样返回 id，状态直接置为
        ``rejected`` / ``error``，让 HTTP 层能给出确定的 429 / 400 语义。
        """
        project = str(project or "").strip()
        mode = str(mode or "").strip().lower()
        with self._lock:
            self._pull_seq += 1
            request_id = f"pull-{self._pull_seq}"
            record = {
                "request_id": request_id,
                "project": project,
                "mode": mode,
                "state": PULL_QUEUED,
                "created_at": self._clock(),
                "started_at": None,
                "finished_at": None,
                "fetched": 0,
                "installed": 0,
                "error": "",
                "retry_after_ms": 0,
                "source": "manual",
            }
            self._pull_records[request_id] = record
            self._pull_order.append(request_id)
            self._trim_pulls_locked()
        self._counters["pull_requests"] += 1
        if not project:
            record["state"] = PULL_ERROR
            record["error"] = "缺少 project"
            return request_id
        if mode not in ("incremental", "full"):
            record["state"] = PULL_ERROR
            record["error"] = f"未知拉取模式 {mode!r}"
            return request_id
        try:
            self._pull_queue.put_nowait(request_id)
        except queue.Full:
            record["state"] = PULL_REJECTED
            record["error"] = "拉取队列已满"
            self._counters["pull_rejected"] += 1
        return request_id

    def pull_result(self, request_id: str) -> dict | None:
        """查一次人工拉取的状态（``queued|running|done|error|rejected``）。"""
        with self._lock:
            record = self._pull_records.get(str(request_id))
            return dict(record) if record is not None else None

    # ------------------------------------------------------------------ #
    # 测试入口：跑一轮
    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        """跑一轮选举 / 下行周期 / 租约检查（不依赖真实秒数）。"""
        if not self._started:
            return
        self._tick_election()
        self._tick_aggregator()
        self._tick_period()
        self._tick_lease()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        """可观测快照；``adapter`` 直接并进 ``/api/cluster/status``。"""
        endpoint = self._leader_endpoint()
        leader_peer = self._leader_peer()
        with self._lock:
            pulls = [self._pull_records[rid] for rid in self._pull_order
                     if rid in self._pull_records]
            pull_summary = {
                "queued": self._pull_queue.qsize(),
                "results": len(pulls),
                "running": sum(1 for r in pulls
                               if r["state"] == PULL_RUNNING),
                "done": self._counters["pull_done"],
                "failed": self._counters["pull_failed"],
                "rejected": self._counters["pull_rejected"],
            }
            errors = list(self._errors)
        view_stats = self.view.stats()
        return {
            "enabled": True,
            "started": self._started,
            "node": self.node,
            "address": self.address,
            "router_id": self._self_rid,
            "listen_port": self.listen_port,
            "control_port": self.tcp_server.port,
            "incarnation": self._incarnation,
            "multicast_group": self.limits.multicast_group,
            "multicast_port": self.limits.multicast_port,
            "multicast_mode": self.downlink_sender.multicast_mode,
            "role": self._role(),
            "control_leader": self.election.leader_id,
            "leader_id": self.election.leader_id,
            "control_epoch": self.election.leader_epoch,
            "epoch": self.election.leader_epoch,
            "control_leader_address": leader_peer.get("address") if leader_peer else None,
            "is_leader": self.election.is_leader,
            "uplink_target": list(endpoint) if endpoint else None,
            "entries": view_stats.get("entries", 0),
            "downlink_queue": len(self.downlink_queue),
            "uplink_queue": self.uplink_queue.qsize(),
            "view": view_stats,
            "downlink": self.downlink_sender.status(),
            "downlink_rx": self.downlink_receiver.status(),
            "uplink": self.uplink_sender.status(),
            "lease": self.lease.status(),
            "election": self.election.status(),
            "tcp": self.tcp_server.status(),
            "worker_sock": self.worker_server.status(),
            "pull": pull_summary,
            "gap": {
                "streak": self._gap_streak,
                "threshold": self.limits.gap_intervals,
                "pulls": self._counters["gap_pulls"],
                "inflight": self._gap_inflight,
            },
            "counters": dict(self._counters),
            "errors": errors,
            "threads": sorted(self._thread_names()),
        }

    # ------------------------------------------------------------------ #
    # 装配：把各子系统接起来
    # ------------------------------------------------------------------ #
    def _spawn_threads(self) -> None:
        """按 §6 的线程表起后台调度线程（每个只做一件事）。"""
        specs = (
            ("control-election", self._loop_election),
            ("control-downlink-tx", self._loop_downlink),
            ("control-lease", self._loop_lease),
            ("control-pull", self._loop_pull),
        )
        for name, target in specs:
            thread = threading.Thread(target=target, name=name, daemon=True)
            with self._lock:
                self._threads.append(thread)
            thread.start()

    def _loop_election(self) -> None:
        while not self._stop.wait(_TICK_POLL):
            try:
                self._tick_election()
            except Exception:  # noqa: BLE001  后台线程必须活着
                logger.exception("control-election 轮次失败")

    def _loop_downlink(self) -> None:
        while not self._stop.wait(_TICK_POLL):
            try:
                self._tick_period()
            except Exception:  # noqa: BLE001
                logger.exception("control-downlink-tx 轮次失败")

    def _loop_lease(self) -> None:
        while not self._stop.wait(_TICK_POLL):
            try:
                self._tick_lease()
            except Exception:  # noqa: BLE001
                logger.exception("control-lease 轮次失败")

    def _loop_pull(self) -> None:
        """人工拉取队列的消费线程：只排队，真正干活在 ``control-pull`` 池里。

        独立队列 + 独立线程池是 I7 的落点：Leader 下行/选举/租约的定时器
        不会被一次慢拉取拖住。
        """
        while not self._stop.is_set():
            try:
                request_id = self._pull_queue.get(timeout=_TICK_POLL)
            except queue.Empty:
                continue
            if not self._pull_pool.submit(lambda rid=request_id: self._run_pull(rid)):
                with self._lock:
                    record = self._pull_records.get(request_id)
                if record is not None and record["state"] == PULL_QUEUED:
                    record["state"] = PULL_ERROR
                    record["error"] = "拉取线程池未运行"
                    record["finished_at"] = self._clock()
                    self._counters["pull_failed"] += 1

    def _thread_names(self) -> set[str]:
        with self._lock:
            return {thread.name for thread in self._threads}

    # ------------------------------------------------------------------ #
    # 轮次：选举 / 聚合 / 周期 / 租约
    # ------------------------------------------------------------------ #
    def _tick_election(self) -> None:
        event = self.election.tick()
        if event == "declare":
            # 声明要立刻发出去，不能等下一次 Hello 周期（否则 Waiting 结束后的
            # 收敛会白白多等一个 hello_interval）。
            self._send_hello()
        elif event == "become_leader":
            self._on_became_leader()
        elif event == "leader_lost":
            self._on_leader_lost()
        self._sync_leader()
        self._send_hello_if_due()
        self._send_heartbeat_if_due()

    def _tick_aggregator(self) -> None:
        """把聚合窗口到期的条目交给上行队列（窗口由注入时钟决定）。"""
        try:
            self.aggregator.flush()
        except Exception:  # noqa: BLE001
            logger.exception("aggregator flush 失败")

    def _tick_period(self) -> None:
        """周期下行（Leader）或水位缺口检测（非 Leader），并推进 DNA。"""
        now = self._clock()
        interval = self._downlink_interval()
        with self._lock:
            last = self._period_at
            if last is not None and now - last < interval:
                return
            self._period_at = now
        # D6：连续 dna_quiet_intervals 个周期无变化的 steady 条目置 DNA，
        # 置位后立刻从待下行队列里摘掉（DNA 不进周期下行，但仍可拉取）。
        for quiet in self.view.mark_quiet(interval):
            self.downlink_queue.drop(quiet.project, quiet.tid)
            self._counters["dna_marked"] += 1
        if self.election.is_leader:
            self.downlink_sender.set_peers(self._peer_addresses())
            try:
                self.downlink_sender.send_periodic()
            except OSError as exc:
                self._note_error(f"downlink-tx: {exc}")
        else:
            self._check_gap()

    def _tick_lease(self) -> None:
        """本地续约（上行）与 Leader 侧过期提示（D4 只提示、不安装）。"""
        now = self._clock()
        throttle = min(1.0, max(0.05, float(self.limits.heartbeat_interval)))
        with self._lock:
            last = self._lease_at
            if last is not None and now - last < throttle:
                return
            self._lease_at = now
        for renewal in self.lease.renewals_due():
            self.lease.mark_renewed(renewal)
            if not self.election.is_leader:
                # 续约本身也是一次“我还在”的上报：走聚合器上行给 Leader。
                self.aggregator.submit(renewal)
        if not self.election.is_leader:
            return
        for hint in self.lease.expired():
            self._send_expiring(hint)

    # ------------------------------------------------------------------ #
    # Leader 变更
    # ------------------------------------------------------------------ #
    def _on_became_leader(self) -> None:
        self._counters["leader_changes"] += 1
        self.lease.on_leader_change()  # grace_period 内不判过期
        with self._lock:
            self._gap_streak = 0
        self._leader_key = (self._self_rid, self.election.leader_epoch)
        logger.info("control plane: 本节点成为 Leader（epoch=%s）",
                    self.election.leader_epoch)
        # 切主：Leader 不再向任何人上行，先停上行并把目标清掉（未发条目留在
        # 发送器的待发缓冲里，下野后能原样继续发，不丢队列）。
        self.uplink_sender.clear_target()
        self.uplink_sender.stop()
        # 自己手里的条目现在要由本节点对外扩散，先灌进待下行队列。
        for entry in self.view.entries():
            if entry.dna:
                continue
            self.downlink_queue.put(entry)

    def _on_leader_lost(self) -> None:
        self._counters["leader_changes"] += 1
        self.lease.on_leader_change()
        with self._lock:
            self._gap_streak = 0
        self._leader_key = None
        # 旧 Leader 已不可用：清掉上行目标，等下一个 Leader 出现再重定向。
        self.uplink_sender.clear_target()
        logger.info("control plane: Leader 失效，等待重新选举")

    def _sync_leader(self) -> None:
        """Leader 变化：重置租约宽限、同步上行目标与下行回退地址。"""
        endpoint = self._leader_endpoint()
        key = (self.election.leader_id or 0, self.election.leader_epoch)
        with self._lock:
            changed = key != self._leader_key
            self._leader_key = key
        if changed:
            self.lease.on_leader_change()
        if endpoint is None:
            self.uplink_sender.clear_target()
        else:
            self.uplink_sender.set_target(*endpoint)
            # 下野后重新拿到 Leader：把上行发送线程拉起来（幂等），未发条目
            # 仍在发送器缓冲里，从新目标继续发。
            self.uplink_sender.start()
        self.downlink_sender.set_peers(self._peer_addresses())

    def _role(self) -> str:
        """控制面角色：``leader`` / ``follower`` / ``waiting`` / ``candidate``。"""
        if self.election.is_leader:
            return "leader"
        if self.election.leader_id is not None:
            return "follower"
        phase = str(self.election.status().get("phase") or "")
        return "waiting" if phase == "waiting" else "candidate"

    # ------------------------------------------------------------------ #
    # Hello / Heartbeat
    # ------------------------------------------------------------------ #
    def _send_hello_if_due(self) -> None:
        now = self._clock()
        last = self._hello_at
        if last is not None and now - last < float(self.limits.hello_interval):
            return
        self._send_hello()

    def _send_hello(self) -> None:
        payload = self.election.hello()
        data = encode_line(payload)
        self._hello_at = self._clock()
        targets = self._hello_targets()
        if not targets:
            return
        for target in targets:
            try:
                self._transport.send_unicast(target, data)
                self._counters["hellos"] += 1
            except OSError as exc:
                self._note_error(f"hello->{target}: {exc}")

    def _send_heartbeat_if_due(self) -> None:
        if not self.election.is_leader:
            return
        now = self._clock()
        last = self._heartbeat_at
        if last is not None and now - last < float(self.limits.heartbeat_interval):
            return
        try:
            self.downlink_sender.send_heartbeat(self.election.heartbeat())
            self._heartbeat_at = now
            self._counters["heartbeats"] += 1
        except OSError as exc:
            self._note_error(f"heartbeat: {exc}")

    def _send_expiring(self, hint: StateEntry) -> None:
        """下发 ``expiring`` 信封（D4：不带版本、不安装进任何视图）。"""
        payload = {
            "type": "expiring",
            "project": hint.project,
            "tid": hint.tid,
            "owner_agent": hint.owner_agent,
            "incarnation": hint.incarnation,
            "version": hint.version,
        }
        self._broadcast(encode_line(payload))
        self._counters["expiring_sent"] += 1
        # 业主就是本机的 Worker 时，补一条定向通知（socket 写不满不阻塞）。
        if self._is_local(hint.owner_agent):
            self._notify({"type": "expiring", "project": hint.project,
                          "tid": hint.tid, "owner_agent": hint.owner_agent,
                          "incarnation": hint.incarnation,
                          "version": hint.version}, hint.owner_agent)

    def _broadcast(self, data: bytes) -> int:
        """一条独立报文：组播，或回退模式下逐节点单播（D9）。"""
        try:
            if self.downlink_sender.multicast_mode == "fallback":
                targets = self._peer_addresses()
                for target in targets:
                    self._transport.send_unicast(target, data)
                return len(targets)
            self._transport.send_multicast(data)
            return 1
        except OSError as exc:
            self._note_error(f"broadcast: {exc}")
            return 0

    # ------------------------------------------------------------------ #
    # 安装与通知
    # ------------------------------------------------------------------ #
    def _install(
        self,
        entry: StateEntry,
        *,
        source: str,
        notify: bool = True,
        enqueue_downlink: bool = False,
    ) -> bool:
        """按 I5 安装；成功才记租约、才通知 Worker、才考虑向下扩散。"""
        result = self.view.install(entry)
        if not result.installed:
            self._counters["stale"] += 1
            return False
        self._counters["installed"] += 1
        self._installed_total += 1
        self.lease.note(entry, local=self._is_local(entry.owner_agent))
        if enqueue_downlink and self.election.is_leader and not entry.dna:
            # Leader 收到上行条目后要向外扩散，否则其它节点永远看不到它。
            self.downlink_queue.put(entry)
        if notify:
            self._notify({"type": "install", "entry": entry.to_dict()},
                         entry.owner_agent)
        return True

    def _notify(self, message: dict, agent: str) -> None:
        if not agent:
            return
        result: Any = 0
        try:
            result = self.worker_server.notify(agent, message)
        except Exception:  # noqa: BLE001  通知失败不能影响安装路径
            logger.debug("worker notify 失败", exc_info=True)
            return
        try:
            delivered = int(result or 0)
        except (TypeError, ValueError):
            delivered = 0
        if delivered:
            self._counters["notify"] += delivered
        else:
            self._counters["notify_dropped"] += 1

    def _route_local(self, entry: StateEntry) -> None:
        """本地条目：Leader 直接下行（瞬态另发事件包），否则走上行聚合。"""
        if self.election.is_leader:
            self.downlink_queue.put(entry)
            if entry.transient or entry.urgent:
                try:
                    self.downlink_sender.send_event(entry)
                    self._counters["events"] += 1
                except OSError as exc:
                    self._note_error(f"event: {exc}")
        else:
            self.aggregator.submit(entry)

    def _on_aggregated(self, batch: list[StateEntry]) -> None:
        """聚合器 sink：把一批条目放进上行队列（I6：不做网络 I/O）。"""
        for entry in batch:
            self.uplink_queue.put(entry)
        self._counters["uplink_queued"] += len(batch)

    # ------------------------------------------------------------------ #
    # 入站回调
    # ------------------------------------------------------------------ #
    def _on_worker_entry(self, entry: StateEntry, agent: str) -> None:
        """Worker socket 上报（服务端的读线程调用）。"""
        if not agent:
            return
        if not entry.owner_agent:
            entry = StateEntry(
                project=entry.project, tid=entry.tid, version=entry.version,
                incarnation=entry.incarnation, owner_agent=agent,
                state=entry.state, dna=entry.dna,
                updated_at=entry.updated_at, urgent=entry.urgent)
        self.report_entry(entry)

    def _on_uplink_entries(self, entries: Iterable[StateEntry]) -> None:
        """上行 TCP：本节点是 Leader 时接收子节点上报并向外扩散。"""
        for entry in entries:
            self._counters["uplink_entries"] += 1
            self._install(entry, source="uplink", enqueue_downlink=True)

    def _on_downlink_entry(self, entry: StateEntry) -> None:
        """下行 UDP：只做“校验 → 安装 → 非阻塞通知”。"""
        self._downlink_received += 1
        self._counters["downlink_entries"] += 1
        self._install(entry, source="downlink", enqueue_downlink=False)

    def _on_hello(self, message: dict, source: Any = None) -> None:
        """邻居 Hello（D2 单播）：交给选举状态机建邻居 / 认主。"""
        try:
            event = self.election.observe(message, source)
        except Exception:  # noqa: BLE001  畸形报文不能让收包循环停下
            logger.debug("Hello 处理失败", exc_info=True)
            return
        self._sync_leader()
        if event in ("adopted", "leader_lost"):
            self._counters["leader_changes"] += 1

    def _on_heartbeat(self, message: dict) -> None:
        """Leader 心跳：交给选举状态机（刷新邻居存活与 epoch）。"""
        try:
            event = self.election.observe(message)
        except Exception:  # noqa: BLE001  畸形报文不能让收包循环停下
            logger.debug("心跳处理失败", exc_info=True)
            return
        self._sync_leader()
        if event == "leader_lost":
            self._counters["leader_changes"] += 1

    def _on_expiring(self, message: dict) -> None:
        """过期提示（D4）：不安装进视图，只计数并转发给本地业主。"""
        self._counters["expiring_received"] += 1
        owner = str(message.get("owner_agent") or "")
        if owner and self._is_local(owner):
            self._notify({"type": "expiring", **{k: message.get(k) for k in (
                "project", "tid", "owner_agent", "incarnation", "version")}},
                owner)

    # ------------------------------------------------------------------ #
    # 水位缺口（母文档 §6.2）
    # ------------------------------------------------------------------ #
    def _check_gap(self) -> None:
        with self._lock:
            received = self._downlink_received - self._gap_rx_base
            installed = self._installed_total - self._gap_install_base
            self._gap_rx_base = self._downlink_received
            self._gap_install_base = self._installed_total
            if received <= 0:
                self._gap_streak = 0
                return
            if installed > 0:
                self._gap_streak = 0
                return
            self._gap_streak += 1
            streak = self._gap_streak
            inflight = self._gap_inflight
        if streak < int(self.limits.gap_intervals) or inflight:
            return
        projects = self._local_transient_projects()
        if not projects:
            return
        with self._lock:
            self._gap_inflight = True
        self._counters["gap_pulls"] += 1
        # 缺口拉取走独立池（2 线程 + inflight 串行），不和人工拉取抢线程。
        job = self._gap_pool.submit(lambda: self._run_gap_pull(projects))
        if not job:
            with self._lock:
                self._gap_inflight = False

    def _run_gap_pull(self, projects: list[str]) -> None:
        """缺口拉取：后台增量拉取，结果按安装规则合并（不触发下行）。"""
        endpoint = self._pull_endpoint()
        try:
            if endpoint is None:
                return
            for project in projects:
                request = {
                    "type": "pull",
                    "project": project,
                    "mode": "incremental",
                    "watermarks": self.view.watermarks(project),
                    "from": None,
                }
                try:
                    entries = self._client().fetch(
                        endpoint[0], endpoint[1], request)
                except (PullRetry, OSError):
                    continue
                for entry in entries:
                    self._install(entry, source="pull", enqueue_downlink=False)
        finally:
            with self._lock:
                self._gap_inflight = False
                self._gap_streak = 0

    def _local_transient_projects(self) -> list[str]:
        """本机持有 claimed/releasing 条目的 project（缺口拉取的触发条件）。"""
        projects: list[str] = []
        local = self._local_agents_now()
        for entry in self.view.entries():
            if entry.state not in ("claimed", "releasing"):
                continue
            if local and entry.owner_agent and entry.owner_agent not in local:
                continue
            if entry.project not in projects:
                projects.append(entry.project)
        if not projects and self.config.project_tag:
            projects.append(self.config.project_tag)
        return projects

    # ------------------------------------------------------------------ #
    # 人工拉取执行
    # ------------------------------------------------------------------ #
    def _run_pull(self, request_id: str) -> None:
        with self._lock:
            record = self._pull_records.get(request_id)
        if record is None:
            return
        record["state"] = PULL_RUNNING
        record["started_at"] = self._clock()
        project = record["project"]
        mode = record["mode"]
        if self.election.is_leader:
            # 本机就是 Leader：直接问自己的 PullService，不必绕一圈 TCP。
            try:
                entries = self._self_pull(project, mode)
            except PullRetry as exc:
                self._pull_retry(record, exc)
                return
            except Exception as exc:  # noqa: BLE001
                self._pull_error(record, exc)
                return
        else:
            endpoint = self._pull_endpoint()
            if endpoint is None:
                record["state"] = PULL_ERROR
                record["error"] = "没有可用的 Leader 端点"
                record["finished_at"] = self._clock()
                self._counters["pull_failed"] += 1
                return
            request = {"type": "pull", "project": project, "mode": mode,
                       "from": None}
            if mode == "incremental":
                request["watermarks"] = self.view.watermarks(project)
            try:
                entries = self._client().fetch(endpoint[0], endpoint[1], request)
            except PullRetry as exc:
                self._pull_retry(record, exc)
                return
            except Exception as exc:  # noqa: BLE001  网络/协议错误都要落到记录里
                self._pull_error(record, exc)
                return
        record["fetched"] = len(entries)
        installed = 0
        for entry in entries:
            # G5：人工拉取只合并状态，绝不触发下行。
            if self._install(entry, source="pull", enqueue_downlink=False):
                installed += 1
        record["installed"] = installed
        record["state"] = PULL_DONE
        record["finished_at"] = self._clock()
        self._counters["pull_done"] += 1

    def _pull_retry(self, record: dict, exc: PullRetry) -> None:
        record["state"] = PULL_ERROR
        record["error"] = f"配额重试：{exc.after_ms}ms 后再试"
        record["retry_after_ms"] = int(exc.after_ms)
        record["finished_at"] = self._clock()
        self._counters["pull_failed"] += 1

    def _pull_error(self, record: dict, exc: Exception) -> None:
        record["state"] = PULL_ERROR
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["finished_at"] = self._clock()
        self._counters["pull_failed"] += 1

    def _self_pull(self, project: str, mode: str) -> list[StateEntry]:
        """本机是 Leader 时的本地拉取：直接问自己的 PullService。

        否则会连自己的 ``port+1``——临时端口/未监听时白白失败一次，而且
        语义上没必要绕一圈网络。调用方保证 ``election.is_leader``。
        """
        request = {"type": "pull", "project": project, "mode": mode,
                   "from": None}
        if mode == "incremental":
            request["watermarks"] = self.view.watermarks(project)
        rows = self.pull_service.handle(request)
        if rows and rows[0].get("type") == "retry":
            raise PullRetry(int(rows[0].get("after_ms") or 1))
        entries: list[StateEntry] = []
        for row in rows:
            if row.get("type") != "entry":
                continue
            body = row.get("entry")
            if not isinstance(body, Mapping):
                continue
            try:
                entries.append(StateEntry.from_dict(body))
            except ValueError:
                continue
        return entries

    def _trim_pulls_locked(self) -> None:
        """结果表只留最近 ``_PULL_RESULT_KEEP`` 条，防止长期运行无界增长。"""
        overflow = len(self._pull_order) - _PULL_RESULT_KEEP
        if overflow <= 0:
            return
        for request_id in self._pull_order[:overflow]:
            self._pull_records.pop(request_id, None)
        del self._pull_order[:overflow]

    # ------------------------------------------------------------------ #
    # 端点 / 邻居 / 身份
    # ------------------------------------------------------------------ #
    def _leader_peer(self) -> dict | None:
        leader_id = self.election.leader_id
        if leader_id is None or leader_id == self._self_rid:
            return None
        for peer in self.election.peers():
            if int(peer.get("router_id") or 0) == int(leader_id):
                return peer
        return None

    def _leader_endpoint(self) -> tuple[str, int] | None:
        """Leader 的 (地址, HTTP 端口)——上行与拉取都会再 +offset。"""
        leader_id = self.election.leader_id
        if leader_id is None or leader_id == self._self_rid:
            return None
        peer = self._leader_peer()
        if peer is None:
            return None
        address = str(peer.get("address") or "")
        if not address:
            return None
        port = int(peer.get("port") or 0)
        if port <= 0:
            return None
        return (address, port)

    def _pull_endpoint(self) -> tuple[str, int] | None:
        endpoint = self._leader_endpoint()
        if endpoint is None:
            return None
        return (endpoint[0], endpoint[1] + self._uplink_port_offset())

    def _hello_targets(self) -> list[tuple[str, int]]:
        """Hello 单播目标（D2）：已知邻居 + 适配器给的 seed，端口固定 47899。"""
        port = int(self.limits.multicast_port)
        targets: list[tuple[str, int]] = []
        seen: set[str] = set()
        for peer in self.election.peers():
            address = str(peer.get("address") or "")
            if not address or address == self.address or address in seen:
                continue
            seen.add(address)
            targets.append((address, port))
        for seed in self._seed_peers():
            address = seed["address"]
            if not address or address == self.address or address in seen:
                continue
            seen.add(address)
            targets.append((address, port))
        return targets

    def _peer_addresses(self) -> list[tuple[str, int]]:
        """回退模式下逐节点单播的目标（D9）。"""
        return list(self._hello_targets())

    def _seed_peers(self) -> list[dict]:
        provider = self._peer_provider
        if provider is None:
            return []
        try:
            raw = provider() or []
        except Exception:  # noqa: BLE001  集群节点表读失败不能拖垮控制面
            logger.debug("peer_provider 失败", exc_info=True)
            return []
        out: list[dict] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            address = _address_of(item)
            if not address:
                continue
            out.append({
                "address": address,
                "node": str(item.get("node") or item.get("name") or ""),
                "priority": int(item.get("priority") or 0),
            })
        return out

    def _local_agents_now(self) -> set[str]:
        """本机 agent 集合（``local_agents`` 回调，短 TTL 缓存以保住上报路径）。"""
        provider = self._local_agents
        if provider is None:
            return set()
        now = self._clock()
        with self._lock:
            if now - self._local_agent_at < _LOCAL_AGENT_TTL:
                return set(self._local_agent_cache)
        try:
            raw = provider() or set()
        except Exception:  # noqa: BLE001
            raw = set()
        try:
            agents = {str(item) for item in raw if item}
        except TypeError:
            agents = set()
        with self._lock:
            self._local_agent_cache = agents
            self._local_agent_at = now
        return set(agents)

    def _is_local(self, agent: str) -> bool:
        if not agent:
            return False
        return agent in self._local_agents_now()

    # ------------------------------------------------------------------ #
    # D5 盖号
    # ------------------------------------------------------------------ #
    def _stamp(self, entry: StateEntry) -> StateEntry:
        """给 Worker 上报的条目盖 ``incarnation`` / ``version``（D5）。

        Worker 不持有可靠版本号，所以这里以**本 serve 的视图**为准：
        首次见到该 key 用本 serve 的 incarnation + 至少 1 的版本；之后每次
        上报都严格 ``+1``，绝不回卷（Worker 传更小的号也拉不回来）。
        """
        current = self.view.get(entry.project, entry.tid)
        incarnation = int(self._incarnation)
        if current is None:
            version = max(1, int(entry.version))
            if incarnation <= 0:
                incarnation = max(0, int(entry.incarnation))
        else:
            incarnation = max(incarnation, int(current.incarnation))
            if incarnation > int(current.incarnation):
                version = max(1, int(entry.version))
            else:
                version = int(current.version) + 1
        updated_at = int(entry.updated_at)
        if updated_at <= 0:
            updated_at = _now_ms()
        return StateEntry(
            project=entry.project,
            tid=entry.tid,
            version=version,
            incarnation=incarnation,
            owner_agent=entry.owner_agent,
            state=entry.state,
            dna=entry.dna,
            updated_at=updated_at,
            urgent=entry.urgent,
        )

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #
    def _downlink_interval(self) -> float:
        configured = self.config.leader_multicast_interval
        if configured:
            return float(configured)
        return dynamic_multicast_interval(self._active_entries())

    def _control_port(self) -> int:
        """上行/拉取共用的 TCP 端口（``listen_port + offset``）。

        ``listen_port == 0`` 表示“随便给个临时端口”（单测用）：这时整条
        控制面都退到临时端口族，不能算出 0+1=1 这种要 root 的端口。
        """
        offset = int(self.limits.uplink_port_offset)
        if self.listen_port <= 0:
            return 0
        return self.listen_port + offset

    def _uplink_port_offset(self) -> int:
        """上行/拉取相对 HTTP 端口的偏移（§2.4：``port + 1``）。

        这是**被通告端口的约定**，与自己的 ``listen_port`` 是否为 0 无关；
        自己绑临时端口时只影响 ``_control_port()``。
        """
        return int(self.limits.uplink_port_offset)

    def _client(self) -> Any:
        """当前生效的拉取客户端（测试/嵌入式可整体替换 ``pull_client``）。"""
        return self.pull_client

    def _active_entries(self) -> int:
        """D7：``dna=False`` 且 ``state != "steady"`` 的条目数。"""
        return sum(1 for entry in self.view.entries()
                   if not entry.dna and entry.state != "steady")

    def _clock(self) -> float:
        return float(self.clock())

    def _note_error(self, text: str) -> None:
        with self._lock:
            self._errors.append(str(text))
            if len(self._errors) > _MAX_ERRORS:
                del self._errors[:-_MAX_ERRORS]
