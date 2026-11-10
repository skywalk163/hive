"""控制面状态视图：单调安装（I5）/ 水位 / 增量 / DNA 抑制 / 待下行队列。

对应实现约定 §3.1、§4.3；全部用注入时钟，不 sleep、不碰网络。
"""

from __future__ import annotations

import pytest

from hive_control.config import Limits
from hive_control.model import StateEntry
from hive_control.view import (
    REASON_NEW,
    REASON_NEWER,
    REASON_STALE,
    DownlinkQueue,
    InstallResult,
    StateView,
)


def entry(**kw) -> StateEntry:
    base = dict(
        project="proj",
        tid="T-0001",
        version=1,
        incarnation=7,
        owner_agent="agent-a",
        state="claimed",
        dna=False,
        updated_at=1_700_000_000_000,
    )
    base.update(kw)
    return StateEntry(**base)


class Clock:
    """可手动推进的注入时钟。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> float:
        self.value += delta
        return self.value


# --------------------------------------------------------------------------
# I5：单调安装
# --------------------------------------------------------------------------

def test_install_new_then_newer():
    view = StateView()
    first = entry(version=3, incarnation=7)
    result = view.install(first)
    assert result == InstallResult(True, REASON_NEW)
    assert view.get("proj", "T-0001") is first

    newer = entry(version=4, incarnation=7, state="running")
    result = view.install(newer)
    assert result == InstallResult(True, REASON_NEWER)
    assert view.get("proj", "T-0001") is newer


def test_install_rejects_same_version_even_if_content_differs():
    view = StateView()
    view.install(entry(version=5, state="claimed", owner_agent="a"))
    stale = entry(version=5, state="running", owner_agent="b")
    result = view.install(stale)
    assert result == InstallResult(False, REASON_STALE)
    current = view.get("proj", "T-0001")
    assert current.state == "claimed"
    assert current.owner_agent == "a"


@pytest.mark.parametrize("older", [
    dict(version=1, incarnation=6),   # 版本号更大但 incarnation 更旧
    dict(version=9, incarnation=7),   # 同 incarnation 旧版本（已被 version=9 覆盖过）
])
def test_install_strict_lexicographic(older):
    view = StateView()
    view.install(entry(version=10, incarnation=7))
    result = view.install(entry(project="proj", tid="T-0001", **older,
                                owner_agent="x", state="steady",
                                dna=False, updated_at=1))
    assert result.installed is False
    assert result.reason == REASON_STALE
    assert view.get("proj", "T-0001").incarnation == 7
    assert view.get("proj", "T-0001").version == 10


def test_install_is_per_key():
    view = StateView()
    view.install(entry(tid="T-0001", incarnation=9, version=1))
    # 另一条 task 从更低的 incarnation 开始也应能安装
    result = view.install(entry(tid="T-0002", incarnation=2, version=0))
    assert result == InstallResult(True, REASON_NEW)
    assert view.get("proj", "T-0002").incarnation == 2


def test_stale_counted_but_entries_unchanged():
    view = StateView()
    view.install(entry(version=2))
    assert view.install(entry(version=2)).reason == REASON_STALE
    assert view.install(entry(version=1)).reason == REASON_STALE
    stats = view.stats()
    assert stats["installed"] == 1
    assert stats["stale"] == 2
    assert len(view.entries()) == 1


def test_install_requires_state_entry():
    view = StateView()
    with pytest.raises(TypeError):
        view.install({"project": "proj"})  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 水位 / 增量
# --------------------------------------------------------------------------

def test_entries_and_watermarks_are_sorted_and_scoped():
    view = StateView()
    view.install(entry(tid="T-0002", incarnation=8, version=2))
    view.install(entry(tid="T-0001", incarnation=7, version=5))
    view.install(entry(project="other", tid="T-0001", incarnation=3, version=1))

    assert [e.tid for e in view.entries("proj")] == ["T-0001", "T-0002"]
    assert [e.tid for e in view.entries()] == ["T-0001", "T-0001", "T-0002"]
    assert view.entries("missing") == []
    assert view.watermarks("proj") == {
        "T-0001": (7, 5),
        "T-0002": (8, 2),
    }
    assert view.watermarks("missing") == {}
    assert view.get("other", "T-9999") is None


def test_changed_after_uses_per_tid_watermarks():
    view = StateView()
    view.install(entry(tid="T-0001", incarnation=7, version=5))
    view.install(entry(tid="T-0002", incarnation=8, version=2))

    # T-0001 正好等于水位 → 不发；T-0002 比水位新 → 发
    changed = view.changed_after("proj", watermarks={"T-0001": (7, 5),
                                                     "T-0002": (8, 1)})
    assert [e.tid for e in changed] == ["T-0002"]

    # 水位里没有的条目视为“没见过”，照发
    changed = view.changed_after("proj", watermarks={"T-0001": (7, 5)})
    assert [e.tid for e in changed] == ["T-0002"]


def test_changed_after_falls_back_to_floor():
    view = StateView()
    view.install(entry(tid="T-0001", incarnation=7, version=5))
    view.install(entry(tid="T-0002", incarnation=2, version=0))

    changed = view.changed_after("proj", floor=(2, 0))
    assert [e.tid for e in changed] == ["T-0001"]

    # watermarks 里给了的条目优先，没给的用 floor
    changed = view.changed_after("proj", watermarks={"T-0001": (7, 5)},
                                 floor=(2, 0))
    assert changed == []


def test_changed_after_without_any_bound_returns_everything():
    view = StateView()
    view.install(entry(tid="T-0001"))
    view.install(entry(tid="T-0002", incarnation=1, version=0))
    assert len(view.changed_after("proj")) == 2
    assert view.changed_after("missing") == []


def test_changed_after_includes_dna_entries():
    view = StateView()
    view.install(entry(version=2, state="steady"), now=0.0)
    view.mark_quiet(1.0, now=3.0)
    assert view.get("proj", "T-0001").dna is True
    assert [e.tid for e in view.changed_after("proj", floor=(7, 1))] == ["T-0001"]
    assert view.watermarks("proj") == {"T-0001": (7, 2)}


def test_changed_after_tolerates_malformed_watermark():
    view = StateView()
    view.install(entry(version=4))
    # 坏水位按“缺失”处理，回落到 floor；floor 也没有就全发
    assert len(view.changed_after("proj", watermarks={"T-0001": "bad"})) == 1
    assert len(view.changed_after("proj", watermarks={"T-0001": "bad"},
                                  floor=(7, 4))) == 0


# --------------------------------------------------------------------------
# DNA：稳态抑制（§4.3 / D6）
# --------------------------------------------------------------------------

def test_mark_quiet_after_three_intervals():
    view = StateView()
    view.install(entry(state="steady", version=1, dna=False), now=100.0)

    assert view.mark_quiet(2.0, now=105.0) == []      # 才 2.5 个周期
    marked = view.mark_quiet(2.0, now=106.0)         # 恰好 3 个周期
    assert [e.tid for e in marked] == ["T-0001"]
    assert marked[0].dna is True
    # DNA 只翻位，不动版本与 updated_at
    assert marked[0].version == 1
    assert marked[0].updated_at == 1_700_000_000_000
    # 再调一次不会重复返回
    assert view.mark_quiet(2.0, now=200.0) == []
    assert view.stats()["dna_marked"] == 1


def test_mark_quiet_only_affects_steady_non_dna_entries():
    view = StateView()
    view.install(entry(tid="T-0001", state="claimed"), now=0.0)
    view.install(entry(tid="T-0002", state="running"), now=0.0)
    view.install(entry(tid="T-0003", state="steady", dna=False), now=0.0)
    view.install(entry(tid="T-0004", state="steady", dna=True), now=0.0)

    marked = view.mark_quiet(1.0, now=10.0)
    assert [e.tid for e in marked] == ["T-0003"]
    assert view.get("proj", "T-0001").dna is False
    assert view.get("proj", "T-0002").dna is False
    assert view.get("proj", "T-0004").dna is True


def test_new_version_resets_dna_and_quiet_window():
    view = StateView()
    view.install(entry(state="steady", version=1), now=0.0)
    assert view.mark_quiet(1.0, now=3.0)                # v1 置 DNA

    # owner 发来 v2（dna=False）→ 自然复位，窗口从安装时刻重新算
    view.install(entry(state="steady", version=2, dna=False), now=10.0)
    assert view.get("proj", "T-0001").dna is False
    assert view.mark_quiet(1.0, now=12.5) == []          # 2.5 个周期
    assert view.mark_quiet(1.0, now=13.0)

    # 旧版本到达不能把 DNA 打回 False
    view.install(entry(state="steady", version=2, dna=False), now=20.0)
    assert view.get("proj", "T-0001").dna is True


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_mark_quiet_rejects_non_positive_interval(bad):
    view = StateView()
    with pytest.raises(ValueError):
        view.mark_quiet(bad, now=1.0)


def test_mark_quiet_uses_injected_clock():
    clock = Clock(0.0)
    view = StateView(clock=clock)
    view.install(entry(state="steady"))     # 用默认 now → 注入时钟
    clock.advance(2.9)
    assert view.mark_quiet(1.0) == []
    clock.advance(0.1)
    assert [e.tid for e in view.mark_quiet(1.0)] == ["T-0001"]


def test_mark_quiet_threshold_follows_limits():
    limits = Limits().overridden(dna_quiet_intervals=2)
    view = StateView(limits=limits)
    view.install(entry(state="steady"), now=0.0)
    assert view.mark_quiet(1.0, now=1.0) == []   # 1 个周期 < 2
    assert view.mark_quiet(1.0, now=2.0)         # 2 个周期


# --------------------------------------------------------------------------
# 待下行队列
# --------------------------------------------------------------------------

def test_downlink_queue_keeps_greater_version_only():
    queue = DownlinkQueue()
    queue.put(entry(version=1, state="running"))
    assert len(queue) == 1

    queue.put(entry(version=1, state="claimed"))    # 同版本 → 不替换
    assert len(queue) == 1
    assert queue.drain()[0].state == "running"

    queue.put(entry(version=2, state="running"))
    queue.put(entry(version=1, state="claimed"))    # 旧版本 → 忽略
    rows = queue.drain()
    assert [e.version for e in rows] == [2]
    assert len(queue) == 0


def test_downlink_queue_ignores_dna_entries():
    queue = DownlinkQueue()
    queue.put(entry(dna=True))
    assert len(queue) == 0
    assert queue.stats()["skipped_dna"] == 1


def test_downlink_queue_drop_and_drain():
    queue = DownlinkQueue()
    queue.put(entry(tid="T-0001", version=1))
    queue.put(entry(tid="T-0002", version=1))
    queue.put(entry(tid="T-0003", version=1))

    queue.drop("proj", "T-0002")
    queue.drop("proj", "T-0404")           # 不存在：无副作用
    rows = queue.drain()
    assert [e.tid for e in rows] == ["T-0001", "T-0003"]
    assert len(queue) == 0
    assert queue.drain() == []


def test_downlink_queue_is_per_key():
    queue = DownlinkQueue()
    queue.put(entry(tid="T-0001", incarnation=9, version=1))
    queue.put(entry(tid="T-0002", incarnation=2, version=0))
    assert len(queue) == 2
    queue.put(entry(tid="T-0001", incarnation=9, version=2))
    rows = queue.drain()
    assert {(e.tid, e.version) for e in rows} == {("T-0001", 2), ("T-0002", 0)}


def test_downlink_queue_requires_state_entry():
    queue = DownlinkQueue()
    with pytest.raises(TypeError):
        queue.put("not-an-entry")  # type: ignore[arg-type]