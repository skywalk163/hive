"""看板对常驻集群的只读桥接：/api/cluster + /api/status 的进度合并。

集群是可选件：没起时 /api/cluster 返回 running=false，/api/status 仍要正常；
起了时集群里更新的进度要能覆盖 git 快照里的旧 pushed_at。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.request
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))

import serve  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve_once(status_path):
    srv = serve.ThreadingHTTPServer(("127.0.0.1", 0),
                                     serve.build_handler(str(status_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv.shutdown


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _write_cluster_state(state_dir: Path, snapshot: dict):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps({"snapshot": snapshot}, ensure_ascii=False), encoding="utf-8")


def test_api_cluster_offline_returns_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_CLUSTER_STATE", str(tmp_path / "cluster"))
    monkeypatch.setenv("HIVE_CLUSTER_PORT", str(_free_port()))
    _write_cluster_state(tmp_path / "cluster", {
        "cluster_id": "hive", "term": 3, "role": "follower",
        "leader": {"id": "a", "name": "a", "url": "http://10.0.0.1:8788"},
        "self": {"id": "b", "name": "b"}, "nodes": [], "alive": 1, "known": 2,
        "progress": {}, "leases": {},
    })
    status = tmp_path / "status.json"
    status.write_text("[]", encoding="utf-8")
    base, shutdown = _serve_once(status)
    try:
        code, data = _get_json(base + "/api/cluster")
    finally:
        shutdown()
    assert code == 200
    assert data["running"] is False
    assert data["state"]["leader"]["name"] == "a"
    assert data["state"]["term"] == 3


def test_status_merges_cluster_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_CLUSTER_STATE", str(tmp_path / "cluster"))
    monkeypatch.setenv("HIVE_CLUSTER_PORT", str(_free_port()))
    _write_cluster_state(tmp_path / "cluster", {
        "cluster_id": "hive", "term": 4, "role": "leader",
        "leader": {"id": "a", "name": "a"},
        "self": {"id": "a", "name": "a"}, "nodes": [], "alive": 1, "known": 1,
        "progress": {
            "T-0001": {"task": "T-0001", "agent": "a", "state": "running",
                        "progress": 77, "step": "跑 verify",
                        "message": "集群实时进度",
                        "updated_at": "2099-01-01T00:00:00+08:00"},
        },
        "leases": {},
    })
    status = tmp_path / "status.json"
    status.write_text(json.dumps([{
        "id": "T-0001", "title": "示例", "state": "assigned", "owner": "a",
        "pushed_at": "2000-01-01T00:00:00+08:00",
    }], ensure_ascii=False), encoding="utf-8")
    base, shutdown = _serve_once(status)
    try:
        code, data = _get_json(base + "/api/status")
    finally:
        shutdown()
    assert code == 200
    row = data[0]
    assert row["progress"] == 77
    assert row["push_step"] == "跑 verify"
    assert row["push_message"] == "集群实时进度"
    assert row["push_source"] == "cluster"
