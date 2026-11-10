"""T-0309：Worker ↔ serve Unix socket 的确定性单测。"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from hive_control import Limits
from hive_control.model import StateEntry
from hive_control.wire import encode_line
from hive_control.worker_sock import WorkerClient, WorkerServer

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="平台不支持 AF_UNIX")


def entry(tid: str = "T-1", *, state: str = "steady",
          owner: str = "alice") -> StateEntry:
    return StateEntry(
        project="p",
        tid=tid,
        version=1,
        incarnation=1,
        owner_agent=owner,
        state=state,
        dna=False,
        updated_at=0,
    )


def wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


@pytest.fixture()
def sock_path():
    """tmp_path 下的 socket；路径过长（AF_UNIX 104 字节限制）时退到短临时目录。"""
    base = Path(tempfile.mkdtemp(prefix="hive-sock-"))
    path = base / "s.sock"
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


def test_report_reaches_server_with_agent(sock_path):
    received: list[tuple[StateEntry, str]] = []
    got = threading.Event()

    def on_entry(item: StateEntry, agent: str) -> None:
        received.append((item, agent))
        got.set()

    server = WorkerServer(sock_path, on_entry)
    server.start()
    client = WorkerClient("alice", sock_path)
    client.start()
    try:
        assert wait_until(lambda: "alice" in server.status()["workers"])
        client.report(entry("T-100"))
        assert got.wait(3.0), "服务端没有收到上报"
        item, agent = received[0]
        assert agent == "alice"
        assert item.tid == "T-100"
        assert server.status()["entries"] == 1
    finally:
        client.stop()
        server.stop()


def test_notify_only_reaches_target_agent(sock_path):
    alice_msgs: list[dict] = []
    bob_msgs: list[dict] = []
    alice_got = threading.Event()

    def on_alice(message: dict) -> None:
        alice_msgs.append(message)
        alice_got.set()

    server = WorkerServer(sock_path, lambda item, agent: None)
    server.start()
    alice = WorkerClient("alice", sock_path, on_message=on_alice)
    bob = WorkerClient("bob", sock_path, on_message=bob_msgs.append)
    alice.start()
    bob.start()
    try:
        assert wait_until(
            lambda: {"alice", "bob"} <= set(server.status()["workers"]))
        message = {"type": "install", "entry": entry("T-200").to_dict()}
        assert server.notify("alice", message) == 1
        assert alice_got.wait(3.0), "alice 没有收到通知"
        time.sleep(0.1)
        assert alice_msgs and alice_msgs[0]["type"] == "install"
        assert bob_msgs == []
        assert server.notify("nobody", message) == 0
    finally:
        alice.stop()
        bob.stop()
        server.stop()


def test_notify_drops_instead_of_blocking_when_client_stuck(sock_path):
    server = WorkerServer(sock_path, lambda item, agent: None)
    server.start()
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        raw.connect(sock_path)
        raw.sendall(encode_line({"type": "worker", "agent": "stuck", "pid": 1}))
        assert wait_until(lambda: "stuck" in server.status()["workers"])

        big = {"type": "install",
               "entry": entry("T-300").to_dict(),
               "pad": "x" * 65536}
        started = time.monotonic()
        for _ in range(300):
            server.notify("stuck", big)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"notify 被阻塞了 {elapsed:.3f}s"
        assert server.status()["notify_dropped"] > 0
    finally:
        raw.close()
        server.stop()


def test_report_is_fast(sock_path):
    client = WorkerClient("alice", sock_path)
    count = 1000
    started = time.monotonic()
    for i in range(count):
        client.report(entry(f"T-{i}"))
    elapsed = time.monotonic() - started
    assert elapsed / count < 0.001, f"平均 {elapsed / count * 1000:.3f}ms/次"


def test_client_reconnects_after_server_restart(sock_path):
    first: list[StateEntry] = []
    second: list[StateEntry] = []
    got_first = threading.Event()
    got_second = threading.Event()

    def collect(bucket: list[StateEntry], signal: threading.Event):
        def on_entry(item: StateEntry, agent: str) -> None:
            bucket.append(item)
            signal.set()
        return on_entry

    server1 = WorkerServer(sock_path, collect(first, got_first))
    server1.start()
    client = WorkerClient("alice", sock_path, reconnect_interval=0.02)
    client.start()
    server2: WorkerServer | None = None
    try:
        assert wait_until(lambda: "alice" in server1.status()["workers"])
        client.report(entry("T-400"))
        assert got_first.wait(3.0)

        server2 = WorkerServer(sock_path, collect(second, got_second))
        server1.stop()
        server2.start()

        client.report(entry("T-401"))
        assert got_second.wait(5.0), "服务端重启后客户端没有自动重连"
        assert [item.tid for item in second] == ["T-401"]
    finally:
        client.stop()
        server1.stop()
        if server2 is not None:
            server2.stop()


def test_stop_removes_socket_file_and_is_idempotent(sock_path):
    server = WorkerServer(sock_path, lambda item, agent: None)
    server.start()
    assert Path(sock_path).exists()
    server.stop()
    assert not Path(sock_path).exists()
    server.stop()
    assert not Path(sock_path).exists()


def test_bad_lines_are_skipped(sock_path):
    received: list[StateEntry] = []
    got = threading.Event()

    def on_entry(item: StateEntry, agent: str) -> None:
        received.append(item)
        got.set()

    server = WorkerServer(sock_path, on_entry)
    server.start()
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        raw.connect(sock_path)
        raw.sendall(b"not json\n")
        raw.sendall(encode_line({"type": "worker", "agent": "alice", "pid": 1}))
        raw.sendall(b"{broken\n")
        raw.sendall(encode_line(
            {"type": "entry", "entry": entry("T-500").to_dict()}))
        assert got.wait(3.0)
        assert [item.tid for item in received] == ["T-500"]
        assert server.status()["bad_lines"] == 2
    finally:
        raw.close()
        server.stop()


def test_limits_control_client_queue_capacity(sock_path):
    limits = Limits().overridden(worker_queue_capacity=2)
    client = WorkerClient("alice", sock_path, limits=limits)
    client.report(entry("steady-1"))
    client.report(entry("steady-2"))
    client.report(entry("steady-3"))
    status = client.status()
    assert status["queue"] == 2
    assert status["dropped_steady"] == 1
