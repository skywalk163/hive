"""控制面数据模型：状态条目、版本比较、incarnation 持久化。

对应母文档 §4.1、§4.2 与实现约定 §2.1。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

# §4.1 的状态枚举。母文档 §5.4 用到的 expiring 也收进来，但它只是下行的
# 提示态，不写回 Git，也不参与任务归属（裁定 D4）。
STATES: tuple[str, ...] = (
    "claimed",
    "running",
    "releasing",
    "released",
    "steady",
    "expiring",
)

# 瞬态：队列满时必须优先保住这些条目（G3）。
TRANSIENT_STATES = frozenset({"claimed", "releasing", "expiring"})

_REQUIRED = (
    "project",
    "tid",
    "version",
    "incarnation",
    "owner_agent",
    "state",
    "dna",
    "updated_at",
)


def version_key(entry: "StateEntry") -> tuple[int, int]:
    """(incarnation, version) 字典序比较键，对应 I5。"""
    return (int(entry.incarnation), int(entry.version))


def is_newer(candidate: "StateEntry", current: "StateEntry | None") -> bool:
    """candidate 是否严格新于 current；current 为 None 时恒真。"""
    if current is None:
        return True
    return version_key(candidate) > version_key(current)


@dataclass(frozen=True)
class StateEntry:
    """控制面里描述 ``(project, tid)`` 当前状态的最小单元。"""

    project: str
    tid: str
    version: int
    incarnation: int
    owner_agent: str
    state: str
    dna: bool
    updated_at: int
    urgent: bool = False

    def __post_init__(self) -> None:
        for name in ("project", "tid", "owner_agent"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} 必须是字符串")
        if not self.project:
            raise ValueError("project 不能为空")
        if not self.tid:
            raise ValueError("tid 不能为空")
        for name in ("version", "incarnation", "updated_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是整数")
            if value < 0:
                raise ValueError(f"{name} 不能为负")
        if self.state not in STATES:
            raise ValueError(f"未知状态 {self.state!r}")
        if not isinstance(self.dna, bool):
            raise ValueError("dna 必须是 bool")
        if not isinstance(self.urgent, bool):
            raise ValueError("urgent 必须是 bool")

    # -- 构造 ---------------------------------------------------------- #
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StateEntry":
        if not isinstance(data, Mapping):
            raise ValueError("状态条目必须是 JSON 对象")
        missing = [k for k in _REQUIRED if k not in data]
        if missing:
            raise ValueError("状态条目缺字段：" + ",".join(missing))
        try:
            version = int(data["version"])
            incarnation = int(data["incarnation"])
            updated_at = int(data["updated_at"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"状态条目数字字段不合法：{exc}") from exc
        dna = data["dna"]
        if not isinstance(dna, bool):
            raise ValueError("dna 必须是 bool")
        return cls(
            project=str(data["project"]),
            tid=str(data["tid"]),
            version=version,
            incarnation=incarnation,
            owner_agent=str(data["owner_agent"]),
            state=str(data["state"]),
            dna=dna,
            updated_at=updated_at,
            urgent=bool(data.get("urgent", False)),
        )

    @classmethod
    def from_line(cls, line: str | bytes) -> "StateEntry":
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        return cls.from_dict(json.loads(line))

    # -- 序列化 -------------------------------------------------------- #
    def to_dict(self) -> dict:
        out = {
            "project": self.project,
            "tid": self.tid,
            "version": self.version,
            "incarnation": self.incarnation,
            "owner_agent": self.owner_agent,
            "state": self.state,
            "dna": self.dna,
            "updated_at": self.updated_at,
        }
        if self.urgent:
            out["urgent"] = True
        return out

    def to_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False,
                          separators=(",", ":"))

    # -- 语义 ---------------------------------------------------------- #
    @property
    def key(self) -> tuple[str, str]:
        return (self.project, self.tid)

    @property
    def version_key(self) -> tuple[int, int]:
        return version_key(self)

    @property
    def transient(self) -> bool:
        return self.state in TRANSIENT_STATES

    @property
    def steady(self) -> bool:
        return self.state == "steady"

    def newer_than(self, other: "StateEntry | None") -> bool:
        return is_newer(self, other)

    def bumped(self, **changes: Any) -> "StateEntry":
        """本地 owner 更新：version+1，默认刷新 updated_at。"""
        changes.setdefault("version", self.version + 1)
        changes.setdefault(
            "updated_at",
            int(time.time() * 1000),
        )
        return replace(self, **changes)


class IncarnationStore:
    """``/tmp/hive-incarnation``：每次 serve 启动严格 +1（§4.1）。

    文件不可写时退回 Unix 秒；同一进程内仍保证比上一次大。
    """

    def __init__(self, path: str | os.PathLike[str] | None = None):
        from .config import default_incarnation_path

        self.path = Path(path or default_incarnation_path())
        self._last = 0

    def _read(self) -> int:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            return max(0, int(raw))
        except (OSError, ValueError):
            return 0

    def next(self) -> int:
        previous = max(self._read(), self._last)
        candidate = previous + 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".hive-inc-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(str(candidate))
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except OSError:
            fallback = int(time.time())
            candidate = max(previous + 1, fallback)
        self._last = candidate
        return candidate

    def current(self) -> int:
        return max(self._read(), self._last)
