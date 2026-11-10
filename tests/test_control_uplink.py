"""T-0305：控制面上行 TCP 发送器的确定性单测。

覆盖母文档 §5.1 / 实现约定 §3.4：

- 目标 = Leader 的 ``port+1``（``limits.uplink_port_offset``）；
- 只发 ``entry`` 行；
- 失败指数退避 500ms → 翻倍 → 上限 10s，成功清零；
- 切主 ``set_target`` 后待发条目原样继续发，不丢队列；
- 无 Leader / 退避中不取空队列（不阻塞上报路径）。

时间用注入时钟，连接用假 connector 或临时端口的环回 socket，不 sleep 等真实秒数。
"""

from __future__ import annotations

import socket
import threading
import time

from hive_control.config import Limits
from hive_control.model import StateEntry
from hive_control.uplink import UplinkSender
from hive_control.uplink_queue import UplinkQueue
from hive_control.wire import entry_from_message, iter_lines, parse_line


def entry(tid: str, *, version: int = 1, incarnation: int = 1,
          state: str = "steady", project: str = "p") -> StateEntry:
    return StateEntry(
        project=project,
        tid=tid,
        version=version,
        incarnation=incarnation,
        owner_agent="agent",
        state=state,
        dna=False,
        updated_at=0,
    )


class Clock:
    """可注入时钟：单测不靠真实秒数。"""

    def __init__(self, now: float = 0.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class FakeSocket:
    def __init__(self, sink: list[bytes]):
        self._sink = sink
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self._sink.append(bytes(data))

    def shutdown(self, how) -> None:  # noqa: ARG002 —— 假 socket 不需要真半关
        pass

    def close(self) -> None:
        self.closed = True


class FakeConnector:
    """按 (host, port) 记录调用；``fail_ports`` 里的端口抛连接错误。"""

    def __init__(self, fail_ports=()):
        self.calls: list[tuple[str, int]] = []
        self.fail_ports = set(int(p) for p in fail_ports)
        self.buffers: dict[tuple[str, int], list[bytes]] = {}
        self.sockets: list[FakeSocket] = []

    def __call__(self, host: str, port: int) -> FakeSocket:
        self.calls.append((str(host), int(port)))
        if int(port) in self.fail_ports:
            raise ConnectionRefusedError(f"refused {port}")
        # 同一端口可能分多批连接，字节按端口累加，便于断言“一条不丢”
        buf = self.buffers.setdefault((str(host), int(port)), [])
        sock = FakeSocket(buf)
        self.sockets.append(sock)
        return sock

    def lines(self, host: str, port: int) -> list[dict]:
        raw = b"".join(self.buffers.get((str(host), int(port)), []))
        return [parse_line(line) for line in iter_lines(raw)]

    def entries(self, host: str, port: int) -> list[StateEntry]:
        out = []
        for message in self.lines(host, port):
            parsed = entry_from_message(message)
            assert parsed is not None, message
            out.append(parsed)
        return out


def make_sender(queue=None, target=None, *, clock=None, connector=None,
                limits=None, **kwargs):
    queue = queue if queue is not None else UplinkQueue()
    clock = clock if clock is not None else Clock()
    sender = UplinkSender(
        queue,
        target if target is not None else (lambda: ("127.0.0.1", 8788)),
        limits=limits or Limits(),
        clock=clock,
        connector=connector or FakeConnector(),
        **kwargs,
    )
    return sender, queue, clock


# -- 目标端口与线格式 ---------------------------------------------------- #

def test_send_once_unicasts_to_leader_port_plus_one():
    connector = FakeConnector()
    sender, queue, _ = make_sender(connector=connector)
    for tid in ("a", "b", "c"):
        queue.put(entry(tid))

    assert sender.send_once() is True

    # Leader 的 HTTP 端口 8788 → 上行 TCP 单播 8789（port+1）
    assert connector.calls == [("127.0.0.1", 8789)]
    messages = connector.lines("127.0.0.1", 8789)
    assert [m["type"] for m in messages] == ["entry", "entry", "entry"]
    assert [m["entry"]["tid"] for m in messages] == ["a", "b", "c"]
    # 每一行都能按线格式还原成 StateEntry
    parsed = connector.entries("127.0.0.1", 8789)
    assert [e.tid for e in parsed] == ["a", "b", "c"]

    status = sender.status()
    assert status["sent"] == 3
    assert status["failures"] == 0
    assert status["pending"] == 0
    assert status["queued"] == 0
    assert status["target"] == ["127.0.0.1", 8788]
    assert status["endpoint"] == ["127.0.0.1", 8789]


def test_set_target_applies_port_offset_and_counts_redirection():
    connector = FakeConnector()
    sender, queue, _ = make_sender(connector=connector)
    sender.set_target("10.0.0.2", 9000)
    sender.set_target("10.0.0.2", 9000)  # 同一目标不重复计重定向
    queue.put(entry("x"))

    assert sender.send_once() is True
    assert connector.calls == [("10.0.0.2", 9001)]
    assert sender.status()["redirections"] == 1


def test_port_offset_follows_limits():
    connector = FakeConnector()
    sender, queue, _ = make_sender(
        connector=connector, limits=Limits(uplink_port_offset=3))
    queue.put(entry("x"))
    assert sender.send_once() is True
    assert connector.calls == [("127.0.0.1", 8791)]


def test_send_once_returns_false_when_queue_empty():
    sender, _, _ = make_sender()
    assert sender.send_once() is False
    assert sender.status()["sent"] == 0


def test_no_target_leaves_entries_in_queue():
    sender, queue, _ = make_sender(target=lambda: None)
    queue.put(entry("x"))

    assert sender.send_once() is False
    assert queue.qsize() == 1          # 没 Leader 时不取空队列
    assert sender.status()["pending"] == 0
    assert sender.status()["last_error"] == "no leader target"


# -- 指数退避 ------------------------------------------------------------ #

def test_backoff_doubles_from_500ms_and_caps_at_10s():
    clock = Clock()
    connector = FakeConnector(fail_ports={8789})
    sender, queue, _ = make_sender(clock=clock, connector=connector)
    queue.put(entry("x"))

    assert sender.send_once() is False
    assert sender.status()["failures"] == 1
    assert sender.status()["backoff"] == 0.5
    assert sender.status()["pending"] == 1       # 失败不丢条目
    assert queue.qsize() == 0

    delays = [0.5]
    for _ in range(6):
        current = sender.status()["backoff"]
        clock.advance(current)
        assert sender.send_once() is False
        delays.append(sender.status()["backoff"])

    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    assert sender.status()["failures"] == 7
    # 上限之后不再增长
    clock.advance(10.0)
    assert sender.send_once() is False
    assert sender.status()["backoff"] == 10.0


def test_backoff_gate_blocks_retry_before_deadline():
    clock = Clock()
    connector = FakeConnector(fail_ports={8789})
    sender, queue, _ = make_sender(clock=clock, connector=connector)
    queue.put(entry("x"))
    assert sender.send_once() is False
    assert len(connector.calls) == 1

    clock.advance(0.49)
    assert sender.send_once() is False
    assert len(connector.calls) == 1       # 窗口内不重连

    clock.advance(0.01)
    assert sender.send_once() is False
    assert len(connector.calls) == 2


def test_success_resets_backoff_and_failures():
    clock = Clock()
    connector = FakeConnector(fail_ports={8789})
    sender, queue, _ = make_sender(clock=clock, connector=connector)
    queue.put(entry("x"))
    assert sender.send_once() is False
    clock.advance(0.5)
    assert sender.send_once() is False
    assert sender.status()["backoff"] == 1.0

    connector.fail_ports.clear()
    clock.advance(1.0)
    assert sender.send_once() is True

    status = sender.status()
    assert status["sent"] == 1
    assert status["failures"] == 0
    assert status["backoff"] == 0.0
    assert status["backoff_next"] == 0.5
    assert status["pending"] == 0
    assert connector.entries("127.0.0.1", 8789)[0].tid == "x"


# -- 切主重定向 / 不丢队列 ----------------------------------------------- #

def test_set_target_redirects_pending_entries_to_new_leader():
    clock = Clock()
    connector = FakeConnector(fail_ports={8789})
    sender, queue, _ = make_sender(clock=clock, connector=connector)
    sender.set_target("127.0.0.1", 8788)
    queue.put(entry("a"))
    queue.put(entry("b"))

    assert sender.send_once() is False
    assert sender.status()["pending"] == 2

    # 切主：新 Leader 8600 → 上行 8601；旧退避被清零，立即可以发
    sender.set_target("127.0.0.1", 8600)
    assert sender.status()["backoff"] == 0.0
    assert sender.send_once() is True

    assert connector.calls[-1] == ("127.0.0.1", 8601)
    parsed = connector.entries("127.0.0.1", 8601)
    assert [e.tid for e in parsed] == ["a", "b"]   # 一条不丢、顺序不变
    assert sender.status()["pending"] == 0
    assert sender.status()["sent"] == 2


def test_failure_then_redirect_keeps_queue_untouched_entries():
    connector = FakeConnector(fail_ports={8789})
    sender, queue, _ = make_sender(connector=connector)
    queue.put(entry("a"))
    assert sender.send_once() is False

    # 失败后新上报仍进队列，不被发送器的待发缓冲吞掉
    queue.put(entry("b"))
    assert queue.qsize() == 1
    sender.set_target("127.0.0.1", 8700)
    assert sender.send_once() is True
    assert [e.tid for e in connector.entries("127.0.0.1", 8701)] == ["a", "b"]


def test_partial_send_keeps_unsent_entries():
    """一批发送中途断链：已发出的出队，剩下的留待下次（不丢、不重发）。"""

    class FlakyConnector:
        def __init__(self):
            self.calls: list[tuple[str, int]] = []
            self.buffers: dict[tuple[str, int], list[bytes]] = {}
            self.mode = "refuse"       # refuse → half（发一条就断）→ ok

        def __call__(self, host, port):
            self.calls.append((str(host), int(port)))
            if self.mode == "refuse":
                raise ConnectionRefusedError("leader down")
            buf = self.buffers.setdefault((str(host), int(port)), [])
            if self.mode == "half":
                self.mode = "ok"

                class HalfSock(FakeSocket):
                    def sendall(self, data):
                        if buf:
                            raise BrokenPipeError("peer gone")
                        super().sendall(data)

                return HalfSock(buf)
            return FakeSocket(buf)

        def entries(self, host, port):
            raw = b"".join(self.buffers.get((str(host), int(port)), []))
            out = []
            for message in (parse_line(line) for line in iter_lines(raw)):
                parsed = entry_from_message(message)
                assert parsed is not None
                out.append(parsed)
            return out

    connector = FlakyConnector()
    sender, queue, _ = make_sender(connector=connector)
    queue.put(entry("a"))
    queue.put(entry("b"))

    assert sender.send_once() is False          # 第一次直接连不上
    assert sender.status()["pending"] == 2

    connector.mode = "half"
    sender.set_target("127.0.0.1", 8700)        # 清退避，立即重试
    assert sender.send_once() is False          # a 发出后断链
    assert sender.status()["pending"] == 1      # 只剩 b
    assert [e.tid for e in connector.entries("127.0.0.1", 8701)] == ["a"]

    sender.set_target("127.0.0.1", 8702)        # 再切一次，b 必须补发
    assert sender.send_once() is True
    assert [e.tid for e in connector.entries("127.0.0.1", 8703)] == ["b"]
    assert sender.status()["pending"] == 0
    assert sender.status()["sent"] == 2


# -- 真实 TCP 环回 ------------------------------------------------------- #

def test_real_tcp_roundtrip_on_ephemeral_port():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    received: list[dict] = []
    done = threading.Event()

    def serve():
        try:
            conn, _ = srv.accept()
            with conn:
                raw = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                received.extend(parse_line(line) for line in iter_lines(raw))
        finally:
            done.set()
            srv.close()

    server = threading.Thread(target=serve, daemon=True)
    server.start()

    queue = UplinkQueue()
    sender = UplinkSender(
        queue,
        lambda: (host, port),
        limits=Limits(uplink_port_offset=0),
        clock=Clock(),
    )
    queue.put(entry("a"))
    queue.put(entry("b"))
    try:
        assert sender.send_once() is True
    finally:
        done.wait(2.0)
        server.join(2.0)

    assert [m["type"] for m in received] == ["entry", "entry"]
    assert [entry_from_message(m).tid for m in received] == ["a", "b"]


# -- 后台线程 ------------------------------------------------------------ #

def test_background_thread_sends_and_stop_is_idempotent():
    connector = FakeConnector()
    sender, queue, _ = make_sender(connector=connector)
    sender.start()
    sender.start()  # 幂等
    try:
        queue.put(entry("a"))
        queue.put(entry("b"))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(connector.buffers) == 0:
            time.sleep(0.01)
        while time.monotonic() < deadline:
            if sender.status()["sent"] >= 2:
                break
            time.sleep(0.01)
    finally:
        sender.stop()
        sender.stop()  # 幂等

    parsed = connector.entries("127.0.0.1", 8789)
    assert [e.tid for e in parsed] == ["a", "b"]
    assert sender.status()["running"] is False