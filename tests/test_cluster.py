"""hive 集群层测试：发现/注册/选主/任期围栏/化身号/租约宽限/HTTP 接口。

这些测试全部用 127.0.0.1 + 临时端口，关闭 UDP 广播（避免和开发机上的真集群
抢 8789），只保留可重复的 HTTP 路径。
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hive
import hive_cluster as hc
from conftest import HIVE, sh, ROOT


def wait_until(fn, timeout: float = 6.0, interval: float = 0.05):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception:  # noqa: BLE001  测试轮询期间状态可能正好在变
            pass
        time.sleep(interval)
    return last


@pytest.fixture()
def cluster_factory(tmp_path):
    clusters: list[hc.Cluster] = []

    def make(name: str, seeds=None, **kw):
        cfg = hc.Config(
            state_dir=kw.pop("state_dir", tmp_path / name),
            name=name,
            host=kw.pop("host", "127.0.0.1"),
            port=kw.pop("port", 0),
            advertise=kw.pop("advertise", "127.0.0.1"),
            discover=kw.pop("discover", False),
            seeds=seeds or [],
            hive_dir=kw.pop("hive_dir", tmp_path / "repo"),
            heartbeat_interval=kw.pop("heartbeat_interval", 0.08),
            peer_timeout=kw.pop("peer_timeout", 0.5),
            dead_timeout=kw.pop("dead_timeout", 1.2),
            election_min=kw.pop("election_min", 0.3),
            election_jitter=kw.pop("election_jitter", 0.1),
            leader_lease=kw.pop("leader_lease", 0.7),
            lease_grace=kw.pop("lease_grace", 2.0),
            **kw,
        )
        c = hc.Cluster(cfg)
        c.start()
        clusters.append(c)
        return c

    yield make
    for c in clusters:
        try:
            c.stop()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 身份 / 快照
# --------------------------------------------------------------------------- #

def test_identity_persists_and_incarnation_increases(tmp_path):
    one = hc.Identity(tmp_path, "fb88")
    two = hc.Identity(tmp_path, "fb88")
    assert one.node_id == two.node_id
    assert two.incarnation > one.incarnation
    other = hc.Identity(tmp_path, "win10")
    assert other.node_id != one.node_id


def test_single_node_becomes_leader(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader"), "单节点应自动当选"
    snap = a.snapshot()
    assert snap["leader"]["id"] == a.node_id
    assert snap["term"] >= 1
    assert snap["alive"] == 1 and snap["known"] == 1
    assert snap["quorum"] == 1
    assert any(e["type"] == "leader_elected" for e in snap["events"])


# --------------------------------------------------------------------------- #
# 注册 / 双向信息
# --------------------------------------------------------------------------- #

def test_two_nodes_register_and_share_state(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id), "B 应注册到 A"
    assert wait_until(lambda: len([n for n in a.snapshot()["nodes"]
                                   if n["status"] == "alive"]) == 2)
    assert b.term == a.term
    assert any(n["id"] == b.node_id for n in a.snapshot()["nodes"])

    b.publish_progress({"task": "T-0001", "agent": "b", "state": "running",
                        "progress": 33, "step": "写测试"})
    assert wait_until(lambda: (a.progress.get("T-0001") or {}).get("progress") == 33)
    assert wait_until(lambda: any(e["type"] == "task_progress"
                                  for e in a.snapshot()["events"]))


def test_progress_and_lease_forwarding(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)

    b.publish_lease({"task": "T-0002", "agent": "b",
                     "lease_until": hc._iso(time.time() + 60),
                     "lease_until_epoch": time.time() + 60})
    assert wait_until(lambda: "T-0002" in a.leases)
    assert a.leases["T-0002"]["agent"] == "b"

    b.release_lease("T-0002")
    assert wait_until(lambda: a.leases.get("T-0002", {}).get("released") is True)


# --------------------------------------------------------------------------- #
# 选主 / 故障恢复
# --------------------------------------------------------------------------- #

def test_failover_after_graceful_leader_stop(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)
    old_term = b.term

    a.stop()
    assert wait_until(lambda: b.role == "leader", timeout=6), "A 退出后 B 应接任"
    assert b.term > old_term
    assert b.leader_id == b.node_id
    assert wait_until(lambda: any(e["type"] == "leader_elected"
                                  for e in b.snapshot()["events"]))


def test_failover_after_leader_crash(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)

    # 模拟崩溃：不走 resign，只停循环和 HTTP。B 只能靠租约超时发现。
    a._stop.set()
    a._http.shutdown()
    a._http.server_close()

    assert wait_until(lambda: b.role == "leader", timeout=8), "崩溃的 A 应被租约超时判死"
    assert b.leader_id == b.node_id


def test_lease_grace_after_failover(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)
    b.publish_lease({"task": "T-0003", "agent": "b",
                     "lease_until": hc._iso(time.time() + 60),
                     "lease_until_epoch": time.time() + 60})
    assert wait_until(lambda: "T-0003" in a.leases)

    a.stop()
    assert wait_until(lambda: b.role == "leader", timeout=6)
    lease = wait_until(lambda: b.leases.get("T-0003"))
    assert lease, "新主应恢复已知租约"
    assert float(lease.get("grace_until_epoch") or 0) > time.time(), \
        "新主上任应给在飞租约恢复宽限期"


# --------------------------------------------------------------------------- #
# 任期围栏 / 化身号
# --------------------------------------------------------------------------- #

def test_vote_fencing_and_single_vote_per_term(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    term = a.term

    code, resp = a.handle_vote({"term": term - 1, "candidate": {
        "id": "old-node", "incarnation": 1}})
    assert code == 200 and resp["granted"] is False
    assert resp["reason"] == "stale_term"

    code, resp = a.handle_vote({"term": term + 1, "candidate": {
        "id": "cand-1", "incarnation": time.time_ns()}})
    assert code == 200 and resp["granted"] is True
    code, resp = a.handle_vote({"term": term + 1, "candidate": {
        "id": "cand-2", "incarnation": time.time_ns()}})
    assert code == 200 and resp["granted"] is False


def test_stale_incarnation_rejected(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    node = {"id": "peer-1", "name": "peer", "host": "127.0.0.1",
            "port": 1234, "url": "http://127.0.0.1:1234",
            "incarnation": 1000}
    a.handle_register({"node": node})
    stale = dict(node, name="old-peer", incarnation=999)
    a.handle_register({"node": stale})
    rec = a.snapshot()
    peer = next(n for n in rec["nodes"] if n["id"] == "peer-1")
    assert peer["name"] == "peer", "低化身号不应覆盖已知记录"

    newer = dict(node, name="new-peer", incarnation=1001)
    a.handle_register({"node": newer})
    rec = a.snapshot()
    peer = next(n for n in rec["nodes"] if n["id"] == "peer-1")
    assert peer["name"] == "new-peer"


def test_stale_term_write_rejected(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    a.term = 9
    code, resp = a.handle_push({"task": "T-0004", "term": 8,
                                "agent": "zombie", "progress": 10})
    assert code == 409 and resp.get("stale") is True
    code, resp = a.handle_lease({"task": "T-0004", "term": 8,
                                 "agent": "zombie"})
    assert code == 409 and resp.get("stale") is True


# --------------------------------------------------------------------------- #
# HTTP / token / 长轮询
# --------------------------------------------------------------------------- #

def test_http_token_required_for_state(cluster_factory):
    a = cluster_factory("a", token="s3cret")
    assert wait_until(lambda: a.role == "leader")
    code, health = hc.http_json(f"{a.url}/api/cluster/health")
    assert code == 200 and health["ok"] is True
    code, _ = hc.http_json(f"{a.url}/api/cluster/state")
    assert code == 401
    code, body = hc.http_json(f"{a.url}/api/cluster/state", token="s3cret")
    assert code == 200 and body["state"]["leader"]["id"] == a.node_id
    code, _ = hc.http_json(f"{a.url}/api/cluster/register", "POST",
                           {"node": {"id": "x", "incarnation": 1}})
    assert code == 401


def test_long_poll_returns_on_new_generation(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    since = a.snapshot()["generation"]
    result = {}

    def poll():
        result["state"] = a.handle_wait({"since": [str(since)], "timeout": ["5"]})

    import threading
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    time.sleep(0.15)
    a.publish_progress({"task": "T-0005", "agent": "a", "progress": 50})
    t.join(timeout=4)
    assert result["state"]["generation"] > since
    assert result["state"]["progress"]["T-0005"]["progress"] == 50


def test_discovery_reply_shape(cluster_factory):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    reply = a.discovery_reply()
    assert reply["hive"] == "announce" and reply["proto"] == hc.PROTOCOL
    assert reply["cluster"] == a.config.cluster_id
    assert reply["node"]["id"] == a.node_id
    assert reply["leader"]["id"] == a.node_id
    assert reply["port"] == a.port


# --------------------------------------------------------------------------- #
# CLI / 离线状态
# --------------------------------------------------------------------------- #

def test_cli_status_offline_reads_snapshot(tmp_path):
    cfg = hc.Config(state_dir=tmp_path, name="offline", host="127.0.0.1",
                    port=0, advertise="127.0.0.1", discover=False)
    c = hc.Cluster(cfg)
    c.start()
    assert wait_until(lambda: c.role == "leader")
    c.stop()
    env = {**os_environ(), "HIVE_TESTING": "1", "HIVE_CLUSTER_STATE": str(tmp_path)}
    p = sh([sys.executable, HIVE, "--dir", str(ROOT), "cluster", "status",
            "--state-dir", str(tmp_path), "--json"], env=env, check=False)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["running"] is False
    assert data["state"]["self"]["name"] == "offline"
    assert data["state"]["term"] >= 1


def test_hive_parser_exposes_cluster():
    parser = hive.build_parser()
    args = parser.parse_args(["--dir", ".", "cluster", "status"])
    assert args.func is hive.cmd_cluster
    assert args.action == "status"


def os_environ():
    import os
    return dict(os.environ)


# --------------------------------------------------------------------------- #
# hive.py 的本地发布通道（worker 实际走的那条）
# --------------------------------------------------------------------------- #

def test_hive_py_publishes_progress_and_lease(cluster_factory, monkeypatch):
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    monkeypatch.setenv("HIVE_CLUSTER_PORT", str(a.port))
    monkeypatch.setenv("HIVE_CLUSTER_STATE", str(a.config.state_dir))
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")

    assert hive.cluster_publish_progress({
        "task": "T-0100", "agent": "fb", "state": "running",
        "progress": 61, "step": "写代码", "updated_at": hc._iso()})
    assert hive.cluster_publish_lease({
        "task": "T-0100", "agent": "fb",
        "lease_until": hc._iso(time.time() + 300),
        "lease_until_epoch": time.time() + 300})
    assert wait_until(lambda: (a.progress.get("T-0100") or {}).get("progress") == 61)
    assert wait_until(lambda: a.leases.get("T-0100"))

    assert hive.cluster_release_lease("T-0100")
    assert wait_until(lambda: a.leases["T-0100"].get("released") is True)


def test_init_copies_cluster_module(world):
    """协调仓 init 时把集群模块一起带上，否则每台机器都得手拷。"""
    assert (world.hive1 / "hive_cluster.py").exists()


def test_three_node_crash_elects_exactly_one_leader(cluster_factory):
    """3 节点主节点崩溃：剩下两台必须只选出一个主节点，不能各自以「1 节点多数」当选。

    这依赖跟随者之间的 gossip 心跳：主节点死后，B/C 仍能互相看到对方 alive，
    多数门槛才是 2 而不是 1。
    """
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    c = cluster_factory("c", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id and c.leader_id == a.node_id)
    assert wait_until(lambda: len([n for n in a.snapshot()["nodes"]
                                   if n["status"] == "alive"]) == 3)

    # 模拟 A 崩溃（不 resign）
    a._stop.set()
    a._http.shutdown()
    a._http.server_close()

    assert wait_until(lambda: b.role == "leader" or c.role == "leader", timeout=8)
    # 给可能已经启动的第二轮选举一个窗口；最终必须只有一个 leader。
    time.sleep(1.5)
    leaders = [n for n in (b, c) if n.role == "leader"]
    assert len(leaders) == 1, f"出现双主：{[(n.config.name, n.term) for n in (b, c)]}"
    winner = leaders[0]
    loser = c if winner is b else b
    assert wait_until(lambda: loser.leader_id == winner.node_id, timeout=4)
