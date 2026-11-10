"""T-0304：上行队列与 200ms 聚合器的确定性单测。"""

from __future__ import annotations

import os
import threading
import time

from hive_control.model import StateEntry
from hive_control.uplink_queue import Aggregator, UplinkQueue


def entry(tid: str, *, project: str = "p", version: int = 1,
          incarnation: int = 1, state: str = "steady") -> StateEntry:
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
    """可注入时钟，避免单测 sleep 等真实秒数。"""

    def __init__(self, now: float = 0.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


# -- UplinkQueue ------------------------------------------------------- #

def test_queue_overflow_drops_oldest_steady():
    q = UplinkQueue(capacity=4096)
    for i in range(4096):
        q.put(entry(f"t{i}"))

    q.put(entry("new"))

    assert q.qsize() == 4096
    assert q.stats() == {"dropped_steady": 1, "dropped_transient": 0}
    # 最旧的 steady（t0）被淘汰，t1 成为队首
    assert q.get().tid == "t1"

    last = None
    while True:
        item = q.get(timeout=0.001)
        if item is None:
            break
        last = item
    assert last is not None and last.tid == "new"


def test_queue_full_of_transient_drops_oldest_and_counts():
    q = UplinkQueue(capacity=3)
    for i in range(3):
        q.put(entry(f"t{i}", state="claimed"))

    q.put(entry("new", state="claimed"))

    assert q.stats() == {"dropped_steady": 0, "dropped_transient": 1}
    assert [q.get().tid for _ in range(3)] == ["t1", "t2", "new"]


def test_queue_prefers_dropping_steady_over_older_transient():
    q = UplinkQueue(capacity=3)
    q.put(entry("claim0", state="claimed"))
    q.put(entry("steady1"))
    q.put(entry("steady2"))

    # 满了：虽然 claim0 最旧，但要先丢最近的 steady（steady1）
    q.put(entry("new"))

    assert q.stats() == {"dropped_steady": 1, "dropped_transient": 0}
    assert [q.get().tid for _ in range(3)] == ["claim0", "steady2", "new"]


def test_get_timeout_returns_none_and_put_wakes_waiter():
    q = UplinkQueue(capacity=2)
    assert q.get(timeout=0.01) is None

    got: list[StateEntry] = []
    waiter = threading.Thread(target=lambda: got.append(q.get(timeout=1.0)))
    waiter.start()
    q.put(entry("t"))
    waiter.join(1.0)

    assert not waiter.is_alive()
    assert got and got[0].tid == "t"


# -- Aggregator -------------------------------------------------------- #

def test_aggregator_keeps_max_version_per_key_and_sorts_batches():
    clock = Clock()
    batches: list[list[StateEntry]] = []
    agg = Aggregator(batches.append, window=0.2, clock=clock)

    agg.submit(entry("b", project="p2", version=1))
    agg.submit(entry("a", project="p2", version=1))
    agg.submit(entry("a", project="p2", version=3))
    agg.submit(entry("a", project="p1", version=1))

    assert agg.flush(force=True) == 3
    assert len(batches) == 1
    assert [(e.project, e.tid, e.version) for e in batches[0]] == [
        ("p1", "a", 1),
        ("p2", "a", 3),
        ("p2", "b", 1),
    ]


def test_aggregator_uses_incarnation_as_version_major():
    batches: list[list[StateEntry]] = []
    agg = Aggregator(batches.append, clock=Clock())

    agg.submit(entry("t", incarnation=2, version=1))
    agg.submit(entry("t", incarnation=1, version=99))

    assert agg.flush(force=True) == 1
    assert batches[0][0].incarnation == 2


def test_flush_waits_for_window_from_first_submit():
    clock = Clock()
    batches: list[list[StateEntry]] = []
    agg = Aggregator(batches.append, window=0.2, clock=clock)

    agg.submit(entry("a"))
    clock.advance(0.15)
    agg.submit(entry("b"))
    clock.advance(0.04)          # 距首次提交 0.19s，未到窗口
    assert agg.flush() == 0
    assert batches == []

    clock.advance(0.02)          # 距首次提交 0.21s，窗口已到
    assert agg.flush() == 2
    assert {e.tid for e in batches[0]} == {"a", "b"}


def test_background_thread_flushes_after_window():
    batches: list[list[StateEntry]] = []
    agg = Aggregator(batches.append, window=0.01)
    agg.start()
    agg.start()                   # 幂等：不会重复起线程
    agg.submit(entry("t"))

    deadline = time.monotonic() + 2.0
    while not batches and time.monotonic() < deadline:
        time.sleep(0.005)
    agg.stop()
    agg.stop()                    # 幂等

    assert batches and batches[0][0].tid == "t"


def test_stop_flushes_remaining_and_is_idempotent():
    batches: list[list[StateEntry]] = []
    agg = Aggregator(batches.append, window=10.0, clock=Clock())
    agg.start()
    agg.submit(entry("t"))

    agg.stop()
    assert len(batches) == 1
    assert batches[0][0].tid == "t"

    agg.stop()
    assert len(batches) == 1


def test_submit_and_put_are_fast():
    # Windows CI 计时粒度较粗，放宽到 2ms；POSIX 目标 < 1ms。
    budget = 0.002 if os.name == "nt" else 0.001
    clock = Clock()
    agg = Aggregator(lambda batch: None, window=60.0, clock=clock)
    q = UplinkQueue(capacity=4096)
    entries = [entry(f"t{i}") for i in range(1000)]

    start = time.perf_counter()
    for item in entries:
        agg.submit(item)
    submit_avg = (time.perf_counter() - start) / len(entries)

    start = time.perf_counter()
    for item in entries:
        q.put(item)
    put_avg = (time.perf_counter() - start) / len(entries)

    assert submit_avg < budget, f"submit 平均 {submit_avg * 1000:.3f}ms"
    assert put_avg < budget, f"put 平均 {put_avg * 1000:.3f}ms"
