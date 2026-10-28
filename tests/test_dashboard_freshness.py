"""看板快照新鲜度回归：X-Snapshot-Mtime 缺失时，刷新循环停摆没人发现。

真实故障（2026-10-28 实测）：刷新循环停摆后 `/api/status` 依旧 200，
页面安安静静渲染 2h43m 前的旧快照，人以为「手工退回没生效」。
修法：serve.py 在 /api/status 响应上带 `X-Snapshot-Mtime`（状态文件 mtime，epoch 秒），
前端据此显示「快照 N 分钟前」，超过 10 分钟加 ⚠ 提示。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
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


def _get(base):
    with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
        return r.status, dict(r.headers), json.loads(r.read().decode("utf-8"))


def test_api_status_carries_snapshot_mtime(tmp_path):
    """/api/status 必须带 X-Snapshot-Mtime，值 = 状态文件 mtime（epoch 秒）。"""
    status = tmp_path / "status.json"
    rows = [{"id": "T-0001", "title": "示例", "state": "ready"}]
    status.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    want = int(status.stat().st_mtime)

    base, shutdown = _serve_once(status)
    try:
        code, headers, data = _get(base)
    finally:
        shutdown()
    assert code == 200
    assert data[0]["id"] == "T-0001"
    assert "X-Snapshot-Mtime" in headers, "快照新鲜度头缺失——停摆时页面只能靠人肉发现"
    assert headers["X-Snapshot-Mtime"] == str(want), (
        "X-Snapshot-Mtime 应为状态文件 mtime（epoch 秒），实际 %r != %r"
        % (headers.get("X-Snapshot-Mtime"), want))


def test_snapshot_mtime_follows_file_update(tmp_path):
    """服务器每次请求重读盘：状态文件更新后，头里的 mtime 必须跟着变。"""
    status = tmp_path / "status.json"
    status.write_text(json.dumps([{"id": "T-0001", "state": "ready"}],
                                 ensure_ascii=False), encoding="utf-8")
    base, shutdown = _serve_once(status)
    try:
        _, headers1, _ = _get(base)
        time.sleep(1.1)   # mtime 秒级精度，间隔过短可能同值
        status.write_text(json.dumps([{"id": "T-0002", "state": "done"}],
                                     ensure_ascii=False), encoding="utf-8")
        code, headers2, data = _get(base)
    finally:
        shutdown()
    assert code == 200 and data[0]["id"] == "T-0002"
    assert int(headers2["X-Snapshot-Mtime"]) >= int(headers1["X-Snapshot-Mtime"]), (
        "状态文件更新后快照头没有跟着变，前端会显示旧时刻")


def test_snapshot_header_absent_when_status_missing(tmp_path):
    """缺状态文件 → 503 且不带快照头（没有快照就没有新鲜度可言）。"""
    base, shutdown = _serve_once(tmp_path / "nope.json")
    try:
        try:
            with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
                raise AssertionError("缺状态文件应当 503，实际 %s" % r.status)
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            assert "X-Snapshot-Mtime" not in exc.headers
    finally:
        shutdown()


def test_freshness_sources_wired():
    """源码级：serve.py 与 app.js 的快照新鲜度实现必须都在位。"""
    src = (DASH / "serve.py").read_text(encoding="utf-8")
    assert "X-Snapshot-Mtime" in src, "serve.py 缺快照新鲜度响应头"
    app = (DASH / "app.js").read_text(encoding="utf-8")
    assert "X-Snapshot-Mtime" in app, "app.js 缺快照时刻读取（前端无法显示数据年龄）"
    assert "快照" in app, "app.js 缺「快照 N 分钟前」展示文案"


def test_refresh_loop_writes_atomically():
    """源码级（T-0191 §4）：refresh_loop 不得直写主文件——半截 JSON 会 /api/status 500。

    两台机器的看板共读同一份 dashboard/status.json 时，Path.write_text 直写
    不是原子替换，另一台正好读到一半就 500。必须 tmp + os.replace。
    """
    src = (DASH / "serve.py").read_text(encoding="utf-8")
    assert "write_text(proc.stdout" not in src, (
        "refresh_loop 仍在直写状态主文件（非原子），必须先写 tmp 再 os.replace")
    assert "os.replace" in src, "refresh_loop 缺原子替换"
