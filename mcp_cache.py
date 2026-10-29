#!/usr/bin/env python3
"""P1：hived 的只读缓存。

`hived` 用一个**独立的 read clone** 周期性 fetch/reset，把任务卡、agent、claim、
result 解析成内存快照；MCP 读工具直接读快照，不发 git 子进程、不碰网络。
写工具仍然走 `HiveMcp.hive()`（独立 write clone），claim 语义不变。

缓存是**只读视图**，不是真相：WAIT 1~2 秒内的可见性由刷新周期决定。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from hive import Hive, HiveError, IN_FLIGHT_STATES, git, git_net

# 只缓存「在飞」卡的 claim/result；已 close 的卡从 main 的 results/ 文件读。
_CACHE_STATES = set(IN_FLIGHT_STATES) | {"assigned", "running", "submitted"}


class ReadCache:
    def __init__(self, hive_dir: str | Path, interval: float = 2.0,
                 git_interval: float = 10.0):
        self.dir = Path(hive_dir).expanduser().resolve()
        self.interval = max(0.5, float(interval))
        self.git_interval = max(self.interval, float(git_interval))
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_git = 0.0
        self._updated_at = 0.0
        self._last_error = ""
        self._cards: dict[str, dict] = {}
        self._agents: dict[str, dict] = {}
        self._claims: dict[str, dict] = {}
        self._results: dict[str, dict] = {}
        self._statuses: dict[str, dict] = {}

    # -- 生命周期 ---------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="hive-mcp-cache",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict:
        with self._lock:
            return {
                "updated_at": self._updated_at,
                "cards": len(self._cards),
                "agents": len(self._agents),
                "claims": len(self._claims),
                "results": len(self._results),
                "last_error": self._last_error,
            }

    def _loop(self) -> None:
        self.refresh()
        while not self._stop.wait(self.interval):
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001  缓存失败不能拖垮 hived
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"

    # -- 刷新 -------------------------------------------------------------- #
    def refresh(self) -> None:
        # fetch/reset 可能几秒：不要在持锁时做，读请求继续用上一版快照。
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            if not (self.dir / ".git").exists():
                with self._lock:
                    self._last_error = f"{self.dir} 不是 git 仓"
                return
            now = time.time()
            if now - self._last_git >= self.git_interval:
                try:
                    git_net(["fetch", "--prune", "origin"], self.dir)
                    git(["reset", "--hard", "-q", "origin/main"], self.dir)
                    self._last_git = now
                except HiveError as exc:
                    # 网络失败就继续用上一版快照；10s 后再试，不要每 2s 打一次。
                    self._last_git = now
                    with self._lock:
                        self._last_error = str(exc).splitlines()[0]
            h = Hive(self.dir)
            try:
                cards = h.cards()
                agents = h.agents()
            except HiveError as exc:
                with self._lock:
                    self._last_error = str(exc)
                return
            claims: dict[str, dict] = {}
            results: dict[str, dict] = {}
            statuses: dict[str, dict] = {}

            # 1) close 后归档到 main 的 results/（纯文件，最快）
            for p in sorted((h.dir / "results").glob("T-*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict):
                    results[p.stem] = data

            # 2) 在飞卡才查 claim/res/status 分支（通常几十张，不是几百张）
            for tid, card in cards.items():
                state = card.get("state")
                if state not in _CACHE_STATES and not card.get("owner"):
                    continue
                try:
                    claim = h.claim_info(tid)
                    if isinstance(claim, dict):
                        claims[tid] = claim
                    if tid not in results:
                        res = h.result_info(tid)
                        if isinstance(res, dict):
                            results[tid] = res
                    st = h.status_info(tid)
                    if isinstance(st, dict):
                        statuses[tid] = st
                except HiveError:
                    continue

            with self._lock:
                self._cards = cards
                self._agents = agents
                self._claims = claims
                self._results = results
                self._statuses = statuses
                self._updated_at = now
                self._last_error = ""
        finally:
            self._refresh_lock.release()

    # -- 读接口（浅拷贝，防止工具改到缓存） -------------------------------- #
    def cards(self) -> dict[str, dict]:
        with self._lock:
            return {tid: dict(card) for tid, card in self._cards.items()}

    def card(self, tid: str) -> dict | None:
        with self._lock:
            card = self._cards.get(str(tid))
            return dict(card) if isinstance(card, dict) else None

    def agents(self) -> dict[str, dict]:
        with self._lock:
            return {name: dict(cfg) for name, cfg in self._agents.items()}

    def agent(self, name: str) -> dict | None:
        with self._lock:
            cfg = self._agents.get(str(name))
            return dict(cfg) if isinstance(cfg, dict) else None

    def claim_info(self, tid: str) -> dict | None:
        with self._lock:
            item = self._claims.get(str(tid))
            return dict(item) if isinstance(item, dict) else None

    def result_info(self, tid: str) -> dict | None:
        with self._lock:
            item = self._results.get(str(tid))
            return dict(item) if isinstance(item, dict) else None

    def status_info(self, tid: str) -> dict | None:
        with self._lock:
            item = self._statuses.get(str(tid))
            return dict(item) if isinstance(item, dict) else None


class CachedHive:
    """只读方法的缓存视图；写方法/未覆盖属性委托给真实 Hive。"""

    def __init__(self, hive_dir: str | Path, cache: ReadCache):
        self._real = Hive(hive_dir)
        self._cache = cache

    @property
    def dir(self) -> Path:
        return self._real.dir

    def cards(self) -> dict[str, dict]:
        return self._cache.cards()

    def card(self, tid: str) -> dict:
        card = self._cache.card(tid)
        if card is None:
            raise HiveError(f"没有任务 {tid}")
        return card

    def agents(self) -> dict[str, dict]:
        return self._cache.agents()

    def agent(self, name: str) -> dict:
        cfg = self._cache.agent(name)
        if cfg is None:
            raise HiveError(f"没有登记 agent {name}")
        return cfg

    def claim_info(self, tid: str):
        return self._cache.claim_info(tid)

    def result_info(self, tid: str):
        return self._cache.result_info(tid)

    def status_info(self, tid: str):
        return self._cache.status_info(tid)

    def dep_done(self, dep: str) -> bool:
        res = self.result_info(dep)
        return bool(res and res.get("status") == "done")

    def landable(self) -> list[str]:
        cards = self.cards()
        out = []
        for tid, card in cards.items():
            if card.get("state") == "done":
                continue
            res = self.result_info(tid)
            if not res or res.get("status") != "done":
                continue
            if any(v.get("code") for v in (res.get("verify") or [])):
                continue
            if any((cards.get(d) or {}).get("state") != "done"
                   for d in (card.get("depends_on") or [])):
                continue
            out.append(tid)
        return sorted(out)

    def __getattr__(self, name):
        return getattr(self._real, name)
