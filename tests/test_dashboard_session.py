"""看板登录态保留（0.16.01）。

背景：登录成功后 cookie 只有 24 小时，而且刷新页面按钮就退回「登录」——
cookie 其实还在，用户却以为没登录，又粘一遍 token。这组用例锁三件事：

* cookie 是**长效持久**的（默认 30 天，可配，0 = 会话 cookie）；
* `GET /api/session` 能用 cookie 反查身份（未登录返回 authenticated=false，不是 401）；
* `POST /api/logout` 能清掉 cookie，token 被轮换后自检立刻变成未登录。
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
sys.path.insert(0, str(ROOT / "dashboard"))

import serve  # noqa: E402


@pytest.fixture()
def tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_MCP_TOKENS", str(tmp_path / "tokens.json"))
    import mcp_auth
    return mcp_auth.enroll("board-ctrl", role="controller")


@pytest.fixture()
def audit(tmp_path, monkeypatch):
    path = tmp_path / "audit.log"
    monkeypatch.setattr(serve, "_audit_log_path", lambda: path)
    return path


@pytest.fixture()
def status_file(tmp_path):
    p = tmp_path / "status.json"
    p.write_text(json.dumps([{"id": "T-0001", "state": "ready"}]), encoding="utf-8")
    return p


def _serve(status_path, write_auth):
    srv = serve.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        serve.build_handler(str(status_path), write_auth=write_auth))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv.shutdown


def _post(base, path, body=None, headers=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}"), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}"), dict(exc.headers)


def _get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


# --------------------------------------------------------------------------- #
# cookie 有效期
# --------------------------------------------------------------------------- #

def test_session_days_default_and_env(monkeypatch):
    monkeypatch.delenv(serve.SESSION_DAYS_ENV, raising=False)
    assert serve._session_days() == 30
    monkeypatch.setenv(serve.SESSION_DAYS_ENV, "7")
    assert serve._session_days() == 7
    monkeypatch.setenv(serve.SESSION_DAYS_ENV, "0")
    assert serve._session_days() == 0            # 会话 cookie：关浏览器失效
    monkeypatch.setenv(serve.SESSION_DAYS_ENV, "abc")
    assert serve._session_days() == 30           # 非法值回落
    monkeypatch.setenv(serve.SESSION_DAYS_ENV, "9999")
    assert serve._session_days() == serve.SESSION_DAYS_MAX


def test_login_cookie_is_persistent(status_file, audit, tokens, monkeypatch):
    monkeypatch.delenv(serve.SESSION_DAYS_ENV, raising=False)
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, headers = _post(base, "/api/login", {"token": tokens})
    finally:
        shutdown()
    assert code == 200, (code, body)
    cookie = headers.get("Set-Cookie") or ""
    assert "Max-Age=%d" % (30 * 86400) in cookie, cookie   # 以前只有 86400
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert body["persistent"] is True
    assert body["expires_in"] == 30 * 86400


def test_session_cookie_when_days_zero(status_file, audit, tokens, monkeypatch):
    monkeypatch.setenv(serve.SESSION_DAYS_ENV, "0")
    base, shutdown = _serve(status_file, "on")
    try:
        code, body, headers = _post(base, "/api/login", {"token": tokens})
    finally:
        shutdown()
    assert code == 200, (code, body)
    cookie = headers.get("Set-Cookie") or ""
    assert "Max-Age" not in cookie
    assert body["persistent"] is False


# --------------------------------------------------------------------------- #
# /api/session 自检
# --------------------------------------------------------------------------- #

def test_session_without_cookie_is_not_authenticated(status_file, tokens):
    base, shutdown = _serve(status_file, "on")
    try:
        code, body = _get(base, "/api/session")
    finally:
        shutdown()
    assert code == 200, (code, body)          # 未登录不是错误状态
    assert body["ok"] is True and body["authenticated"] is False
    assert body["mode"] == "on"


def test_session_restores_identity_from_cookie(status_file, tokens):
    base, shutdown = _serve(status_file, "on")
    try:
        _, _, headers = _post(base, "/api/login", {"token": tokens})
        cookie = (headers.get("Set-Cookie") or "").split(";")[0]
        code, body = _get(base, "/api/session", {"Cookie": cookie})
    finally:
        shutdown()
    assert code == 200 and body["authenticated"] is True
    assert body["agent"] == "board-ctrl" and body["role"] == "controller"


def test_session_auth_off_is_authenticated(status_file, tokens):
    base, shutdown = _serve(status_file, "off")
    try:
        code, body = _get(base, "/api/session")
    finally:
        shutdown()
    assert code == 200 and body["authenticated"] is True
    assert body["mode"] == "off" and body["role"] == "controller"


def test_session_false_after_token_rotated(status_file, tokens, monkeypatch):
    """`hive agent enroll` 重发同一 agent 的 token 会吊销旧的。"""
    base, shutdown = _serve(status_file, "on")
    try:
        _, _, headers = _post(base, "/api/login", {"token": tokens})
        cookie = (headers.get("Set-Cookie") or "").split(";")[0]
        import mcp_auth
        mcp_auth.enroll("board-ctrl", role="controller")   # 轮换：旧 token 作废
        code, body = _get(base, "/api/session", {"Cookie": cookie})
    finally:
        shutdown()
    assert code == 200 and body["authenticated"] is False


# --------------------------------------------------------------------------- #
# 登出
# --------------------------------------------------------------------------- #

def test_logout_clears_cookie(status_file, audit, tokens):
    base, shutdown = _serve(status_file, "on")
    try:
        _, _, login_headers = _post(base, "/api/login", {"token": tokens})
        cookie = (login_headers.get("Set-Cookie") or "").split(";")[0]
        code, body, headers = _post(base, "/api/logout", {}, {"Cookie": cookie})
    finally:
        shutdown()
    assert code == 200 and body["ok"] is True
    set_cookie = headers.get("Set-Cookie") or ""
    assert "Max-Age=0" in set_cookie, set_cookie
    rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    assert any(r.get("action") == "logout" and r.get("agent") == "board-ctrl"
               for r in rows)
