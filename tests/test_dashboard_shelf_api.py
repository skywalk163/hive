"""看板 /api/task 的「搁置」三件套：白名单、CLI 映射、角色与标题校验。

前端的折叠面板全靠这三个 action 干活：shelve 封存、unshelve 放回、
respawn 另立新卡。这里只测接口这一层，真正的状态机在 tests/test_shelve.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import serve  # noqa: E402
from test_dashboard_auth import _post, _serve, audit, status_file, tokens  # noqa: E402,F401


def _argv_recorder(monkeypatch, output="print('ok')"):
    calls = []

    def fake_argv(*args):
        lst = [sys.executable, "-c", output]
        # /api/task 调的是 hive_argv()（无参），之后 argv += [...] 会改这个 list；
        # _refresh_status 调的是 hive_argv("status", "--json")，别把它记进来。
        if not args:
            calls.append(lst)
        return lst
    monkeypatch.setattr(serve, "hive_argv", fake_argv)
    return calls


def test_shelf_actions_map_to_hive_cli(status_file, audit, tokens, monkeypatch):
    ctrl, _ = tokens
    calls = _argv_recorder(monkeypatch)
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, _ = _post(base, "/api/task",
                              {"action": "shelve", "tid": "T-0001",
                               "note": "上游 SDK 没发版"},
                              {"X-Hive-Token": ctrl})
        assert code == 200 and body["ok"] is True, (code, body)
        assert calls[0][-4:] == ["shelve", "T-0001", "--reason", "上游 SDK 没发版"], calls[0]

        code, body, _ = _post(base, "/api/task",
                              {"action": "unshelve", "tid": "T-0001",
                               "note": "上游发版了"},
                              {"X-Hive-Token": ctrl})
        assert code == 200 and body["ok"] is True, (code, body)
        assert calls[-1][-4:] == ["unshelve", "T-0001", "--reason", "上游发版了"], calls[-1]

        code, body, _ = _post(base, "/api/task",
                              {"action": "respawn", "tid": "T-0001",
                               "title": "按新接口重做", "note": "需求变了"},
                              {"X-Hive-Token": ctrl})
        assert code == 200 and body["ok"] is True, (code, body)
        argv = calls[-1]
        assert argv[-6:] == ["unshelve", "T-0001", "--reason", "需求变了",
                             "--spawn-new", "--title"] or "--spawn-new" in argv, argv
        assert "按新接口重做" in argv, argv
    finally:
        shutdown()


def test_respawn_returns_new_task_id(status_file, audit, tokens, monkeypatch):
    ctrl, _ = tokens
    _argv_recorder(monkeypatch, output="print('NEW_TASK=T-0002')")
    base, shutdown = _serve(status_file, "on")
    try:
        _code, body, _ = _post(base, "/api/task",
                               {"action": "respawn", "tid": "T-0001",
                                "title": "新卡"},
                               {"X-Hive-Token": ctrl})
    finally:
        shutdown()
    assert body["new_task"] == "T-0002", body


def test_respawn_validates_title(status_file, audit, tokens, monkeypatch):
    ctrl, _ = tokens
    _argv_recorder(monkeypatch)
    base, shutdown = _serve(status_file, "on")
    try:
        for bad in ("", "   ", "x" * 201, "有\n换行"):
            code, body, _ = _post(base, "/api/task",
                                  {"action": "respawn", "tid": "T-0001",
                                   "title": bad},
                                  {"X-Hive-Token": ctrl})
            assert code == 400 and body["ok"] is False, (bad, code, body)
    finally:
        shutdown()


def test_shelf_actions_require_controller(status_file, audit, tokens, monkeypatch):
    _ctrl, worker = tokens
    _argv_recorder(monkeypatch)
    base, shutdown = _serve(status_file, "on")
    try:
        for action in ("shelve", "unshelve", "respawn"):
            code, body, _ = _post(base, "/api/task",
                                  {"action": action, "tid": "T-0001",
                                   "title": "新卡"},
                                  {"Authorization": "Bearer " + worker})
            assert code == 403 and "controller" in body["error"], (action, code, body)
    finally:
        shutdown()


def test_unknown_action_still_rejected(status_file, audit, tokens, monkeypatch):
    ctrl, _ = tokens
    _argv_recorder(monkeypatch)
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, _ = _post(base, "/api/task",
                              {"action": "delete-everything", "tid": "T-0001"},
                              {"X-Hive-Token": ctrl})
    finally:
        shutdown()
    assert code == 400 and "不支持的操作" in body["error"]
