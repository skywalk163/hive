"""T-0311：控制面接入 hive cluster serve（实现约定 §3.10 / D14）。

全程用注入的假对象和临时目录：不碰网络端口、不碰 Git、不 sleep。
重点验证 D14 —— 程序化构造的 Cluster 默认不启动控制面，只有
`hive cluster serve` 入口才开启，避免既有测试实例抢 47899。
"""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import hive_cluster
from hive_cluster import Cluster, Config, build_handler
from hive_control.adapter import ClusterControlAdapter
from hive_control.config import Limits


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------

def make_config(tmp_path, **kw):
    base = dict(
        state_dir=tmp_path, hive_dir=tmp_path, name="t0311-node",
        port=0, discover=False, electable=False, sync_enabled=False,
        mcp_enabled=False, store_enabled=False, coordinator_enabled=False,
        token="", seeds=[],
    )
    base.update(kw)
    return Config(**base)


class FakeCluster:
    """duck-typed cluster：适配器不该 import hive_cluster。"""

    def __init__(self, nodes=None, agents=None):
        self.nodes = nodes if nodes is not None else {
            "n1": {"id": "n1", "url": "http://192.168.0.88:8788"},
            "n2": {"id": "n2", "url": "http://192.168.0.104:8788"},
            "n3": {"id": "n3", "url": ""},
        }
        self._agents = agents if agents is not None else [
            {"name": "workbuddy-win11", "mode": "manual"}]
        self.config = SimpleNamespace(token="")
        self.term = 0

    def _local_agents(self):
        return list(self._agents)


@contextmanager
def http_server(cluster):
    """把真实 Cluster 的 HTTP 面挂在临时端口上，不启动集群其它线程。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(cluster))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    code = resp.status
    conn.close()
    return code, json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------
# D14：开关与生命周期
# --------------------------------------------------------------------------

def test_config_control_plane_off_by_default(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg.control_plane is False
    assert cfg.control_limits is None


def test_programmatic_cluster_does_not_start_control_plane(tmp_path):
    """50+ 既有测试会直接 new Cluster；默认绝不能装配控制面。"""
    cluster = Cluster(make_config(tmp_path))
    assert cluster.control_adapter is None
    cluster._start_control_plane()
    assert cluster.control_adapter is None
    status = cluster.control_status()
    assert status == {"enabled": False, "running": False,
                      "plane_available": False}
    assert cluster.control_request_pull({})[0] == 503
    assert cluster.control_pull_status("x")[0] == 503


def test_start_and_stop_control_plane_are_idempotent(tmp_path):
    cluster = Cluster(make_config(tmp_path))
    cluster.config.control_plane = True
    cluster._start_control_plane()
    first = cluster.control_adapter
    assert isinstance(first, ClusterControlAdapter)
    assert first.status()["running"] is True
    cluster._start_control_plane()
    assert cluster.control_adapter is first       # 幂等，不重复装配
    cluster._stop_control_plane()
    assert cluster.control_adapter is None
    cluster._stop_control_plane()                 # 幂等，不抛


def test_serve_entry_enables_control_plane(tmp_path, monkeypatch):
    """只有 `hive cluster serve` 入口把开关置 True（D14）。"""
    seen = {}

    class FakeServeCluster:
        def __init__(self, config):
            seen["config"] = config
            seen["started"] = False
            seen["stopped"] = False

        def start(self):
            seen["started"] = True
            return self

        def wait_stopped(self, timeout):
            return True

        def stop(self):
            seen["stopped"] = True

    monkeypatch.setattr(hive_cluster, "Cluster", FakeServeCluster)
    monkeypatch.setattr(hive_cluster.signal, "signal",
                        lambda *a, **kw: None)
    args = SimpleNamespace(action="serve", state_dir=tmp_path, dir=tmp_path,
                           leader=False, priority=None)
    assert hive_cluster.run_cli(args) == 0
    assert seen["config"].control_plane is True
    assert seen["started"] and seen["stopped"]


def test_cluster_stop_stops_control_plane_first(tmp_path, monkeypatch):
    """约定 §3.10：Cluster.stop() 先停控制面，再做 leave / 落盘。"""
    cluster = Cluster(make_config(tmp_path))
    events = []

    class Recorder:
        def stop(self):
            events.append("control")

    for name, fn in (
        ("_leave_peers", lambda *a, **kw: events.append("leave")),
        ("_resign_peers", lambda *a, **kw: None),
        ("_persist", lambda *a, **kw: None),
        ("_persist_locked", lambda: None),
        ("_emit_locked", lambda *a, **kw: None),
        ("_remove_pid", lambda: None),
    ):
        monkeypatch.setattr(cluster, name, fn)
    cluster.discovery = None
    cluster.sync_agent = None
    cluster.mcp_service = None
    cluster.archive_agent = None
    cluster.coordinator_agent = None
    cluster.store = None
    cluster._http = None
    cluster._threads = []
    cluster.control_adapter = Recorder()

    cluster.stop()
    assert events and events[0] == "control"
    assert cluster.control_adapter is None


# --------------------------------------------------------------------------
# 适配器自身
# --------------------------------------------------------------------------

def test_adapter_status_shape_and_peer_provider():
    limits = Limits().overridden(control_port=47901)
    adapter = ClusterControlAdapter(FakeCluster(), limits=limits)
    adapter.start()
    status = adapter.status()
    assert status["enabled"] is True
    assert status["running"] is True
    # T-0310 plane.py 尚未落地：如实降级，不伪造数据
    assert status["plane_available"] is False
    assert status["degraded"] is True
    assert status["multicast_mode"] == "off"
    assert status["control_leader"] is None
    assert status["control_epoch"] == 0
    assert status["control_port"] == 47901
    assert status["queues"]["pull"] == 0
    assert status["counters"]["pull_requests"] == 0
    peers = status["peers"]
    assert {p["host"] for p in peers} == {"192.168.0.88", "192.168.0.104"}
    assert all(p["port"] == 47901 for p in peers)
    assert status["local_agents"][0]["name"] == "workbuddy-win11"


def test_adapter_request_pull_and_pull_status():
    adapter = ClusterControlAdapter(FakeCluster())
    adapter.start()
    code, payload = adapter.request_pull({"project": "light", "mode": "full"})
    assert code == 202
    assert payload["ok"] is True and payload["request_id"]
    assert payload["state"] == "pending" and payload["mode"] == "full"
    assert payload["degraded"] is True
    rid = payload["request_id"]

    code, row = adapter.pull_status(rid)
    assert code == 202
    assert row["ok"] is True and row["state"] == "pending"
    assert row["request_id"] == rid and row["project"] == "light"
    assert adapter.status()["counters"]["pull_requests"] == 1
    assert adapter.status()["queues"]["pull"] == 1


def test_adapter_pull_validation_and_unknown_id():
    adapter = ClusterControlAdapter(FakeCluster())
    adapter.start()
    assert adapter.request_pull("not-an-object")[0] == 400
    assert adapter.request_pull({"mode": "bogus"})[0] == 400
    assert adapter.pull_status("")[0] == 400
    assert adapter.pull_status("unknown")[0] == 404
    assert adapter.request_pull({})[1]["mode"] == "incremental"


def test_adapter_delegates_to_injected_plane():
    class FakePlane:
        def __init__(self):
            self.started = self.stopped = False
            self.pulls = []

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def status(self):
            return {"multicast_mode": "multicast", "control_leader": "n1",
                    "control_epoch": 7, "queues": {"uplink": 3},
                    "counters": {"downlink_packets": 12}}

        def request_pull(self, body):
            self.pulls.append(body)
            return {"state": "running"}

    plane = FakePlane()
    adapter = ClusterControlAdapter(FakeCluster(),
                                    plane_factory=lambda cluster: plane)
    adapter.start()
    assert plane.started is True
    status = adapter.status()
    assert status["plane_available"] is True and status["degraded"] is False
    assert status["multicast_mode"] == "multicast"
    assert status["control_leader"] == "n1" and status["control_epoch"] == 7
    assert status["queues"]["uplink"] == 3
    assert status["counters"]["downlink_packets"] == 12

    code, payload = adapter.request_pull({"mode": "full"})
    assert code == 202 and payload["delegated"] is True
    assert payload["state"] == "running" and plane.pulls == [{"mode": "full"}]
    assert adapter.pull_status(payload["request_id"])[1]["state"] == "running"
    adapter.stop()
    assert plane.stopped is True
    assert adapter.status()["running"] is False


def test_adapter_stop_is_idempotent_without_start():
    adapter = ClusterControlAdapter(FakeCluster())
    adapter.stop()
    assert adapter.status()["running"] is False


# --------------------------------------------------------------------------
# HTTP 端点
# --------------------------------------------------------------------------

def test_http_routes_when_control_plane_disabled(tmp_path):
    cluster = Cluster(make_config(tmp_path))
    with http_server(cluster) as port:
        code, body = request(port, "GET", "/api/cluster/status")
        assert code == 200
        assert body["ok"] is True and body["control"]["enabled"] is False

        code, body = request(port, "POST", "/api/cluster/pull",
                             {"project": "light"})
        assert code == 503 and body["ok"] is False

        code, body = request(port, "GET", "/api/cluster/pull/status?id=abc")
        assert code == 503 and body["ok"] is False

        # 旧路由不受影响（约定 §3.10：GET /api/cluster/state 不动）
        code, body = request(port, "GET", "/api/cluster/state")
        assert code == 200 and "state" in body


def test_http_routes_when_control_plane_enabled(tmp_path):
    cluster = Cluster(make_config(tmp_path))
    cluster.config.control_plane = True
    cluster._start_control_plane()
    assert cluster.control_adapter is not None
    with http_server(cluster) as port:
        code, body = request(port, "GET", "/api/cluster/status")
        assert code == 200 and body["control"]["enabled"] is True
        assert body["control"]["running"] is True
        assert "multicast_mode" in body["control"]

        code, body = request(port, "POST", "/api/cluster/pull",
                             {"project": "light", "mode": "incremental"})
        assert code == 202 and body["ok"] is True
        rid = body["request_id"]
        assert rid

        code, row = request(port, "GET", f"/api/cluster/pull/status?id={rid}")
        assert code == 202 and row["state"] == "pending"

        code, row = request(port, "GET", "/api/cluster/pull/status?id=nope")
        assert code == 404 and row["ok"] is False

        code, row = request(port, "GET", "/api/cluster/pull/status")
        assert code == 400 and row["ok"] is False

        code, row = request(port, "POST", "/api/cluster/pull",
                            {"mode": "bogus"})
        assert code == 400 and row["ok"] is False
