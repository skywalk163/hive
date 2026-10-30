"""看板写操作鉴权 + 审计（T-0207）。

* 回环绑定（默认）→ 写操作免鉴权，保持旧体验；
* 非回环绑定或显式 --auth on → 写操作必须带 token，否则 401；
* close/reopen 只许 controller 角色，retry/unassign 允许 worker 自己的 token；
* 每次写入都在 dashboard/audit.log 留一行（成功/失败/未鉴权）。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
sys.path.insert(0, str(DASH))

import serve  # noqa: E402


@pytest.fixture()
def tokens(tmp_path, monkeypatch):
    """独立 token 表；返回 (ctrl_token, worker_token)。"""
    monkeypatch.setenv("HIVE_MCP_TOKENS", str(tmp_path / "tokens.json"))
    import mcp_auth
    ctrl = mcp_auth.enroll("board-ctrl", role="controller")
    worker = mcp_auth.enroll("board-worker", role="worker")
    return ctrl, worker


@pytest.fixture()
def audit(tmp_path, monkeypatch):
    path = tmp_path / "audit.log"

    def _path():
        return path
    monkeypatch.setattr(serve, "_audit_log_path", _path)
    return path


@pytest.fixture()
def status_file(tmp_path):
    p = tmp_path / "status.json"
    p.write_text(json.dumps([{"id": "T-0001", "state": "ready"}],
                            ensure_ascii=False), encoding="utf-8")
    return p


def _serve(status_path, write_auth):
    srv = serve.ThreadingHTTPServer(("127.0.0.1", 0),
                                    serve.build_handler(str(status_path),
                                                        write_auth=write_auth))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv.shutdown


def _post(base, path, body, headers=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}"), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}"), dict(exc.headers)


# --------------------------------------------------------------------------- #
# 鉴权模式
# --------------------------------------------------------------------------- #

def test_auth_mode_auto_by_bind_host(monkeypatch):
    monkeypatch.delenv("HIVE_DASH_AUTH", raising=False)
    assert serve._auth_mode("127.0.0.1") == "off"
    assert serve._auth_mode("localhost") == "off"
    assert serve._auth_mode("0.0.0.0") == "on"
    monkeypatch.setenv("HIVE_DASH_AUTH", "off")
    assert serve._auth_mode("0.0.0.0") == "off"


def test_write_without_token_is_401(status_file, audit, tokens):
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, _ = _post(base, "/api/task",
                              {"action": "retry", "tid": "T-0001"})
    finally:
        shutdown()
    assert code == 401, (code, body)
    assert "token" in body["error"]
    lines = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    assert lines and lines[-1]["ok"] is False and lines[-1]["detail"] == "未鉴权"


def test_read_is_always_allowed(status_file, tokens):
    base, shutdown = _serve(status_file, "on")
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
            assert r.status == 200
    finally:
        shutdown()


def test_login_sets_cookie_and_authorizes(status_file, audit, tokens):
    ctrl, _worker = tokens
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, headers = _post(base, "/api/login", {"token": ctrl})
        assert code == 200, (code, body)
        assert body["agent"] == "board-ctrl"
        cookie = headers.get("Set-Cookie") or ""
        assert serve.COOKIE_NAME in cookie and "HttpOnly" in cookie
    finally:
        shutdown()


def test_bad_token_login_rejected(status_file, audit, tokens):
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, _ = _post(base, "/api/login", {"token": "nope"})
    finally:
        shutdown()
    assert code == 401 and body["ok"] is False


# --------------------------------------------------------------------------- #
# 角色
# --------------------------------------------------------------------------- #

def test_worker_cannot_close_but_can_retry(status_file, audit, tokens, monkeypatch):
    _ctrl, worker = tokens
    calls = []

    def fake_argv(*args):
        # 处理器先 `argv = hive_argv()` 再 `argv += [...]`，所以记下一份可变 list
        # 就能在响应返回后看到完整命令行
        lst = [sys.executable, "-c", "print('ok')"]
        calls.append(lst)
        return lst
    monkeypatch.setattr(serve, "hive_argv", fake_argv)
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, _ = _post(base, "/api/task", {"action": "close", "tid": "T-0001"},
                              {"Authorization": "Bearer " + worker})
        assert code == 403, (code, body)
        assert "controller" in body["error"]

        code, body, _ = _post(base, "/api/task", {"action": "retry", "tid": "T-0001"},
                              {"Authorization": "Bearer " + worker})
        assert code == 200 and body["ok"] is True, (code, body)
        assert calls and "retry" in calls[0]
    finally:
        shutdown()


def test_controller_close_goes_through_and_audits(status_file, audit, tokens, monkeypatch):
    ctrl, _worker = tokens
    monkeypatch.setattr(serve, "hive_argv",
                        lambda *a: [sys.executable, "-c", "print('ok')"])
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, _ = _post(base, "/api/task",
                              {"action": "close", "tid": "T-0001", "note": "看板关闭"},
                              {"X-Hive-Token": ctrl})
    finally:
        shutdown()
    assert code == 200 and body["ok"] is True, (code, body)
    rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    ok = [r for r in rows if r.get("action") == "close" and r.get("ok")]
    assert ok and ok[-1]["agent"] == "board-ctrl" and ok[-1]["note"] == "看板关闭"


def test_auth_off_keeps_old_behavior(status_file, audit, tokens, monkeypatch):
    monkeypatch.setattr(serve, "hive_argv",
                        lambda *a: [sys.executable, "-c", "print('ok')"])
    base, shutdown = _serve(status_file, "off")
    try:
        code, body, _ = _post(base, "/api/task",
                              {"action": "unassign", "tid": "T-0001"})
    finally:
        shutdown()
    assert code == 200 and body["ok"] is True, (code, body)
