"""看板状态通道的编码回归：cp936 与 UTF-8 BOM 曾在 Windows 上让看板永不刷新。

两条真实故障（2026-10-28 实测）：

1. `refresh_loop` 用 `subprocess.run(["python3", ...])` 且不指定编码，子进程 stdout
   走 Windows 默认 cp936，`hive status --json` 碰到 `✓` 直接 UnicodeEncodeError 退出
   （rc=1、stdout 空）→ 状态文件永远写不出来 → `/api/status` 一直 503，看板停在旧快照；
2. 状态文件被 PowerShell 重定向写过一次，带 UTF-8 BOM，`json.loads` 抛
   "Unexpected UTF-8 BOM" → `/api/status` 500。
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


def test_api_status_accepts_utf8_bom(tmp_path):
    """带 BOM 的状态文件也必须能读（PowerShell 重定向的历史产物）。"""
    status = tmp_path / "status.json"
    rows = [{"id": "T-0001", "title": "示例", "state": "ready"}]
    status.write_text("\ufeff" + json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    base, shutdown = _serve_once(status)
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
            assert r.status == 200
            data = json.loads(r.read().decode("utf-8"))
    finally:
        shutdown()
    assert data[0]["id"] == "T-0001"


def test_api_status_missing_file_is_503(tmp_path):
    base, shutdown = _serve_once(tmp_path / "nope.json")
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
            raise AssertionError("缺状态文件应当 503，实际 %s" % r.status)
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
    finally:
        shutdown()


def test_refresh_uses_utf8_and_current_interpreter():
    """子进程必须带 UTF-8 环境与当前解释器，不许硬编码 python3。"""
    src = (DASH / "serve.py").read_text(encoding="utf-8")
    # 允许 `sys.executable or "python3"` 这种兜底，但不许把 python3 直接当 argv 首元素
    assert '["python3"' not in src, "不得硬编码 python3 作为子进程解释器"
    assert "sys.executable" in src
    assert "PYTHONIOENCODING" in src and "PYTHONUTF8" in src
    assert 'encoding="utf-8-sig"' in src, "状态文件读取要兼容 BOM"
    # refresh_loop 的两个 subprocess.run 都要带 env + encoding
    assert src.count("env=PYTHON_ENV") >= 2
    assert src.count('encoding="utf-8"') >= 2