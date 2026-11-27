"""T-0538：`/ci.json` 一直 500 的回归判据。

前端 `app.js` 每 30 秒（`CI_MS=30000`）请求一次 `/ci.json`。`dashboard/ci.json` 是
`scripts/ci-check.sh` 生成、且被 .gitignore 忽略的运行时产物：新机器天然没有它，
旧实现按静态文件处理就一直 500，`dashboard.err.log` 刷了 1700+ 条。现在接口必须
永远返回 200 + 前端 `renderCI()` 认识的空数据结构。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))

import serve  # noqa: E402


def _serve_once(status_path):
    """在随机端口起一个真实看板服务，返回 (base_url, shutdown)。"""
    srv = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.build_handler(str(status_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv.shutdown


def _get_ci(base):
    with urllib.request.urlopen(base + "/ci.json", timeout=10) as r:
        return r.status, r.headers, json.loads(r.read().decode("utf-8"))


def test_ci_json_missing_file_returns_200_empty(tmp_path, monkeypatch):
    """没有 ci.json（新机器常态）→ 200 + runners 数组 / repos 对象，绝不 500。"""
    monkeypatch.setattr(serve, "BASE_DIR", tmp_path)
    base, shutdown = _serve_once(tmp_path / "status.json")
    try:
        code, headers, data = _get_ci(base)
    finally:
        shutdown()
    assert code == 200
    assert headers["Content-Type"].startswith("application/json")
    assert isinstance(data, dict)
    assert data["runners"] == []
    assert data["repos"] == {}


def test_ci_json_malformed_file_returns_200_empty(tmp_path, monkeypatch):
    """ci-check.sh 写了一半的坏 JSON 也不能让前端拿到 500。"""
    (tmp_path / "ci.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(serve, "BASE_DIR", tmp_path)
    base, shutdown = _serve_once(tmp_path / "status.json")
    try:
        code, _, data = _get_ci(base)
    finally:
        shutdown()
    assert code == 200
    assert data["runners"] == [] and data["repos"] == {}


def test_ci_json_existing_file_is_served(tmp_path, monkeypatch):
    """真有 CI 数据时必须原样吐出，不能被兜底空数据吞掉。"""
    real = {"checked_at": "2026-11-27T12:00:00+08:00",
            "runners": [{"name": "fb5", "status": "online"}],
            "repos": {"skywalk/hive": {"latest": {"status": "Success"}}}}
    (tmp_path / "ci.json").write_text(json.dumps(real), encoding="utf-8")
    monkeypatch.setattr(serve, "BASE_DIR", tmp_path)
    base, shutdown = _serve_once(tmp_path / "status.json")
    try:
        code, _, data = _get_ci(base)
    finally:
        shutdown()
    assert code == 200
    assert data == real


def test_ci_json_is_a_route_not_a_static_whitelist_entry():
    """源码级钉死：走 serve_ci 接口，不再靠一个被 gitignore 的文件存在与否。"""
    src = (DASH / "serve.py").read_text(encoding="utf-8")
    assert "API_CI" in src and "def serve_ci" in src
    assert '"/ci.json": ("ci.json"' not in src, "又退回静态文件 500 路径了"
