"""T-0313：三处集群上报钩子接入控制面 Unix socket 的确定性单测。

覆盖母文档 §2.3 / D5：
- lease → ``claimed``；progress 的 state → ``running`` / ``steady`` / ``released``；
  release → ``released``；
- 上报只入内存队列（``report()`` 一次调用、参数正确），HTTP 原返回值不受影响；
- 控制面不可用 / 导入失败 / 构造异常时，三个函数仍返回原 HTTP 结果、不抛异常。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# conftest.py 已把 ROOT 插入 sys.path；这里显式兜底，便于直接跑本文件。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hive  # noqa: E402


class FakeReporter:
    """假 WorkerClient：只记录 report() 入队的条目，不连 socket。"""

    def __init__(self) -> None:
        self.entries: list[object] = []

    def report(self, entry) -> None:
        self.entries.append(entry)


@pytest.fixture()
def fake_reporter(monkeypatch) -> FakeReporter:
    reporter = FakeReporter()
    monkeypatch.setattr(hive, "_control_reporter", lambda: reporter)
    # 卡面读取走空目录：project/owner 走 payload 兜底，不依赖真实协调仓。
    monkeypatch.setenv("HIVE_DIR", str(ROOT))
    # _cluster_http 在 pytest 进程里默认短路；放开测试隔离，钉成 200。
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")
    monkeypatch.setattr(
        hive, "_cluster_http",
        lambda method, path, payload=None, timeout=1.0: (200, None))
    yield reporter


def test_lease_hook_reports_claimed(fake_reporter):
    payload = {"task": "T-0313", "agent": "doubao-win",
               "repo": "light", "fence": 1}
    ok = hive.cluster_publish_lease(payload)

    assert ok is True
    assert len(fake_reporter.entries) == 1
    entry = fake_reporter.entries[0]
    assert entry.tid == "T-0313"
    assert entry.project == "light"
    assert entry.owner_agent == "doubao-win"
    assert entry.state == "claimed"
    assert entry.version == 0 and entry.incarnation == 0
    assert entry.dna is False


def test_release_hook_reports_released(fake_reporter):
    ok = hive.cluster_release_lease("T-0313")

    assert ok is True
    assert len(fake_reporter.entries) == 1
    entry = fake_reporter.entries[0]
    assert entry.tid == "T-0313"
    assert entry.state == "released"
    # release 钩子没有 payload/agent：owner 兜底，project 走默认。
    assert entry.project
    assert entry.owner_agent


@pytest.mark.parametrize("raw,expected", [
    ("running", "running"),
    ("verifying", "running"),
    ("done", "released"),
    ("failed", "steady"),
    ("blocked", "steady"),
    ("needs_review", "steady"),
    ("", "steady"),
])
def test_progress_state_mapping(fake_reporter, raw, expected):
    payload = {"task": "T-0313", "agent": "doubao-win",
               "repo": "light", "state": raw}
    ok = hive.cluster_publish_progress(payload)

    assert ok is True
    assert len(fake_reporter.entries) == 1
    entry = fake_reporter.entries[0]
    assert entry.state == expected
    assert entry.tid == "T-0313"
    assert entry.project == "light"
    assert entry.owner_agent == "doubao-win"


def test_progress_falls_back_to_card_when_payload_lacks_repo(monkeypatch, tmp_path):
    """payload 不带 repo 时，从卡面取 project；取不到再兜底默认。"""
    reporter = FakeReporter()
    monkeypatch.setattr(hive, "_control_reporter", lambda: reporter)
    monkeypatch.setenv("HIVE_DIR", str(tmp_path))
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")
    monkeypatch.setattr(
        hive, "_cluster_http",
        lambda method, path, payload=None, timeout=1.0: (200, None))
    ok = hive.cluster_publish_progress(
        {"task": "T-0001", "agent": "alice", "state": "running"})

    assert ok is True
    entry = reporter.entries[0]
    assert entry.tid == "T-0001"
    assert entry.owner_agent == "alice"
    assert entry.project == "hive"


def test_control_plane_down_does_not_break_hooks(monkeypatch):
    """控制面不可用：_control_reporter 返回 None，三个钩子仍正常返回 HTTP 结果。"""
    monkeypatch.setattr(hive, "_control_reporter", lambda: None)
    monkeypatch.setenv("HIVE_DIR", str(ROOT))
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")
    monkeypatch.setattr(
        hive, "_cluster_http",
        lambda method, path, payload=None, timeout=1.0: (200, None))

    assert hive.cluster_publish_lease({"task": "T-0313", "agent": "a"}) is True
    assert hive.cluster_publish_progress(
        {"task": "T-0313", "agent": "a", "state": "running"}) is True
    assert hive.cluster_release_lease("T-0313") is True


def test_reporter_constructor_failure_is_swallowed(monkeypatch):
    """构造 WorkerClient 抛异常时，懒加载返回 None，钩子不影响原逻辑。"""
    def boom():
        raise RuntimeError("socket 不可用")

    monkeypatch.setattr(hive, "_control_reporter", boom)
    monkeypatch.setenv("HIVE_DIR", str(ROOT))
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")
    monkeypatch.setattr(
        hive, "_cluster_http",
        lambda method, path, payload=None, timeout=1.0: (200, None))

    assert hive.cluster_publish_lease({"task": "T-0313", "agent": "a"}) is True
    assert hive.cluster_publish_progress(
        {"task": "T-0313", "agent": "a", "state": "running"}) is True
    assert hive.cluster_release_lease("T-0313") is True


def test_report_exception_does_not_affect_http_result(monkeypatch):
    """report() 自身抛异常（队列满/入队失败）被静默，HTTP 返回值原样保留。"""
    class BrokenReporter:
        def report(self, entry):
            raise RuntimeError("queue full")

    monkeypatch.setattr(hive, "_control_reporter", lambda: BrokenReporter())
    monkeypatch.setenv("HIVE_DIR", str(ROOT))
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")
    monkeypatch.setattr(
        hive, "_cluster_http",
        lambda method, path, payload=None, timeout=1.0: (200, None))

    assert hive.cluster_publish_lease({"task": "T-0313", "agent": "a"}) is True
    assert hive.cluster_publish_progress(
        {"task": "T-0313", "agent": "a", "state": "done"}) is True
    assert hive.cluster_release_lease("T-0313") is True


def test_import_failure_falls_back_silently(monkeypatch):
    """hive_control 模块导入失败时，懒加载单例为 None，钩子照常返回。"""
    monkeypatch.setattr(hive, "_CONTROL_REPORTER", None)
    monkeypatch.setenv("HIVE_DIR", str(ROOT))
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")
    monkeypatch.setattr(
        hive, "_cluster_http",
        lambda method, path, payload=None, timeout=1.0: (200, None))
    # 强制 _control_entry 里的 StateEntry 导入失败：用一个会抛 ImportError 的桩。
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("hive_control"):
            raise ImportError("hive_control 未部署")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert hive.cluster_publish_lease({"task": "T-0313", "agent": "a"}) is True
    assert hive.cluster_publish_progress(
        {"task": "T-0313", "agent": "a", "state": "running"}) is True
    assert hive.cluster_release_lease("T-0313") is True


def test_empty_tid_release_returns_false(monkeypatch):
    monkeypatch.setattr(hive, "_control_reporter", lambda: FakeReporter())
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")
    monkeypatch.setattr(
        hive, "_cluster_http",
        lambda method, path, payload=None, timeout=1.0: (200, None))
    assert hive.cluster_release_lease("") is False


def test_entry_is_serializable(fake_reporter):
    """映射出的条目能序列化成 JSON 行（与 worker_sock 行协议对齐）。"""
    hive.cluster_publish_progress(
        {"task": "T-0313", "agent": "doubao-win",
         "repo": "light", "state": "running"})
    entry = fake_reporter.entries[0]
    line = json.dumps(entry.to_dict(), ensure_ascii=False)
    parsed = json.loads(line)
    assert parsed["tid"] == "T-0313"
    assert parsed["state"] == "running"
