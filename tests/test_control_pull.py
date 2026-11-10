"""T-0306：控制面拉取协议（增量水位 / 全量分页 / 配额 / TCP）单测。"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from hive_control.config import Limits
from hive_control.model import StateEntry
from hive_control.pull import (
    ControlTcpServer,
    PullClient,
    PullRetry,
    PullService,
)
from hive_control.wire import encode_line


class Clock:
    def __init__(self, now: float = 0.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class FakeView:
    """只实现 pull.py 需要的 ``entries(project)`` 读接口。"""

    def __init__(self, entries=()):
        self._entries = list(entries)

    def entries(self, project=None):
        if project is None:
            return list(self._entries)
        return [e for e in self._entries if e.project == project]


def entry(tid: str, *, project: str = "p", version: int = 1,
          incarnation: int = 1, state: str = "steady",
          dna: bool = False) -> StateEntry:
    return StateEntry(
        project=project,
        tid=tid,
        version=version,
        incarnation=incarnation,
        owner_agent="agent",
        state=state,
        dna=dna,
        updated_at=0,
    )


def service(view, clock=None, **limit_changes) -> PullService:
    return PullService(lambda: view, limits=Limits(**limit_changes),
                       clock=clock or Clock())


def server_for(view, received: list | None = None, **limit_changes):
    limits = Limits(**limit_changes)
    svc = PullService(lambda: view, limits=limits, clock=Clock())
    received = received if received is not None else []
    srv = ControlTcpServer(
        "127.0.0.1", 0,
        on_entries=lambda entries: received.extend(entries),
        pull=svc, limits=limits)
    return srv, svc, received, limits


def read_until_blank(sock: socket.socket) -> list[dict]:
    """读到空行为止；解析每行 JSON。"""
    out: list[dict] = []
    stream = sock.makefile("rb")
    try:
        while True:
            raw = stream.readline()
            if not raw:
                break
            if not raw.strip():
                break
            out.append(json.loads(raw))
    finally:
        stream.close()
    return out


# -- 全量分页 ---------------------------------------------------------- #

def test_full_pull_250_entries_three_pages_client_collects_all():
    view = FakeView(entry(f"t{i:03d}", version=1) for i in range(250))
    svc = service(view, pull_page_size=100)

    rows = svc.handle({"type": "pull", "project": "p",
                       "mode": "full", "from": None})
    headers = [r for r in rows if r["type"] == "page"]
    items = [r for r in rows if r["type"] == "entry"]

    assert len(headers) == 3
    assert [h["page"] for h in headers] == [1, 2, 3]
    assert all(h["total_pages"] == 3 for h in headers)
    assert len(items) == 250
    assert [r["entry"]["tid"] for r in items] == [f"t{i:03d}" for i in range(250)]


def test_full_pull_over_tcp_client_reassembles_pages():
    view = FakeView(entry(f"t{i:03d}") for i in range(250))
    srv, svc, _received, limits = server_for(view, pull_page_size=100)
    srv.start()
    try:
        client = PullClient(limits=limits, clock=Clock())
        got = client.fetch("127.0.0.1", srv.port, {
            "type": "pull", "project": "p", "mode": "full", "from": None,
        })
    finally:
        srv.stop()

    assert len(got) == 250
    assert client.pages == 3
    assert {e.tid for e in got} == {f"t{i:03d}" for i in range(250)}


# -- 增量水位 ---------------------------------------------------------- #

def test_incremental_per_tid_watermarks_only_newer_and_keeps_dna():
    view = FakeView([
        entry("a", version=5),
        entry("b", version=2),
        entry("c", version=3, dna=True),
    ])
    svc = service(view)

    rows = svc.handle({
        "type": "pull", "project": "p", "mode": "incremental",
        "watermarks": {"a": [1, 5], "b": [1, 1], "c": [1, 3]},
    })

    assert [r["entry"]["tid"] for r in rows] == ["b"]


def test_incremental_without_watermarks_uses_global_from_floor():
    view = FakeView([
        entry("a", version=5),
        entry("b", version=2),
        entry("c", version=3, dna=True),
    ])
    svc = service(view)

    rows = svc.handle({
        "type": "pull", "project": "p", "mode": "incremental",
        "from": [1, 2],
    })

    assert [r["entry"]["tid"] for r in rows] == ["a", "c"]
    assert any(r["entry"]["dna"] for r in rows)


def test_incremental_uses_higher_incarnation_even_with_lower_version():
    view = FakeView([
        entry("old", incarnation=1, version=99),
        entry("new", incarnation=2, version=0),
    ])
    svc = service(view)

    rows = svc.handle({
        "type": "pull", "project": "p", "mode": "incremental",
        "watermarks": {"old": [1, 99], "new": [1, 99]},
    })

    assert [r["entry"]["tid"] for r in rows] == ["new"]


# -- 配额 -------------------------------------------------------------- #

def test_incremental_quota_eleventh_request_retries_with_after_ms():
    svc = service(FakeView(), Clock())
    request = {"type": "pull", "project": "p", "mode": "incremental"}

    for _ in range(10):
        rows = svc.handle(request)
        assert rows == []

    retry = svc.handle(request)
    assert retry[0]["type"] == "retry"
    assert retry[0]["after_ms"] > 0


def test_full_quota_second_request_retries_and_other_project_independent():
    clock = Clock()
    svc = service(FakeView([entry("t")]), clock)
    request = {"type": "pull", "project": "p", "mode": "full", "from": None}

    first = svc.handle(request)
    assert first[0]["type"] == "page"

    retry = svc.handle(request)
    assert retry[0]["type"] == "retry"
    assert retry[0]["after_ms"] > 0

    # 另一个 project 不受影响
    other = svc.handle({"type": "pull", "project": "q",
                        "mode": "full", "from": None})
    assert other[0]["type"] == "page"

    # 窗口过后恢复
    clock.advance(60.0)
    again = svc.handle(request)
    assert again[0]["type"] == "page"


def test_incremental_quota_is_thread_safe():
    svc = service(FakeView(), Clock())
    request = {"type": "pull", "project": "p", "mode": "incremental"}
    outcome: list[bool] = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        rows = svc.handle(request)
        outcome.append(bool(rows and rows[0]["type"] == "retry"))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2.0)

    assert len(outcome) == 20
    assert sum(1 for retried in outcome if retried) == 10


# -- 空变化 / 校验 ------------------------------------------------------ #

def test_empty_incremental_returns_empty_line_and_client_gets_nothing():
    view = FakeView([entry("t", version=1)])
    srv, svc, _received, limits = server_for(view)
    srv.start()
    try:
        client = PullClient(limits=limits, clock=Clock())
        got = client.fetch("127.0.0.1", srv.port, {
            "type": "pull", "project": "p", "mode": "incremental",
            "watermarks": {"t": [1, 1]},
        })
    finally:
        srv.stop()

    assert got == []
    assert client.pages == 0


@pytest.mark.parametrize("payload", [
    {"type": "pull", "mode": "incremental"},
    {"type": "pull", "project": "p", "mode": "nope"},
])
def test_invalid_pull_request_returns_error_line(payload):
    rows = service(FakeView()).handle(payload)
    assert rows[0]["type"] == "error"
    assert rows[0]["error"]


# -- TCP 同端口混合 ---------------------------------------------------- #

def test_same_port_accepts_entry_then_pull_on_one_connection():
    view = FakeView([entry("new", version=7)])
    srv, svc, received, _limits = server_for(view)
    srv.start()
    try:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=3.0) as sock:
            sock.sendall(encode_line({
                "type": "entry",
                "entry": entry("up", version=3).to_dict(),
            }))
            sock.sendall(encode_line({
                "type": "pull", "project": "p", "mode": "incremental",
                "from": [0, 0],
            }))
            rows = read_until_blank(sock)
    finally:
        srv.stop()

    assert [e.tid for e in received] == ["up"]
    assert [r["entry"]["tid"] for r in rows if r["type"] == "entry"] == ["new"]


def test_bad_json_line_is_skipped_without_killing_connection():
    view = FakeView([entry("new")])
    srv, svc, received, _limits = server_for(view)
    srv.start()
    try:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=3.0) as sock:
            sock.sendall(b"{not-json\n")
            sock.sendall(encode_line({
                "type": "entry",
                "entry": entry("up").to_dict(),
            }))
            sock.sendall(encode_line({
                "type": "pull", "project": "p", "mode": "incremental",
                "from": [0, 0],
            }))
            rows = read_until_blank(sock)
    finally:
        status = srv.status()
        srv.stop()

    assert status["bad_lines"] >= 1
    assert [e.tid for e in received] == ["up"]
    assert [r["entry"]["tid"] for r in rows if r["type"] == "entry"] == ["new"]


# -- 客户端 ------------------------------------------------------------ #

class OneShotServer:
    """测试用一次性 TCP 服务：收到请求后按给定字节回复。"""

    def __init__(self, reply: bytes):
        self.reply = reply
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            conn, _ = self.sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(self.reply)
        finally:
            self.sock.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.thread.join(2.0)


def test_client_skips_bad_lines_and_counts_pages():
    reply = b"".join([
        b"{broken\n",
        encode_line({"type": "page", "page": 1, "total_pages": 1}),
        encode_line({"type": "entry", "entry": entry("t").to_dict()}),
        b"\n",
    ])
    with OneShotServer(reply) as fake:
        client = PullClient(limits=Limits(), clock=Clock())
        got = client.fetch("127.0.0.1", fake.port, {
            "type": "pull", "project": "p", "mode": "full", "from": None,
        })

    assert [e.tid for e in got] == ["t"]
    assert client.pages == 1
    assert client.bad_lines == 1


def test_client_raises_pull_retry_with_after_ms():
    reply = b"".join([
        encode_line({"type": "retry", "after_ms": 250}),
        b"\n",
    ])
    with OneShotServer(reply) as fake:
        client = PullClient(limits=Limits(), clock=Clock())
        with pytest.raises(PullRetry) as info:
            client.fetch("127.0.0.1", fake.port, {
                "type": "pull", "project": "p", "mode": "incremental",
            })

    assert info.value.after_ms == 250
