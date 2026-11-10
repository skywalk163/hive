"""控制面拉取：增量水位、全量分页、配额、TCP 服务端与客户端。

对应母文档 §6 与实现约定 §2.4 / §3.5，裁定 D3（逐条水位）、D15（人工拉取
走独立队列，不在这里实现队列本身，只提供可被队列调用的拉取原语）。

TCP 监听与上行共用一个端口：每条连接读首行，``type=="pull"`` 交给
``PullService``，其余行按状态条目交给 ``on_entries``。
"""

from __future__ import annotations

import math
import socket
import threading
import time
from collections import deque
from typing import Any, Callable, Mapping

from .config import Limits
from .model import StateEntry, version_key
from .wire import encode_line, entry_from_message, message_type, parse_line

__all__ = [
    "PullRetry",
    "PullService",
    "ControlTcpServer",
    "PullClient",
]


class PullRetry(Exception):
    """配额超限：调用方应在 ``after_ms`` 毫秒后重试。"""

    def __init__(self, after_ms: int):
        self.after_ms = int(after_ms)
        super().__init__(f"pull quota exceeded, retry after {self.after_ms}ms")


def _pair(value: Any) -> tuple[int, int] | None:
    """把 ``from`` / 水位值归一成 ``(incarnation, version)``。"""
    if value is None:
        return None
    if isinstance(value, Mapping):
        inc, ver = value.get("incarnation"), value.get("version")
        if inc is None or ver is None:
            return None
        value = (inc, ver)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _entry_row(entry: StateEntry) -> dict:
    return {"type": "entry", "entry": entry.to_dict()}


class PullService:
    """拉取协议服务端逻辑（无网络、可注入时钟，便于单测）。"""

    def __init__(self, view: Callable[[], Any], *, limits: Limits,
                 clock: Callable[[], float] = time.monotonic):
        self.view = view
        self.limits = limits
        self.clock = clock
        self._lock = threading.Lock()
        # project -> 已接受的增量请求时间（滑动 1s 窗口）
        self._incremental_stamps: dict[str, deque[float]] = {}
        # project -> 已接受的全量请求时间（滑动 60s 窗口）
        self._full_stamps: dict[str, deque[float]] = {}
        self._counters = {
            "incremental": 0,
            "full": 0,
            "retries": 0,
            "errors": 0,
            "entries": 0,
        }

    # -- 内部 ---------------------------------------------------------- #
    def _resolve_view(self):
        view = self.view
        if callable(view) and not hasattr(view, "entries"):
            view = view()
        return view

    def _entries(self, project: str) -> list[StateEntry]:
        view = self._resolve_view()
        items = view.entries(project)
        return list(items or [])

    def _error(self, message: str) -> list[dict]:
        with self._lock:
            self._counters["errors"] += 1
        return [{"type": "error", "error": message}]

    def _check_incremental_quota(self, project: str) -> int:
        """返回 0 表示放行；否则返回建议的 ``after_ms``。"""
        now = self.clock()
        window = 1.0
        limit = max(1, int(self.limits.pull_incremental_per_sec))
        with self._lock:
            stamps = self._incremental_stamps.setdefault(project, deque())
            while stamps and now - stamps[0] >= window:
                stamps.popleft()
            if len(stamps) >= limit:
                wait = stamps[0] + window - now
                return max(1, int(math.ceil(wait * 1000)))
            stamps.append(now)
            self._counters["incremental"] += 1
        return 0

    def _check_full_quota(self, project: str) -> int:
        now = self.clock()
        window = 60.0
        limit = max(1, int(self.limits.pull_full_per_minute))
        # 当前常量下 limit 固定为 1；写成通用滑动窗口便于测试覆盖。
        with self._lock:
            stamps = self._full_stamps.setdefault(project, deque())
            while stamps and now - stamps[0] >= window:
                stamps.popleft()
            if len(stamps) >= limit:
                wait = stamps[0] + window - now
                return max(1, int(math.ceil(wait * 1000)))
            stamps.append(now)
            self._counters["full"] += 1
        return 0

    @staticmethod
    def _watermarks(request: dict) -> dict[str, tuple[int, int]]:
        raw = request.get("watermarks")
        out: dict[str, tuple[int, int]] = {}
        if isinstance(raw, Mapping):
            for tid, value in raw.items():
                pair = _pair(value)
                if pair is not None:
                    out[str(tid)] = pair
        return out

    def _incremental(self, project: str, request: dict) -> list[dict]:
        watermarks = self._watermarks(request)
        floor = _pair(request.get("from"))
        rows: list[StateEntry] = []
        for entry in self._entries(project):
            bound = watermarks.get(entry.tid) if watermarks else None
            if bound is None:
                bound = floor
            # DNA 条目同样返回（D6）；水位是严格大于。
            if bound is None or version_key(entry) > bound:
                rows.append(entry)
        rows.sort(key=lambda e: (e.project, e.tid))
        with self._lock:
            self._counters["entries"] += len(rows)
        return [_entry_row(entry) for entry in rows]

    def _full(self, project: str) -> list[dict]:
        rows = sorted(self._entries(project), key=lambda e: (e.project, e.tid))
        page_size = max(1, int(self.limits.pull_page_size))
        total_pages = max(1, int(math.ceil(len(rows) / page_size)))
        out: list[dict] = []
        for page in range(1, total_pages + 1):
            out.append({"type": "page", "page": page,
                        "total_pages": total_pages})
            chunk = rows[(page - 1) * page_size:page * page_size]
            out.extend(_entry_row(entry) for entry in chunk)
        with self._lock:
            self._counters["entries"] += len(rows)
        return out

    # -- 公开 API ------------------------------------------------------ #
    def handle(self, request: dict) -> list[dict]:
        """处理一条拉取请求，返回响应行（不含结尾空行）。"""
        if not isinstance(request, Mapping):
            return self._error("拉取请求必须是 JSON 对象")
        project = str(request.get("project") or "").strip()
        if not project:
            return self._error("缺少 project")
        mode = str(request.get("mode") or "").strip().lower()
        if mode not in ("incremental", "full"):
            return self._error(f"未知拉取模式 {mode!r}")

        if mode == "incremental":
            wait = self._check_incremental_quota(project)
            if wait:
                with self._lock:
                    self._counters["retries"] += 1
                return [{"type": "retry", "after_ms": wait}]
            return self._incremental(project, dict(request))

        wait = self._check_full_quota(project)
        if wait:
            with self._lock:
                self._counters["retries"] += 1
            return [{"type": "retry", "after_ms": wait}]
        return self._full(project)

    def status(self) -> dict:
        with self._lock:
            return {
                "requests": {
                    "incremental": self._counters["incremental"],
                    "full": self._counters["full"],
                },
                "retries": self._counters["retries"],
                "errors": self._counters["errors"],
                "entries": self._counters["entries"],
                "projects": {
                    "incremental": len(self._incremental_stamps),
                    "full": len(self._full_stamps),
                },
            }


class ControlTcpServer:
    """上行 + 拉取共用端口的 TCP 服务端；每连接一个线程。"""

    def __init__(self, bind: str, port: int, *,
                 on_entries: Callable[[list[StateEntry]], None],
                 pull: PullService, limits: Limits):
        self.bind = bind
        self._configured_port = int(port)
        self.on_entries = on_entries
        self.pull = pull
        self.limits = limits
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._clients: set[socket.socket] = set()
        self._client_threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._actual_port = self._configured_port
        self._bad_lines = 0
        self._connections = 0

    # -- 生命周期 ------------------------------------------------------ #
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, self._configured_port))
        sock.listen(64)
        sock.settimeout(0.2)
        self._sock = sock
        self._actual_port = int(sock.getsockname()[1])
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, name="control-tcp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients)
            threads = list(self._client_threads)
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        for thread in threads:
            thread.join(2.0)
        with self._lock:
            self._clients.clear()
            self._client_threads.clear()

    @property
    def port(self) -> int:
        return self._actual_port

    def status(self) -> dict:
        with self._lock:
            return {
                "bind": self.bind,
                "port": self._actual_port,
                "running": bool(self._sock is not None),
                "connections": self._connections,
                "bad_lines": self._bad_lines,
            }

    # -- 连接处理 ------------------------------------------------------ #
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._clients.add(conn)
                self._connections += 1
            thread = threading.Thread(target=self._serve, args=(conn,),
                                      name="control-tcp-conn", daemon=True)
            with self._lock:
                self._client_threads.add(thread)
            thread.start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(float(self.limits.tcp_idle_timeout))
            stream = conn.makefile("rb")
            try:
                while not self._stop.is_set():
                    try:
                        raw = stream.readline()
                    except (socket.timeout, TimeoutError):
                        break
                    except OSError:
                        break
                    if not raw:
                        break
                    if not raw.strip():
                        continue
                    try:
                        message = parse_line(raw)
                    except Exception:  # noqa: BLE001  坏行跳过，不崩连接
                        with self._lock:
                            self._bad_lines += 1
                        continue
                    if message_type(message) == "pull":
                        self._respond_pull(conn, message)
                        return
                    entry = entry_from_message(message)
                    if entry is None:
                        with self._lock:
                            self._bad_lines += 1
                        continue
                    try:
                        self.on_entries([entry])
                    except Exception:  # noqa: BLE001  回调异常不能打断连接
                        pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self._clients.discard(conn)
                self._client_threads.discard(threading.current_thread())

    def _respond_pull(self, conn: socket.socket, request: dict) -> None:
        try:
            rows = self.pull.handle(request)
        except Exception as exc:  # noqa: BLE001  服务端异常也走协议错误行
            rows = [{"type": "error", "error": str(exc)}]
        try:
            for row in rows:
                conn.sendall(encode_line(row))
            conn.sendall(b"\n")
        except OSError:
            pass


class PullClient:
    """拉取客户端：自动收全分页，遇配额重试抛 ``PullRetry``。"""

    def __init__(self, *, limits: Limits, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self.bad_lines = 0
        self.pages = 0

    def fetch(self, host: str, port: int, request: dict) -> list[StateEntry]:
        self.bad_lines = 0
        self.pages = 0
        result: list[StateEntry] = []
        with socket.create_connection(
                (host, int(port)),
                timeout=float(self.limits.tcp_idle_timeout)) as sock:
            sock.settimeout(float(self.limits.tcp_idle_timeout))
            sock.sendall(encode_line(request))
            stream = sock.makefile("rb")
            try:
                while True:
                    try:
                        raw = stream.readline()
                    except (socket.timeout, TimeoutError):
                        break
                    except OSError:
                        break
                    if not raw:
                        break
                    line = raw.strip()
                    if not line:
                        break
                    try:
                        message = parse_line(line)
                    except Exception:  # noqa: BLE001  坏行跳过并计数
                        self.bad_lines += 1
                        continue
                    kind = message_type(message)
                    if kind == "page":
                        self.pages += 1
                        continue
                    if kind == "retry":
                        try:
                            after_ms = int(message.get("after_ms") or 0)
                        except (TypeError, ValueError):
                            after_ms = 0
                        raise PullRetry(max(1, after_ms))
                    if kind == "error":
                        raise ValueError(str(message.get("error") or "pull error"))
                    entry = entry_from_message(message)
                    if entry is None:
                        self.bad_lines += 1
                        continue
                    result.append(entry)
            finally:
                try:
                    stream.close()
                except OSError:
                    pass
        return result
