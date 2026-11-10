"""有界上行队列与 200ms 聚合器（T-0304）。

对应 ``docs/控制面v0.5-实现约定.md`` §3.3 与母文档 §5.1：

- Worker 上报路径只做内存操作（I6），不做 I/O、不打印日志、不等待；
- 队列容量固定，满时优先淘汰最旧的 ``steady`` 条目（G3）；
- 聚合窗口 200ms，按 ``(project, tid)`` 只保留 ``(incarnation, version)``
  最大者，时间源可注入，便于确定性测试。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque

from .model import StateEntry

Key = tuple[str, str]


class UplinkQueue:
    """固定容量 FIFO 队列，满时优先丢最旧的 steady 条目。

    ``put`` 只持有内部锁做常数级内存操作，永不阻塞调用方，也不会因为
    队列满而抛异常；瞬态（``claimed``/``releasing``/``expiring``）尽量保留。
    """

    def __init__(self, capacity: int = 4096):
        capacity = int(capacity)
        if capacity < 1:
            raise ValueError("capacity 必须为正整数")
        self.capacity = capacity
        self._items: Deque[StateEntry] = deque()
        self._cond = threading.Condition(threading.Lock())
        self._dropped_steady = 0
        self._dropped_transient = 0

    def put(self, entry: StateEntry) -> None:
        """入队；满时先按 G3 淘汰一条，再放入新条目。"""
        with self._cond:
            if len(self._items) >= self.capacity:
                self._drop_one_locked()
            self._items.append(entry)
            self._cond.notify()

    def _drop_one_locked(self) -> None:
        """优先丢最旧的 steady；没有 steady 才丢队首瞬态。"""
        index = None
        for i, item in enumerate(self._items):
            if item.state == "steady":
                index = i
                break
        if index is not None:
            del self._items[index]
            self._dropped_steady += 1
        else:
            self._items.popleft()
            self._dropped_transient += 1

    def get(self, timeout: float | None = None) -> StateEntry | None:
        """取队首；超时仍为空返回 ``None``。"""
        with self._cond:
            if not self._items:
                self._cond.wait(timeout)
            if not self._items:
                return None
            return self._items.popleft()

    def qsize(self) -> int:
        with self._cond:
            return len(self._items)

    def stats(self) -> dict:
        with self._cond:
            return {
                "dropped_steady": self._dropped_steady,
                "dropped_transient": self._dropped_transient,
            }


class Aggregator:
    """按 ``(project, tid)`` 聚合状态条目，窗口到期后批量交给 sink。

    ``submit`` 只更新内存字典；sink 在锁外调用，避免慢 sink 卡住 Worker。
    """

    def __init__(
        self,
        sink: Callable[[list[StateEntry]], None],
        *,
        window: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
    ):
        window = float(window)
        if window <= 0:
            raise ValueError("window 必须为正数")
        self.window = window
        self._sink = sink
        self._clock = clock
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._send_lock = threading.Lock()
        self._pending: dict[Key, StateEntry] = {}
        self._first_at: float | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, entry: StateEntry) -> None:
        """只写内存：同 key 保留版本大者，并唤醒后台 flush 线程。"""
        with self._cond:
            current = self._pending.get(entry.key)
            if current is None or entry.version_key > current.version_key:
                self._pending[entry.key] = entry
            if self._first_at is None:
                self._first_at = self._clock()
            self._cond.notify()

    def flush(self, force: bool = False) -> int:
        """窗口到期或 ``force`` 时把当前批次交给 sink，返回条目数。"""
        now = self._clock()
        with self._lock:
            if not self._pending:
                return 0
            if not force and self._first_at is not None:
                if now - self._first_at < self.window:
                    return 0
            batch = [self._pending[key] for key in sorted(self._pending)]
            self._pending.clear()
            self._first_at = None
        with self._send_lock:
            self._sink(batch)
        return len(batch)

    def start(self) -> None:
        """启动后台 flush 线程；重复调用不重复起线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopped = False
            self._thread = threading.Thread(
                target=self._run, name="hive-aggregator", daemon=True)
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        """停止后台线程并 flush 剩余条目；幂等。"""
        with self._cond:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
            self._cond.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.window * 2))
        self.flush(force=True)

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopped:
                    return
                if not self._pending:
                    self._cond.wait()
                    continue
                remaining = self.window - (self._clock() - self._first_at)
                if remaining > 0:
                    self._cond.wait(remaining)
                    continue
            self.flush()
