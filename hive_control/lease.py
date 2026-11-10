"""控制面租约簿：建租约、周期续约、过期提示。

对应母文档 §5.4、实现约定 §3.7 与裁定 D4：

* 本地 ``claimed``/``running`` 条目建租约，续约间隔 ``lease_ttl/3``（默认 30s）；
* Leader 侧超过 ``lease_ttl``（默认 90s）只产出 ``state="expiring"`` 的**提示副本**，
  同 ``(incarnation, version)`` 不变，不写回 Git、不当作状态安装；
* 切主后 ``grace_period``（默认 30s）内不判过期；
* 同一租约每 ``lease_ttl`` 最多提示一次；所有时间用注入时钟，线程安全。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from .config import Limits
from .model import StateEntry

# 与 ControlConfig 的默认值一致；plane 可把用户配置显式传进来，测试可覆盖。
DEFAULT_LEASE_TTL = 90.0
DEFAULT_GRACE_PERIOD = 30.0
RENEWAL_INTERVAL_RATIO = 3.0

# 持有租约的状态。released/steady/expiring 是非租约状态，note 时清理。
LEASE_STATES = frozenset({"claimed", "running", "releasing"})


@dataclass
class _Lease:
    """一条租约簿记录（内部可变；对外只暴露 StateEntry 副本）。"""

    entry: StateEntry
    local: bool
    last_renewal_at: float
    last_hint_at: float | None = None


class LeaseBook:
    """``(project, tid)`` → 租约。Leader 与本地 Worker 共用一本簿子。

    ``lease_ttl`` / ``grace_period`` 不传时用模块默认值（与 ControlConfig 一致）；
    这两个窗口属于用户配置，从 ``ControlConfig`` 传入，而不是 ``Limits`` 常量。
    """

    def __init__(self, *, limits: Limits, clock: Callable[[], float] = time.monotonic,
                 lease_ttl: float | None = None,
                 grace_period: float | None = None) -> None:
        self._limits = limits
        self._clock = clock
        self._lease_ttl = float(DEFAULT_LEASE_TTL if lease_ttl is None else lease_ttl)
        self._grace_period = float(
            DEFAULT_GRACE_PERIOD if grace_period is None else grace_period)
        if self._lease_ttl <= 0:
            raise ValueError("lease_ttl 必须为正数")
        if self._grace_period < 0:
            raise ValueError("grace_period 不能为负")
        self._renewal_interval = self._lease_ttl / RENEWAL_INTERVAL_RATIO
        self._leases: dict[tuple[str, str], _Lease] = {}
        self._lock = threading.RLock()
        self._grace_until = 0.0

    # -- 写入 ---------------------------------------------------------- #
    def note(self, entry: StateEntry, local: bool,
             *, renewal: bool = False) -> None:
        """登记 / 刷新一条租约。

        * 新 ``(project, tid)``：记为租约，``last_renewal_at = now``；
        * 同版本条目（上行续约副本）：只刷新时间，不重置提示计时；
        * 版本更大：视为新一轮租约，重置续约与提示计时；
        * 版本更小：旧条目，按 I5 忽略；
        * ``released`` 等非租约状态：清掉记录（只改内存，不碰 Git）。
        """
        if entry.state not in LEASE_STATES:
            self.drop(entry.project, entry.tid)
            return
        now = self._clock()
        key = entry.key
        with self._lock:
            record = self._leases.get(key)
            if record is None:
                self._leases[key] = _Lease(
                    entry=entry,
                    local=bool(local),
                    last_renewal_at=now,
                )
                return
            if entry.version_key < record.entry.version_key:
                return
            if entry.version_key == record.entry.version_key:
                record.entry = entry
                record.local = record.local or bool(local)
                record.last_renewal_at = now
                return
            # 版本推进 = 新一轮租约周期：提示计时复位，允许重新提示。
            record.entry = entry
            record.local = bool(local)
            record.last_renewal_at = now
            record.last_hint_at = None

    def mark_renewed(self, entry: StateEntry) -> None:
        """续约副本已发出：刷新续约计时（幂等；旧版本不回卷）。"""
        with self._lock:
            record = self._leases.get(entry.key)
            if record is None:
                return
            if entry.version_key != record.entry.version_key:
                return
            record.entry = entry
            record.last_renewal_at = self._clock()

    def drop(self, project: str, tid: str) -> None:
        with self._lock:
            self._leases.pop((project, tid), None)

    def on_leader_change(self) -> None:
        """切主：``grace_period`` 内 ``expired()`` 一律返回空。"""
        with self._lock:
            self._grace_until = self._clock() + self._grace_period

    # -- 读取 ---------------------------------------------------------- #
    def renewals_due(self) -> list[StateEntry]:
        """本地租约到点（默认 30s）返回续约副本：版本不变、updated_at 刷新。"""
        now = self._clock()
        with self._lock:
            due = [
                self._copy(record.entry)
                for record in self._leases.values()
                if record.local
                and record.entry.state in LEASE_STATES
                and now - record.last_renewal_at >= self._renewal_interval
            ]
        return due

    def expired(self, grace_until: float = 0.0) -> list[StateEntry]:
        """Leader 侧超时条目返回 ``expiring`` 提示副本（D4：只是提示）。

        宽限期（调用方传入或切主自动设置）内永远返回空；同一租约每
        ``lease_ttl`` 最多提示一次。
        """
        now = self._clock()
        with self._lock:
            if now < max(float(grace_until or 0.0), self._grace_until):
                return []
            hints: list[StateEntry] = []
            for record in self._leases.values():
                if now - record.last_renewal_at <= self._lease_ttl:
                    continue
                if (record.last_hint_at is not None
                        and now - record.last_hint_at < self._lease_ttl):
                    continue
                record.last_hint_at = now
                hints.append(self._copy(record.entry, state="expiring",
                                        urgent=True))
            return hints

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            records = list(self._leases.values())
            return {
                "leases": len(records),
                "local": sum(1 for r in records if r.local),
                "renewals_due": sum(
                    1 for r in records
                    if r.local and now - r.last_renewal_at >= self._renewal_interval),
                "expired": sum(
                    1 for r in records
                    if now - r.last_renewal_at > self._lease_ttl),
                "hinted": sum(1 for r in records if r.last_hint_at is not None),
                "lease_ttl": self._lease_ttl,
                "renewal_interval": self._renewal_interval,
                "grace_period": self._grace_period,
                "grace_until": round(self._grace_until, 3),
                "grace_remaining": round(
                    max(0.0, self._grace_until - now), 3),
            }

    # -- 内部 ---------------------------------------------------------- #
    def _copy(self, entry: StateEntry, **changes) -> StateEntry:
        """副本：默认只刷新 updated_at，版本与归属一律不动。"""
        changes.setdefault("updated_at", self._stamp(entry))
        return replace(entry, **changes)

    def _stamp(self, entry: StateEntry) -> int:
        """updated_at 用注入时钟推进，保证副本严格比原条目新。"""
        return max(int(entry.updated_at), int(self._clock() * 1000)) + 1
