"""控制面状态视图：单调安装、水位、增量查询、DNA、待下行队列。

对应母文档 §4、§4.3 与实现约定 §3.1：

- ``StateView.install`` 严格按 I5：``(incarnation, version)`` 只有严格变大才安装；
  旧版本（``stale``）不覆盖、不记录日志，只计数。
- ``changed_after`` 优先使用逐条 ``watermarks``，缺水位时才用 ``floor`` 当全局下界（D3）。
- ``mark_quiet`` 把 ``steady`` 且连续 ``dna_quiet_intervals`` 个下行周期无变化的条目
  置 ``dna=True``；DNA 只是本地下行提示，不改版本、不写回 Git（D6）。
- ``DownlinkQueue`` 同 key 只留版本大者，并且不接收 DNA 条目（DNA 由 ``plane`` drop）。

所有 ``now`` 参数都可注入；默认时钟 ``time.monotonic``，单测不依赖真实秒数。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping

from .config import Limits
from .model import StateEntry, version_key

__all__ = ["InstallResult", "StateView", "DownlinkQueue"]


# install 结果的 reason（契约 §3.1）
REASON_NEW = "new"
REASON_NEWER = "newer"
REASON_STALE = "stale"


@dataclass(frozen=True)
class InstallResult:
    """一次 ``install`` 的判定结果。"""

    installed: bool
    reason: str


def _coerce_watermark(raw: object) -> tuple[int, int] | None:
    """把 ``[incarnation, version]`` / ``(incarnation, version)`` 规整成二元组。"""
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)):
        return None
    try:
        inc, ver = raw  # type: ignore[misc]
    except (TypeError, ValueError):
        return None
    try:
        return (int(inc), int(ver))
    except (TypeError, ValueError):
        return None


class StateView:
    """``(project, tid)`` → 当前条目的内存视图。

    控制面里每个线程都可能写入（下行收包、上行 TCP、Worker socket），
    因此所有公开方法都在同一把锁下操作；锁内只做纯内存计算，不做 I/O。
    """

    def __init__(
        self,
        limits: Limits | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or Limits()
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], StateEntry] = {}
        # 每个 key 最近一次“版本变化”的本地时刻；DNA 只依赖这个窗口。
        self._changed_at: dict[tuple[str, str], float] = {}
        self._installed = 0
        self._stale = 0
        self._dna_marked = 0

    # -- 写入 ---------------------------------------------------------- #
    def install(self, entry: StateEntry, now: float | None = None) -> InstallResult:
        """按 I5 安装一条状态条目；旧版本一律不安装。"""
        if not isinstance(entry, StateEntry):
            raise TypeError("install 只接受 StateEntry")
        moment = self._now(now)
        key = entry.key
        with self._lock:
            current = self._entries.get(key)
            if current is None:
                self._entries[key] = entry
                self._changed_at[key] = moment
                self._installed += 1
                return InstallResult(True, REASON_NEW)
            if not entry.newer_than(current):
                # I5：stale 不覆盖、不回调、不写日志，只计数。
                self._stale += 1
                return InstallResult(False, REASON_STALE)
            self._entries[key] = entry
            self._changed_at[key] = moment
            self._installed += 1
            return InstallResult(True, REASON_NEWER)

    # -- 读取 ---------------------------------------------------------- #
    def get(self, project: str, tid: str) -> StateEntry | None:
        with self._lock:
            return self._entries.get((project, tid))

    def entries(self, project: str | None = None) -> list[StateEntry]:
        """按 ``(project, tid)`` 排序的快照；``project=None`` 时返回全部。"""
        with self._lock:
            rows = sorted(self._entries.items())
            if project is not None:
                rows = [item for item in rows if item[0][0] == project]
            return [entry for _key, entry in rows]

    def watermarks(self, project: str) -> dict[str, tuple[int, int]]:
        """``{tid: (incarnation, version)}``，供增量拉取请求携带（D3）。"""
        with self._lock:
            return {
                tid: version_key(entry)
                for (proj, tid), entry in sorted(self._entries.items())
                if proj == project
            }

    def changed_after(
        self,
        project: str,
        watermarks: Mapping[str, tuple[int, int]] | None = None,
        floor: tuple[int, int] | None = None,
    ) -> list[StateEntry]:
        """返回比客户端水位新的条目；逐条水位优先，缺水位时用 ``floor``（D3）。"""
        lower = _coerce_watermark(floor)
        with self._lock:
            rows = sorted(self._entries.items())
            out: list[StateEntry] = []
            for (proj, tid), entry in rows:
                if proj != project:
                    continue
                bound: tuple[int, int] | None = None
                if watermarks is not None:
                    bound = _coerce_watermark(watermarks.get(tid))
                if bound is None:
                    bound = lower
                # 客户端完全没带水位时视为“没见过”，全给。
                if bound is None or version_key(entry) > bound:
                    out.append(entry)
            return out

    # -- DNA 稳态抑制 -------------------------------------------------- #
    def mark_quiet(self, interval: float, now: float | None = None) -> list[StateEntry]:
        """把连续 ``dna_quiet_intervals`` 个周期无变化的 steady 条目置 DNA。"""
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise TypeError("interval 必须是数字")
        interval = float(interval)
        if interval <= 0:
            raise ValueError("interval 必须为正数")
        moment = self._now(now)
        threshold = self.limits.dna_quiet_intervals * interval
        marked: list[StateEntry] = []
        with self._lock:
            for key in sorted(self._entries):
                entry = self._entries[key]
                if not entry.steady or entry.dna:
                    continue
                changed_at = self._changed_at.get(key)
                if changed_at is None or moment - changed_at < threshold:
                    continue
                # DNA 是本地提示：版本与 updated_at 都不动，只翻 dna 位。
                quiet = replace(entry, dna=True)
                self._entries[key] = quiet
                marked.append(quiet)
            self._dna_marked += len(marked)
        return marked

    # -- 统计 ---------------------------------------------------------- #
    def stats(self) -> dict:
        with self._lock:
            entries = list(self._entries.values())
            by_state: dict[str, int] = {}
            for entry in entries:
                by_state[entry.state] = by_state.get(entry.state, 0) + 1
            return {
                "entries": len(entries),
                "projects": len({entry.project for entry in entries}),
                "dna": sum(1 for entry in entries if entry.dna),
                "by_state": by_state,
                "installed": self._installed,
                "stale": self._stale,
                "dna_marked": self._dna_marked,
            }

    # -- 内部 ---------------------------------------------------------- #
    def _now(self, now: float | None) -> float:
        if now is None:
            return float(self._clock())
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("now 必须是数字")
        return float(now)


class DownlinkQueue:
    """待下行的条目队列。

    - 同一 ``(project, tid)`` 只保留 ``(incarnation, version)`` 较大者；
    - DNA 条目不进队列（由 ``plane`` 在置位时 ``drop``）；
    - ``drain`` 按入队顺序返回并清空，便于下行发送器按包切分。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], StateEntry] = {}
        self._put = 0
        self._skipped = 0
        self._skipped_dna = 0

    def put(self, entry: StateEntry) -> None:
        if not isinstance(entry, StateEntry):
            raise TypeError("put 只接受 StateEntry")
        with self._lock:
            if entry.dna:
                self._skipped_dna += 1
                return
            current = self._items.get(entry.key)
            if current is not None and not entry.newer_than(current):
                self._skipped += 1
                return
            self._items[entry.key] = entry
            self._put += 1

    def drop(self, project: str, tid: str) -> None:
        with self._lock:
            self._items.pop((project, tid), None)

    def drain(self) -> list[StateEntry]:
        with self._lock:
            rows = list(self._items.values())
            self._items.clear()
            return rows

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def stats(self) -> dict:
        with self._lock:
            return {
                "queued": len(self._items),
                "put": self._put,
                "skipped_old": self._skipped,
                "skipped_dna": self._skipped_dna,
            }