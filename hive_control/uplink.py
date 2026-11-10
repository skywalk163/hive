"""控制面上行 TCP 发送器（T-0305）。

对应 ``docs/控制面v0.5-实现约定.md`` §3.4 与母文档 §5.1：

- 子节点把聚合后的状态条目 **TCP 单播**到 Leader 的 ``port+1``（§2.4）：
  目标里的 ``port`` 是 Leader 的 HTTP 端口，实际连接端口按
  ``limits.uplink_port_offset``（默认 1）加偏移；
- 连接不可用不阻塞上报队列：发送器只在“真要发”时取队列，失败把未发条目留在
  自己的待发缓冲里，切主后原样继续发（不丢队列）；
- 失败按指数退避：500ms 起翻倍，上限 ``limits.uplink_backoff_max``（默认 10s），
  成功清零；
- 本模块只发 ``entry`` 行，不做监听、不做拉取（监听/拉取归 ``pull.py``，T-0306）。

所有时间源可注入；单测用假 connector 与临时端口，不依赖真实网络环境。
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from typing import Callable, Deque

from .config import Limits
from .model import StateEntry
from .uplink_queue import UplinkQueue
from .wire import encode_line

__all__ = ["UplinkSender", "DEFAULT_INITIAL_BACKOFF", "CONNECT_TIMEOUT"]

# 母文档 §5.1：首次失败后 500ms 重试。
DEFAULT_INITIAL_BACKOFF = 0.5
# 连接/发送超时：必须有界，避免 Leader 半死时卡住发送线程（§6 网络 I/O 都有超时）。
CONNECT_TIMEOUT = 2.0

TargetProvider = Callable[[], "tuple[str, int] | None"]
Connector = Callable[[str, int], socket.socket]


def _default_connector(host: str, port: int) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:  # 某些平台/假 socket 不支持，不影响发送
        pass
    return sock


class UplinkSender:
    """把 :class:`UplinkQueue` 里的聚合条目发送到当前 Leader 的 TCP 端口。

    线程模型（§6）：``control-uplink`` 线程只做“聚合结果 → TCP 单播”，
    不做拉取响应、不持有其它模块的锁。``send_once()`` 是同步测试入口，
    与后台线程互斥（同一时刻只会有一批在发）。
    """

    def __init__(
        self,
        queue: UplinkQueue,
        target: TargetProvider,
        *,
        limits: Limits,
        clock: Callable[[], float] = time.monotonic,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        connector: Connector | None = None,
    ):
        max_backoff = float(getattr(limits, "uplink_backoff_max", 10.0))
        if max_backoff <= 0:
            raise ValueError("uplink_backoff_max 必须为正数")
        initial = float(initial_backoff)
        if initial <= 0:
            raise ValueError("initial_backoff 必须为正数")
        self.queue = queue
        self._target = target
        self.limits = limits
        self._clock = clock
        self._connector: Connector = connector or _default_connector
        self._port_offset = int(getattr(limits, "uplink_port_offset", 1))

        self._initial_backoff = initial
        self._max_backoff = max_backoff
        # 失败后将要等待的时长；成功后回到 initial。
        self._next_backoff = initial
        # 当前正在生效的等待时长（0 = 可以立即重试）。
        self._backoff = 0.0
        self._next_attempt = 0.0

        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending: Deque[StateEntry] = deque()

        self._explicit: tuple[str, int] | None = None
        self._failures = 0
        self._sent = 0
        self._bytes_sent = 0
        self._redirections = 0
        self._connected = False
        self._last_error = ""
        self._last_success_at: float | None = None
        self._dropped = 0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- 目标 ---------------------------------------------------------- #
    def set_target(self, host: str, port: int) -> None:
        """切主重定向：指向新 Leader 的 HTTP 端口（连接端口 = ``port + 1``）。

        只改目标地址，**不动**队列与待发缓冲；新 Leader 立即获得一次发送机会
        （退避清零），避免旧 Leader 的退避把新连接也一起拖住。
        """
        if not host:
            raise ValueError("host 不能为空")
        port = int(port)
        if not 0 < port < 65536:
            raise ValueError("port 必须在 1..65535")
        with self._lock:
            previous = self._explicit
            self._explicit = (str(host), port)
            if previous != self._explicit:
                self._redirections += 1
                self._backoff = 0.0
                self._next_backoff = self._initial_backoff
                self._next_attempt = 0.0

    def clear_target(self) -> None:
        """Leader 未知时清空显式目标（回落到 ``target`` 回调）。"""
        with self._lock:
            self._explicit = None
            self._backoff = 0.0
            self._next_backoff = self._initial_backoff
            self._next_attempt = 0.0

    def _resolve_target(self) -> tuple[str, int] | None:
        with self._lock:
            explicit = self._explicit
        if explicit is not None:
            return explicit
        try:
            resolved = self._target()
        except Exception as exc:  # noqa: BLE001 —— 回调坏了不能拖垮发送线程
            with self._lock:
                self._last_error = f"target: {exc}"
            return None
        if not resolved:
            return None
        host, port = resolved
        return str(host), int(port)

    # -- 发送 ---------------------------------------------------------- #
    def send_once(self) -> bool:
        """测试入口：把队列取空并发一批；返回是否真的发出去。

        - 没有可用的 Leader：不动队列，返回 ``False``；
        - 仍在退避窗口内：不动队列，返回 ``False``；
        - 连接/发送失败：已发出的条目出队，未发的留在待发缓冲（不丢），
          记录失败并推迟下次尝试，返回 ``False``；
        - 成功：返回 ``True``，退避与失败计数清零。
        """
        target = self._resolve_target()
        if target is None:
            with self._lock:
                self._last_error = "no leader target"
            return False

        now = self._clock()
        with self._lock:
            if now < self._next_attempt:
                return False

        with self._send_lock:
            batch = self._take_batch()
            if not batch:
                return False
            return self._send_batch(target, batch)

    def _take_batch(self) -> list[StateEntry]:
        """待发缓冲在前（顺序不变），再把队列里现有的条目非阻塞地并入本批。"""
        with self._lock:
            while True:
                item = self.queue.get(timeout=0)
                if item is None:
                    break
                self._pending.append(item)
            return list(self._pending)

    def _connect_endpoint(self, target: tuple[str, int]) -> tuple[str, int]:
        """Leader HTTP 端口 → 上行 TCP 端口（``port + offset``，默认 +1）。"""
        host, port = target
        return host, int(port) + self._port_offset

    def _send_batch(self, target: tuple[str, int], batch: list[StateEntry]) -> bool:
        host, port = self._connect_endpoint(target)
        completed = 0
        sent_bytes = 0
        sock: socket.socket | None = None
        error: Exception | None = None
        try:
            sock = self._connector(host, port)
            with self._lock:
                self._connected = True
            for entry in batch:
                line = encode_line({"type": "entry", "entry": entry.to_dict()})
                sock.sendall(line)
                completed += 1
                sent_bytes += len(line)
        except Exception as exc:  # noqa: BLE001 —— 网络错误一律按“这批失败”处理
            error = exc
        finally:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        with self._lock:
            self._sent += completed
            self._bytes_sent += sent_bytes
        if completed:
            self._forget_sent(completed)

        if error is not None:
            self._record_failure(error)
            return False

        with self._lock:
            self._failures = 0
            self._backoff = 0.0
            self._next_backoff = self._initial_backoff
            self._next_attempt = 0.0
            self._connected = False
            self._last_success_at = self._clock()
            if not self._pending:
                self._last_error = ""
        return True

    def _forget_sent(self, count: int) -> None:
        with self._lock:
            for _ in range(min(count, len(self._pending))):
                self._pending.popleft()

    def _record_failure(self, error: Exception) -> None:
        with self._lock:
            self._failures += 1
            self._connected = False
            self._last_error = f"{type(error).__name__}: {error}"
            delay = self._next_backoff
            self._backoff = delay
            self._next_backoff = min(delay * 2.0, self._max_backoff)
            self._next_attempt = self._clock() + delay

    # -- 生命周期 ------------------------------------------------------ #
    def start(self) -> None:
        """启动后台发送线程；重复调用不重复起线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="control-uplink", daemon=True)
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        """停止后台线程；未发完的条目保留在待发缓冲，下次 start 继续。"""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=CONNECT_TIMEOUT + 1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            target = self._resolve_target()
            if target is None:
                self._stop.wait(0.2)
                continue
            with self._lock:
                blocked = self._clock() < self._next_attempt
                wait = self._next_attempt - self._clock()
            if blocked:
                self._stop.wait(min(max(wait, 0.01), 0.5))
                continue
            if not self._has_work():
                # 无活可干时用队列的阻塞取等待，避免空转；取到的条目进待发缓冲。
                item = self.queue.get(timeout=0.2)
                if item is not None:
                    with self._lock:
                        self._pending.append(item)
                continue
            self.send_once()

    def _has_work(self) -> bool:
        with self._lock:
            if self._pending:
                return True
        return self.queue.qsize() > 0

    # -- 观测 ---------------------------------------------------------- #
    def status(self) -> dict:
        target = self._resolve_target()
        endpoint = self._connect_endpoint(target) if target else None
        with self._lock:
            return {
                "target": list(target) if target else None,
                "endpoint": list(endpoint) if endpoint else None,
                "pending": len(self._pending),
                "queued": self.queue.qsize(),
                "sent": self._sent,
                "bytes_sent": self._bytes_sent,
                "failures": self._failures,
                "backoff": self._backoff,
                "backoff_next": self._next_backoff,
                "redirections": self._redirections,
                "connected": self._connected,
                "last_error": self._last_error,
                "last_success_at": self._last_success_at,
                "running": bool(self._thread and self._thread.is_alive()),
            }