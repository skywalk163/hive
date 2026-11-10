"""Worker ↔ serve 的 Unix socket 通道（T-0309）。

对应 ``docs/控制面v0.5-实现约定.md`` §2.3 / §3.8 与 D10：

- Worker 上报只做内存入队，立即返回（I6 / G1）；真正的 connect/send 在后台线程；
- serve → Worker 的通知（``install`` / ``expiring``）用**非阻塞 send**，
  遇到 ``EAGAIN`` / ``EWOULDBLOCK`` / ``BrokenPipe`` 直接丢弃并计数（G2），
  绝不因为某个 Worker 不读而卡住 serve；
- 每次连接首行 ``{"type":"worker","agent":...}`` 注册，之后 ``entry`` 信封
  交给 ``on_entry(entry, agent)``；坏行跳过；
- POSIX 路径 ``/tmp/hive-control.sock``，Windows ``%TEMP%\\hive-control.sock``，
  由 :class:`hive_control.config.Limits` 的 ``socket_path`` 提供，构造时可传测试路径。
"""

from __future__ import annotations

import os
import select
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .config import Limits
from .model import StateEntry
from .wire import encode_line, entry_from_message, parse_line

__all__ = ["WorkerServer", "WorkerClient"]

_ACCEPT_TIMEOUT = 0.2
_READ_CHUNK = 65536
_DEFAULT_RECONNECT = 0.2


class _Connection:
    """服务端侧的一条 Worker 连接。"""

    __slots__ = ("sock", "agent", "buffer", "send_lock", "alive")

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.agent = ""
        self.buffer = b""
        self.send_lock = threading.Lock()
        self.alive = True


class WorkerServer:
    """接收 Worker 上报、向指定 agent 推送通知的 Unix socket 服务端。"""

    def __init__(
        self,
        path: str,
        on_entry: Callable[[StateEntry, str], None],
        *,
        limits: Limits | None = None,
    ):
        self.path = str(path)
        self.on_entry = on_entry
        self.limits = limits or Limits()
        self._lock = threading.RLock()
        self._conns: set[_Connection] = set()
        self._by_agent: dict[str, set[_Connection]] = {}
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._counters = {
            "entries": 0,
            "bad_lines": 0,
            "notify_sent": 0,
            "notify_dropped": 0,
            "connections": 0,
        }

    # -- 生命周期 ------------------------------------------------------ #
    def start(self) -> None:
        """绑定并开始接收连接；重复调用幂等。"""
        with self._lock:
            if self._accept_thread is not None and self._accept_thread.is_alive():
                return
            self._stop.clear()
            self._unlink()
            parent = Path(self.path).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.settimeout(_ACCEPT_TIMEOUT)
            listener.bind(self.path)
            listener.listen(128)
            self._listener = listener
            thread = threading.Thread(
                target=self._accept_loop, name="hive-worker-sock", daemon=True)
            self._accept_thread = thread
        thread.start()

    def stop(self) -> None:
        """停止接收、关闭所有连接并清理 socket 文件；幂等。"""
        with self._lock:
            if self._stop.is_set() and self._listener is None:
                return
            self._stop.set()
            listener = self._listener
            self._listener = None
            conns = list(self._conns)
            self._conns.clear()
            self._by_agent.clear()
            thread = self._accept_thread
            self._accept_thread = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        for conn in conns:
            self._close_conn(conn)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._unlink()

    # -- 通知 ---------------------------------------------------------- #
    def notify(self, owner_agent: str, message: dict) -> int:
        """只投递给该 agent 的连接；非阻塞，满则丢弃，返回成功连接数。"""
        data = encode_line(message)
        with self._lock:
            targets = list(self._by_agent.get(owner_agent, ()))
        sent = 0
        dropped = 0
        dead: list[_Connection] = []
        for conn in targets:
            if not conn.alive:
                continue
            with conn.send_lock:
                try:
                    written = conn.sock.send(data)
                except (BlockingIOError, InterruptedError):
                    dropped += 1
                    continue
                except (BrokenPipeError, ConnectionResetError, OSError):
                    dropped += 1
                    dead.append(conn)
                    continue
            if written == len(data):
                sent += 1
            else:
                # 半包会破坏行协议：丢弃剩余部分并断开这条连接。
                dropped += 1
                dead.append(conn)
        with self._lock:
            self._counters["notify_sent"] += sent
            self._counters["notify_dropped"] += dropped
        for conn in dead:
            self._close_conn(conn)
        return sent

    def status(self) -> dict:
        with self._lock:
            agents = sorted(a for a, conns in self._by_agent.items() if conns)
            out = {
                "path": self.path,
                "running": self._listener is not None,
                "connections": len(self._conns),
                "workers": agents,
            }
            out.update(self._counters)
            return out

    # -- 内部 ---------------------------------------------------------- #
    def _unlink(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            try:
                sock.setblocking(False)
            except OSError:
                sock.close()
                continue
            conn = _Connection(sock)
            with self._lock:
                self._conns.add(conn)
                self._counters["connections"] += 1
            threading.Thread(target=self._read_loop, args=(conn,),
                             name="hive-worker-conn", daemon=True).start()

    def _read_loop(self, conn: _Connection) -> None:
        try:
            while not self._stop.is_set() and conn.alive:
                try:
                    readable, _, _ = select.select([conn.sock], [], [], _ACCEPT_TIMEOUT)
                except (OSError, ValueError):
                    break
                if not readable:
                    continue
                try:
                    chunk = conn.sock.recv(_READ_CHUNK)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                conn.buffer += chunk
                while b"\n" in conn.buffer:
                    raw, conn.buffer = conn.buffer.split(b"\n", 1)
                    self._handle_line(conn, raw)
        finally:
            self._close_conn(conn)

    def _handle_line(self, conn: _Connection, raw: bytes) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            message = parse_line(line)
        except (ValueError, UnicodeDecodeError):
            with self._lock:
                self._counters["bad_lines"] += 1
            return
        if not conn.agent:
            if (message.get("type") == "worker"
                    and isinstance(message.get("agent"), str)
                    and message["agent"]):
                conn.agent = message["agent"]
                with self._lock:
                    self._by_agent.setdefault(conn.agent, set()).add(conn)
                return
            with self._lock:
                self._counters["bad_lines"] += 1
            return
        entry = entry_from_message(message)
        if entry is None:
            with self._lock:
                self._counters["bad_lines"] += 1
            return
        with self._lock:
            self._counters["entries"] += 1
        try:
            self.on_entry(entry, conn.agent)
        except Exception:  # noqa: BLE001  单个条目处理失败不拖垮读线程
            pass

    def _close_conn(self, conn: _Connection) -> None:
        with self._lock:
            if not conn.alive:
                return
            conn.alive = False
            self._conns.discard(conn)
            if conn.agent:
                conns = self._by_agent.get(conn.agent)
                if conns is not None:
                    conns.discard(conn)
                    if not conns:
                        self._by_agent.pop(conn.agent, None)
        with conn.send_lock:
            try:
                conn.sock.close()
            except OSError:
                pass


class WorkerClient:
    """Worker 侧的 socket 客户端：上报只入内存队列，后台线程负责发送。"""

    def __init__(
        self,
        agent: str,
        path: str,
        *,
        on_message: Callable[[dict], None] | None = None,
        limits: Limits | None = None,
        reconnect_interval: float = _DEFAULT_RECONNECT,
    ):
        if not agent:
            raise ValueError("agent 不能为空")
        self.agent = str(agent)
        self.path = str(path)
        self.on_message = on_message
        self.limits = limits or Limits()
        self.reconnect_interval = max(0.001, float(reconnect_interval))
        self._capacity = max(1, int(self.limits.worker_queue_capacity))
        self._cond = threading.Condition(threading.Lock())
        self._queue: list[StateEntry] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._counter = {
            "sent": 0,
            "dropped_steady": 0,
            "dropped_transient": 0,
            "messages": 0,
            "reconnects": 0,
        }
        self._connected = False

    # -- 上报（必须立即返回） ------------------------------------------ #
    def report(self, entry: StateEntry) -> None:
        """只做内存入队；满时优先丢最旧的 steady（G3），绝不阻塞调用方。"""
        with self._cond:
            if len(self._queue) >= self._capacity:
                self._drop_one_locked()
            self._queue.append(entry)
            self._cond.notify()

    def _drop_one_locked(self) -> None:
        for i, item in enumerate(self._queue):
            if item.state == "steady":
                del self._queue[i]
                self._counter["dropped_steady"] += 1
                return
        if self._queue:
            self._queue.pop(0)
            self._counter["dropped_transient"] += 1

    # -- 生命周期 ------------------------------------------------------ #
    def start(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run, name="hive-worker-client", daemon=True)
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        """停止后台线程，退出前尽量把剩余上报刷出去；幂等。"""
        with self._cond:
            if self._stop.is_set() and self._thread is None:
                return
            self._stop.set()
            thread = self._thread
            self._thread = None
            sock = self._sock
            self._cond.notify_all()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._flush_once()

    def status(self) -> dict:
        with self._cond:
            out = {
                "agent": self.agent,
                "path": self.path,
                "queue": len(self._queue),
                "connected": self._connected,
            }
            out.update(self._counter)
            return out

    # -- 后台线程 ------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop.is_set():
            sock = self._connect()
            if sock is None:
                self._stop.wait(self.reconnect_interval)
                continue
            try:
                self._pump(sock)
            except (OSError, ConnectionError):
                pass
            finally:
                self._close_sock()
        self._close_sock()

    def _connect(self) -> socket.socket | None:
        if self._stop.is_set():
            return None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(self.path)
            sock.sendall(encode_line({
                "type": "worker", "agent": self.agent, "pid": os.getpid()}))
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            return None
        with self._cond:
            self._sock = sock
            self._connected = True
            self._counter["reconnects"] += 1
        return sock

    def _pump(self, sock: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            entry = self._pop_entry()
            if entry is not None:
                try:
                    sock.sendall(encode_line(
                        {"type": "entry", "entry": entry.to_dict()}))
                except OSError:
                    self._requeue(entry)
                    raise
                with self._cond:
                    self._counter["sent"] += 1
                continue
            try:
                readable, _, _ = select.select([sock], [], [], 0.05)
            except (OSError, ValueError):
                raise ConnectionError("select 失败")
            if not readable:
                continue
            try:
                chunk = sock.recv(_READ_CHUNK)
            except socket.timeout:
                continue
            except OSError as exc:
                raise ConnectionError(str(exc)) from exc
            if not chunk:
                raise ConnectionError("服务端关闭连接")
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self._dispatch(raw)

    def _dispatch(self, raw: bytes) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            message = parse_line(line)
        except (ValueError, UnicodeDecodeError):
            return
        with self._cond:
            self._counter["messages"] += 1
        if self.on_message is not None:
            try:
                self.on_message(message)
            except Exception:  # noqa: BLE001  回调异常不影响后台发送
                pass

    def _pop_entry(self) -> StateEntry | None:
        with self._cond:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def _requeue(self, entry: StateEntry) -> None:
        with self._cond:
            if len(self._queue) >= self._capacity:
                self._drop_one_locked()
            self._queue.insert(0, entry)

    def _close_sock(self) -> None:
        with self._cond:
            sock = self._sock
            self._sock = None
            self._connected = False
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass

    def _flush_once(self) -> None:
        """stop 时的一次性尽力发送（短超时；失败就放弃，不阻塞调用方太久）。"""
        with self._cond:
            pending = list(self._queue)
            self._queue.clear()
        if not pending:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        try:
            sock.connect(self.path)
            sock.sendall(encode_line({
                "type": "worker", "agent": self.agent, "pid": os.getpid()}))
            for entry in pending:
                sock.sendall(encode_line(
                    {"type": "entry", "entry": entry.to_dict()}))
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
