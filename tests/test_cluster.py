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


def test_udp_discovery_roundtrip(tmp_path):
    """UDP 发现真实往返：一个节点起 listener，另一个 Discovery 发 discover 收 announce。

    不依赖两台机器/两个 listener 绑同一端口，避免 CI 上的 UDP 行为差异。
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    udp_port = probe.getsockname()[1]
    probe.close()

    cfg = hc.Config(state_dir=tmp_path / "udp-node", name="udp-node",
                    host="127.0.0.1", port=0, advertise="127.0.0.1",
                    discover=True, discovery_port=udp_port, seeds=[],
                    heartbeat_interval=0.1, peer_timeout=0.6,
                    election_min=0.3, leader_lease=0.7)
    c = hc.Cluster(cfg)
    try:
        c.start()
        wait_until(lambda: c.role == "leader", timeout=4)
        # 用一个独立身份的 Discovery 发包；c 的 listener 会忽略自己的 node_id。
        sender_cfg = hc.Config(state_dir=tmp_path / "udp-sender", name="udp-sender",
                               host="127.0.0.1", port=0, advertise="127.0.0.1",
                               discover=True, discovery_port=udp_port, seeds=[])
        sender = hc.Cluster(sender_cfg)
        d = hc.Discovery(sender)
        replies = d.discover(timeout=1.5)
        assert any((r.get("node") or {}).get("id") == c.node_id for r in replies), \
            f"没有收到本节点的 announce：{replies}"
        reply = next(r for r in replies if (r.get("node") or {}).get("id") == c.node_id)
        assert reply["leader"]["id"] == c.node_id
        assert reply["port"] == c.port
    finally:
        c.stop()


def test_observer_does_not_elect(cluster_factory):
    """P0：verify-only 机器设 electable=false，只观察、不参选。"""
    a = cluster_factory("a")
    b = cluster_factory("b", seeds=[a.url], electable=False)
    assert wait_until(lambda: a.snapshot().get("role") == "leader", timeout=6)
    assert wait_until(lambda: b.snapshot().get("role") != "leader", timeout=1)

    sb = b.snapshot()
    assert sb["self"]["electable"] is False
    assert sb["role"] != "leader"

    def _sees_b():
        nodes = {n.get("name"): n for n in a.snapshot().get("nodes") or []}
        return nodes.get("b")

    nb = wait_until(_sees_b, timeout=4)
    assert nb is not None and nb.get("electable") is False


def test_store_claim_replicates_and_failover(cluster_factory):
    """P2a：leader CAS + 复制到 follower；leader 挂后新主仍看得到 claim。"""
    a = cluster_factory("a", store_enabled=True)
    b = cluster_factory("b", seeds=[a.url], store_enabled=True)
    c = cluster_factory("c", seeds=[a.url], store_enabled=True)
    for node in (a, b, c):
        node.store.ingest_cards({"T-0001": {"id": "T-0001", "state": "ready",
                                           "owner": "", "title": "t",
                                           "lease_minutes": 10}})
    assert wait_until(lambda: a.snapshot().get("role") == "leader", timeout=6)
    assert wait_until(lambda: a.snapshot().get("alive") == 3, timeout=6)

    code, resp = hc.http_json(a.url.rstrip("/") + "/api/cluster/store/claim",
                              "POST", {"task": "T-0001", "agent": "A",
                                       "lease_minutes": 10, "term": a.term},
                              timeout=4.0)
    assert code == 200 and resp.get("ok"), resp
    lease = resp["claim"]["lease_id"]

    def _both_see():
        for n in (b, c):
            claim = n.store.claim_info("T-0001")
            if not claim or claim.get("owner") != "A":
                return False
        return True

    assert wait_until(_both_see, timeout=6)
    assert b.store.claim_info("T-0001")["lease_id"] == lease

    # 第二名抢同一张卡：leader 立即拒绝，不复制
    code, resp = hc.http_json(a.url.rstrip("/") + "/api/cluster/store/claim",
                              "POST", {"task": "T-0001", "agent": "B",
                                       "lease_minutes": 10, "term": a.term},
                              timeout=4.0)
    assert code == 409 and not resp.get("ok"), resp

    # 主节点挂掉，剩两台选新主；已复制的 claim 仍在
    a.stop()
    assert wait_until(lambda: b.snapshot().get("role") == "leader"
                      or c.snapshot().get("role") == "leader", timeout=8)
    leader = b if b.snapshot().get("role") == "leader" else c
    assert leader.store.claim_info("T-0001")["owner"] == "A"


def test_store_election_restriction(cluster_factory):
    """P2a：候选人的 store seq 落后时不能当选。"""
    c = cluster_factory("a", store_enabled=True)
    c.store.ingest_cards({"T-0001": {"id": "T-0001", "state": "ready"}})
    c.store.claim("T-0001", "A", term=c.term, lease_minutes=10)
    assert c.store.last_seq() >= 1

    code, resp = c.handle_vote({
        "term": c.term + 1,
        "candidate": {"id": "behind", "electable": True,
                      "incarnation": 1, "last_store_seq": 0},
    })
    assert code == 200
    assert resp.get("granted") is False
    assert resp.get("reason") == "store_behind", resp


def test_worker_claim_store_backend(world, cluster_factory, monkeypatch, tmp_path):
    """claim_backend=store：Worker.claim 走 leader CAS，不再 push Git claim 分支。"""
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001", repo="light")
    c = cluster_factory("a", hive_dir=world.hive1, store_enabled=True,
                        sync_enabled=False)
    assert wait_until(lambda: c.snapshot().get("role") == "leader", timeout=6)
    c.store.ingest_cards(hive.Hive(str(world.hive1)).cards())

    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "store")
    monkeypatch.setenv("HIVE_STORE_PATH", str(c.config.store_path))
    monkeypatch.setenv("HIVE_CLUSTER_PORT", str(c.port))
    monkeypatch.setenv("HIVE_CLUSTER_STATE", str(c.config.state_dir))
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")

    w = hive.Worker(str(world.hive1), "fb",
                    hive_state=str(tmp_path / "worker-state"))
    assert w.claim("T-0001") is True
    claim = c.store.claim_info("T-0001")
    assert claim and claim["owner"] == "fb", claim
    assert world.refs("refs/heads/claim/T-0001") == []


def test_store_dispatch_assigns_without_git(world, cluster_factory,
                                             monkeypatch, tmp_path):
    """P2b：store 模式下 dispatch 直接 store assign，Worker.pending 不依赖 Git inbox。"""
    world.add_agent("fb", "exec", caps=["freebsd"], command="true",
                    repos={"hive": world.code_clone("fb")})
    world.add_task("T-0001", repo="hive")
    c = cluster_factory("a", hive_dir=world.hive1, store_enabled=True,
                        sync_enabled=False)
    assert wait_until(lambda: c.snapshot().get("role") == "leader", timeout=6)
    c.store.ingest_cards(hive.Hive(str(world.hive1)).cards())

    monkeypatch.setenv("HIVE_CLAIM_BACKEND", "store")
    monkeypatch.setenv("HIVE_STORE_PATH", str(c.config.store_path))
    monkeypatch.setenv("HIVE_CLUSTER_PORT", str(c.port))
    monkeypatch.setenv("HIVE_CLUSTER_STATE", str(c.config.state_dir))
    monkeypatch.setenv("HIVE_CLUSTER_ALLOW_TEST", "1")

    h = hive.Hive(str(world.hive1))
    h.cmd_dispatch(apply=True, only_agent="fb")
    row = c.store.task("T-0001")
    assert row["state"] == "assigned" and row["owner"] == "fb", row
    # Git 卡面没有 assign（store 模式不再写 owner/state）
    assert world.card("T-0001")["state"] == "ready"
    assert world.card("T-0001")["owner"] in (None, "")

    w = hive.Worker(str(world.hive1), "fb",
                    hive_state=str(tmp_path / "worker-state"))
    assert any(tid == "T-0001" for tid, _ in w.pending())
    assert w.claim("T-0001") is True
    assert c.store.claim_info("T-0001")["owner"] == "fb"


def test_store_commit_failure_does_not_apply_locally(cluster_factory, monkeypatch):
    """prepare 成功但 commit 未达多数：leader 本地也不能先应用。"""
    c = cluster_factory("a", store_enabled=True)
    assert wait_until(lambda: c.snapshot().get("role") == "leader", timeout=6)
    c.store.ingest_cards({"T-0001": {"id": "T-0001", "state": "ready",
                                    "title": "t"}})
    monkeypatch.setattr(c, "_replicate_op", lambda op: True)
    monkeypatch.setattr(c, "_commit_op_replicated", lambda seq: False)
    code, resp = c.handle_store("claim", {"task": "T-0001", "agent": "A",
                                          "lease_minutes": 10, "term": c.term})
    assert code == 503 and resp.get("code") == "E_NO_QUORUM_COMMIT", resp
    assert c.store.claim_info("T-0001") is None
    assert c.store.commit_seq() == 0
    assert c.store.last_seq() == 1


# --------------------------------------------------------------------------- #
# 人工切主控：promote / prefer（「谁当主控」由人说了算）
# --------------------------------------------------------------------------- #

def test_promote_moves_leadership(cluster_factory):
    """人工把从属节点推上主控：它当选、原主控降级、任期推进。"""
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)
    old_term = a.term

    code, body = b.handle_promote({"force": True})
    assert code == 200 and body["ok"], body
    assert b.role == "leader"
    assert body["previous_leader"] == a.node_id
    assert wait_until(lambda: a.role == "follower"), "原主控必须降级，不能留下双主"
    assert wait_until(lambda: a.leader_id == b.node_id)
    assert b.term > old_term
    assert any(e["type"] == "leader_elected" for e in b.snapshot()["events"])


def test_promote_leader_is_noop(cluster_factory):
    """已经是主控时 promote 幂等，不该白涨一期任期。"""
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    term = a.term
    code, body = a.handle_promote({})
    assert code == 200 and body.get("already") is True
    assert a.term == term


def test_promote_observer_rejected(cluster_factory):
    """observer（electable=false）不能被推上主控。"""
    a = cluster_factory("a", electable=False)
    code, body = a.handle_promote({})
    assert code == 409 and not body["ok"]
    assert "observer" in body["error"]


def test_preferred_leader_gets_zero_delay(cluster_factory):
    """首选主控节点的选举延迟为 0：主控一失联它立刻发起选举。"""
    a = cluster_factory("a", preferred_leader="a")
    assert wait_until(lambda: a.role == "leader")
    with a.lock:
        assert a._election_delay_locked() == 0.0
        assert a._preferred_leader_id_locked() == a.node_id


def test_non_preferred_node_yields_a_window(cluster_factory):
    """非首选节点要让出一个选举窗口，避免和首选节点同轮打平。"""
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url], preferred_leader="a")
    assert wait_until(lambda: b.leader_id == a.node_id)
    assert wait_until(lambda: b._preferred_leader_id_locked() == a.node_id)
    with b.lock:
        delay = b._election_delay_locked()
    # 默认延迟是 election_min + rank*jitter；非首选至少再加一个 election_min
    assert delay >= b.config.election_min * 1.9, delay


def test_preferred_offline_falls_back(cluster_factory):
    """首选节点不在线 / 不存在时退回默认延迟，不能拖慢整个集群。"""
    a = cluster_factory("a", preferred_leader="ghost-node")
    assert wait_until(lambda: a.role == "leader")
    with a.lock:
        # 只剩自己一个 voter，rank=0 → 就是 election_min，没有额外惩罚
        assert a._election_delay_locked() == a.config.election_min


def test_force_vote_overrides_store_behind(cluster_factory):
    """人工夺权时允许候选人 store 落后（git 才是事实账），且留下审计事件。"""
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)

    class _FakeStore:
        def commit_seq(self):
            return 99

    cand = b._self_node()
    cand["last_store_seq"] = 1  # 比 A 的 99 落后
    a.store = _FakeStore()
    try:
        term = a.term + 5
        code, body = a.handle_vote({"term": term, "candidate": cand})
        assert body.get("granted") is False
        assert body.get("reason") == "store_behind"

        code, body = a.handle_vote({"term": term, "candidate": cand, "force": True})
        assert body.get("granted") is True, body
        assert any(e["type"] == "leader_forced" for e in a.snapshot()["events"]), \
            "强制放行必须留审计痕迹"
    finally:
        a.store = None


def test_force_vote_can_be_disabled(cluster_factory):
    """HIVE_CLUSTER_ALLOW_FORCE_PROMOTE=0 时，force 也压不过 store 闸门。

    给「宁可没主控也不接受落后节点」这种更严的策略留出口。
    """
    a = cluster_factory("a")
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url])
    assert wait_until(lambda: b.leader_id == a.node_id)

    class _FakeStore:
        def commit_seq(self):
            return 99

    cand = b._self_node()
    cand["last_store_seq"] = 1
    a.store = _FakeStore()
    a.config.allow_forced_promote = False
    try:
        code, body = a.handle_vote({"term": a.term + 5, "candidate": cand,
                                    "force": True})
        assert body.get("granted") is False
        assert body.get("reason") == "store_behind"
    finally:
        a.store = None


def test_hive_parser_exposes_promote_and_prefer():
    parser = hive.build_parser()
    for action in ("leader", "promote", "prefer"):
        args = parser.parse_args(["--dir", ".", "cluster", action])
        assert args.action == action
        assert args.func is hive.cmd_cluster
    args = parser.parse_args(["--dir", ".", "cluster", "promote", "--node", "fb98"])
    assert args.node == "fb98"
    args = parser.parse_args(["--dir", ".", "cluster", "start", "--leader"])
    assert args.leader is True


# --------------------------------------------------------------------------- #
# OSPF 式主控优先级：值大者优先、不抢占在位主控、0 = 不参选
# --------------------------------------------------------------------------- #

def test_priority_is_broadcast_in_self_node(cluster_factory):
    """优先级写在节点自述里随心跳广播 —— 这是它优于 preferred_leader 的地方：
    每台只需声明「我是几」，不需要在别人机器上再配一遍「谁是首选」。"""
    a = cluster_factory("a", leader_priority=2)
    assert a._self_node()["priority"] == 2
    assert a._self_node()["electable"] is True


def test_higher_priority_gets_shorter_delay(cluster_factory):
    """优先级高的等得更短，于是先发起选举、先当选。"""
    a = cluster_factory("a", leader_priority=1)
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url], leader_priority=5)
    assert wait_until(lambda: b.leader_id == a.node_id)
    # 双方都要在对方「活节点」表里，延迟才是在同一张表上算出来的
    assert wait_until(lambda: a._node_priority_locked(b.node_id) == 5)
    with a.lock:
        da = a._election_delay_locked()
    with b.lock:
        db = b._election_delay_locked()
    assert db < da, (da, db)


def test_same_priority_falls_back_to_node_id(cluster_factory):
    """同优先级按 node_id 定序（相当于 OSPF 用 Router ID 打平手），
    保证「谁先跑」是确定的，不会两边同轮打平后反复重选。"""
    a = cluster_factory("a", leader_priority=3)
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url], leader_priority=3)
    assert wait_until(lambda: b.leader_id == a.node_id)
    with a.lock, b.lock:
        da = a._election_delay_locked()
        db = b._election_delay_locked()
    assert da != db
    # node_id 字典序小的排前面，等待更短
    if a.node_id < b.node_id:
        assert da < db
    else:
        assert db < da


def test_priority_zero_never_starts_election(cluster_factory):
    """0 沿用 OSPF 的 DR priority 0 语义：这台不主动竞选主控。"""
    a = cluster_factory("a", leader_priority=1)
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url], leader_priority=0)
    assert wait_until(lambda: b.leader_id == a.node_id)
    with b.lock:
        assert b._election_delay_locked() == float("inf")


def test_all_zero_priority_still_elects_a_leader(cluster_factory):
    """全集群都是 0 时必须退化成人人 1。

    否则没有任何节点愿意发起选举，集群永远没有主控 —— 这种「全都配了 0」
    在真实环境里一定会发生（比如有人把优先级理解成「越小越优先」全填 0），
    死锁比不理解更糟。
    """
    a = cluster_factory("a", leader_priority=0)
    assert wait_until(lambda: a.role == "leader")
    with a.lock:
        assert a._election_delay_locked() < float("inf")


def test_priority_does_not_preempt_sitting_leader(cluster_factory):
    """OSPF 的 DR 不抢占：高优先级节点后加入，不会把在位主控挤下来。

    这条是「稳」的关键。若允许抢占，给一台机器设了 2，它每次重启都会把
    主控抢走，集群跟着抖一轮 —— 而重启往往恰是在排障的时候。
    """
    a = cluster_factory("a", leader_priority=1)
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url], leader_priority=9)
    assert wait_until(lambda: b.leader_id == a.node_id)
    time.sleep(1.0)   # 给足一个选举超时，让「想抢」的念头有时间发生
    assert a.role == "leader", "优先级高不等于可以抢占在位主控"
    assert b.role == "follower"


def test_priority_decides_next_election(cluster_factory):
    """主控真的消失之后，才按优先级选下一个 —— 这时高优先级那台接手。"""
    a = cluster_factory("a", leader_priority=1)
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url], leader_priority=9)
    assert wait_until(lambda: b.leader_id == a.node_id)

    a.stop()
    assert wait_until(lambda: b.role == "leader", timeout=10.0), \
        "高优先级节点应在新一轮选举里当选"


def test_hive_parser_exposes_priority():
    parser = hive.build_parser()
    args = parser.parse_args(["--dir", ".", "cluster", "priority"])
    assert args.action == "priority" and args.func is hive.cmd_cluster
    args = parser.parse_args(["--dir", ".", "cluster", "priority", "--priority", "2"])
    assert args.priority == 2
    args = parser.parse_args(["--dir", ".", "cluster", "serve", "--priority", "0"])
    assert args.priority == 0


# --------------------------------------------------------------------------- #
# OSPF DR priority 的取值域 0..255，以及 0 = 硬资格（不是「懒得先开口」）
# --------------------------------------------------------------------------- #

def test_priority_domain_is_0_to_255(cluster_factory):
    """OSPF 的 DR/BDR 接口优先级是 8bit：0..255，默认 1。"""
    assert (hc.MIN_LEADER_PRIORITY, hc.MAX_LEADER_PRIORITY,
            hc.DEFAULT_LEADER_PRIORITY) == (0, 255, 1)
    assert hc.normalize_leader_priority(None) == 1
    assert hc.normalize_leader_priority("abc") == 1
    assert hc.normalize_leader_priority(-7) == 0
    assert hc.normalize_leader_priority(999) == 255
    # 从 config.json / 环境变量 / 构造参数来的越界值都要被规整，不能混进排序
    a = cluster_factory("a", leader_priority=300)
    assert a.config.leader_priority == 255
    assert a._self_node()["priority"] == 255
    b = cluster_factory("b", seeds=[a.url], leader_priority=-3)
    assert b.config.leader_priority == 0


def test_cli_rejects_out_of_range_priority(cluster_factory, capsys):
    """CLI 要当场报错，不能把 300 静默当成 255 —— 输错得让人看见。"""
    cfg = cluster_factory("a").config
    assert hc._cmd_priority(cfg, 256, restart=False) == 2
    assert "0..255" in capsys.readouterr().err
    assert hc._cmd_priority(cfg, -1, restart=False) == 2
    assert cfg.leader_priority == 1, "被拒的值不能写进配置"


def test_serve_args_reject_out_of_range_priority():
    """serve/start --priority 也走同一道校验（ClusterError 会被 CLI 友好打印）。"""
    with pytest.raises(hc.ClusterError):
        hc._config_from_args(argparse.Namespace(priority=256))
    with pytest.raises(hc.ClusterError):
        hc._config_from_args(argparse.Namespace(priority=-1))


def test_abstaining_candidate_cannot_get_votes(cluster_factory):
    """0 是硬资格：集群侧要拒票，不能只靠候选人自己 delay=inf 自觉。

    老代码 / 配错的节点抢跑时，正优先级节点必须挡住它，否则照样凑够多数当上主控。
    """
    a = cluster_factory("a", leader_priority=2)          # 投票方
    b = cluster_factory("b", seeds=[a.url], leader_priority=0)  # 已弃权
    assert wait_until(lambda: b.leader_id == a.node_id)
    code, resp = a.handle_vote({"term": a.term + 1,
                                "candidate": b._self_node()})
    assert code == 200
    assert resp["granted"] is False
    assert resp["reason"] == "candidate_priority_zero"


def test_promote_refused_for_abstaining_node(cluster_factory):
    """人工 promote 也不越过硬资格，且要提前给出可照做的提示。"""
    a = cluster_factory("a", leader_priority=2)
    assert wait_until(lambda: a.role == "leader")
    b = cluster_factory("b", seeds=[a.url], leader_priority=0)
    assert wait_until(lambda: b.leader_id == a.node_id)
    code, resp = b.handle_promote({})
    assert code == 409 and resp["ok"] is False
    assert "priority=0" in resp["error"]
    assert "priority --priority 1" in resp["error"]
    assert b.role != "leader"


def test_preferred_pointing_at_abstaining_node_does_not_deadlock(cluster_factory):
    """preferred 指向一台弃权节点时，全集群不能一起给它让路空等。"""
    a = cluster_factory("a", leader_priority=2)
    b = cluster_factory("b", seeds=[a.url], leader_priority=0)
    assert wait_until(lambda: b.leader_id == a.node_id)
    assert wait_until(lambda: a._node_priority_locked(b.node_id) == 0)
    with a.lock:
        a.config.preferred_leader = b.node_id
        d_with = a._election_delay_locked()
        a.config.preferred_leader = ""
        d_without = a._election_delay_locked()
    assert d_with == d_without


def test_zero_jitter_still_orders_by_priority(cluster_factory):
    """election_jitter 被设成 0 时，优先级不能退化成「同时开跑、谁快谁当」。"""
    a = cluster_factory("a", leader_priority=1)
    b = cluster_factory("b", seeds=[a.url], leader_priority=9)
    assert wait_until(lambda: b.leader_id == a.node_id)
    a.config.election_jitter = 0.0
    b.config.election_jitter = 0.0
    with a.lock:
        da = a._election_delay_locked()
    with b.lock:
        db = b._election_delay_locked()
    assert db < da, (da, db)


# --------------------------------------------------------------------------- #
# 重启 / 启动自检 / 幽灵节点防护（2026-11-06 Windows 节点重启误报复盘）
# --------------------------------------------------------------------------- #

def test_startup_timeout_is_longer_on_windows(monkeypatch):
    """Windows 冷启动 > 8s 曾让 restart 误报失败；窗口要更长且可由 env 覆盖。"""
    monkeypatch.delenv("HIVE_CLUSTER_STARTUP_TIMEOUT", raising=False)
    monkeypatch.setattr(hc.os, "name", "nt")
    assert hc._startup_timeout() == 30.0
    monkeypatch.setattr(hc.os, "name", "posix")
    assert hc._startup_timeout() == 15.0
    monkeypatch.setenv("HIVE_CLUSTER_STARTUP_TIMEOUT", "45")
    assert hc._startup_timeout() == 45.0


def test_spawn_alive_but_slow_is_not_a_failure(monkeypatch, tmp_path):
    """自检窗口用尽但子进程仍活着 = 启动慢，不是失败，返回 0 且不再 spawn。"""
    cfg = hc.Config(state_dir=tmp_path / "st", name="slow",
                    host="127.0.0.1", port=0, advertise="127.0.0.1",
                    discover=False, hive_dir=tmp_path / "repo")

    class FakeProc:
        pid = 4242
        def poll(self):
            return None          # 一直活着

    monkeypatch.setattr(hc, "_local_health", lambda *a, **k: None)
    monkeypatch.setattr(hc, "_startup_timeout", lambda: 0.05)
    monkeypatch.setattr(hc.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hc.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(hc, "_tail_text", lambda *a, **k: "ConnectionResetError noise")
    rc = hc._spawn_background(cfg, argparse.Namespace())
    assert rc == 0, "进程还活着却被判成启动失败，会导致调用方反复 restart"


def test_spawn_dead_process_is_a_failure(monkeypatch, tmp_path):
    """进程真的退了才返回非 0。"""
    cfg = hc.Config(state_dir=tmp_path / "st2", name="dead",
                    host="127.0.0.1", port=0, advertise="127.0.0.1",
                    discover=False, hive_dir=tmp_path / "repo2")

    class FakeProc:
        pid = 4343
        def poll(self):
            return 1

    monkeypatch.setattr(hc, "_local_health", lambda *a, **k: None)
    monkeypatch.setattr(hc.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(hc, "_tail_text", lambda *a, **k: "real traceback")
    assert hc._spawn_background(cfg, argparse.Namespace()) == 1


def test_http_handler_swallows_connection_reset(monkeypatch):
    """探活/旧连接断开抛 10054 时不能把 traceback 刷进日志（会被误读成崩溃）。"""
    Handler = hc.build_handler(object())

    def boom(self):
        raise ConnectionResetError(10054, "forcibly closed")

    monkeypatch.setattr(hc.BaseHTTPRequestHandler, "handle", boom)
    d = Handler.__new__(Handler)
    d.close_connection = False
    Handler.handle(d)          # 不该抛
    assert d.close_connection is True


def test_load_state_dedupes_same_url_ghost(cluster_factory):
    """磁盘里同一 URL 的两个身份，加载后只留一个，且不再指向被删的 leader。"""
    a = cluster_factory("dedupe")
    ghost = "ghost-old-id"
    real = a.node_id
    state_path = a.config.state_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "term": 62,
        "nodes": {
            real: {"id": real, "url": "http://10.0.0.9:8788",
                   "incarnation": 200, "name": "real"},
            ghost: {"id": ghost, "url": "http://10.0.0.9:8788",
                    "incarnation": 100, "name": "ghost"},
        },
        "leader_id": ghost,
        "leader_url": "http://10.0.0.9:8788",
        "leader_lease_until_epoch": 0.0,
    }), encoding="utf-8")

    cfg = hc.Config(state_dir=a.config.state_dir, name=a.config.name,
                    host="127.0.0.1", port=0, advertise="127.0.0.1",
                    discover=False, hive_dir=a.config.hive_dir)
    c2 = hc.Cluster(cfg)
    with c2.lock:
        # __init__ 之后 _put_self_node() 会用本机真实 URL 覆盖 self 记录，
        # 所以这里只验「幽灵被删 + leader 不再指向它」。
        assert ghost not in c2.nodes
        assert c2.leader_id != ghost
        # 再直接验一次去重本身：同 URL 只留一个身份。
        c2.nodes[real] = {"id": real, "url": "http://10.0.0.9:8788",
                          "incarnation": 200, "name": "real"}
        c2.nodes[ghost] = {"id": ghost, "url": "http://10.0.0.9:8788",
                           "incarnation": 100, "name": "ghost"}
        c2.leader_id = ghost
        c2._dedupe_node_urls_locked()
        urls = [str(r.get("url") or "").rstrip("/") for r in c2.nodes.values()]
        assert urls.count("http://10.0.0.9:8788") == 1, urls
        assert ghost not in c2.nodes
        assert c2.leader_id != ghost


def test_update_node_rejects_same_url_ghost(cluster_factory):
    """心跳把同 URL 的旧身份送回来时拒绝续命，避免 _adopt_leader touch 复活。"""
    a = cluster_factory("ghostguard")
    mine = {"id": "real-node", "url": "http://10.0.0.9:8788",
            "incarnation": 200, "name": "real"}
    ghost = {"id": "ghost-node", "url": "http://10.0.0.9:8788",
             "incarnation": 100, "name": "ghost"}
    with a.lock:
        assert a._update_node_locked(mine) is True
        assert a._update_node_locked(ghost) is False
        assert "ghost-node" not in a.nodes


# --------------------------------------------------------------------------- #
# 幽灵 leader 死循环（2026-11-09）：follower 互相喂 .103 的租约，谁都不选举
# --------------------------------------------------------------------------- #

def _offline_cluster(tmp_path, name="offline", **kw):
    """只要内存状态、不启 HTTP/后台循环，方便确定性地摆弄租约。"""
    cfg = hc.Config(
        state_dir=tmp_path / name, name=name,
        host="127.0.0.1", port=0, advertise="127.0.0.1",
        discover=False, hive_dir=tmp_path / "repo",
        election_min=kw.pop("election_min", 999.0),
        leader_lease=kw.pop("leader_lease", 5.0),
        **kw)
    return hc.Cluster(cfg)


def _ghost_record():
    return {"id": "ghost-103", "name": "laiweilideMacBook-Air",
            "url": "http://192.168.0.103:8788", "incarnation": 1794110903088,
            "priority": 1, "electable": True}


def test_restart_drops_stale_leader_even_with_live_lease(tmp_path):
    """磁盘里租约**还没过期**的幽灵主控，重启后也不能被读回内存。

    旧代码只在「租约过期」或「leader 是自己」时才清，于是手工改盘后一重启
    幽灵立刻复活 —— 操作手册里反复复发的正是这一条。
    """
    d = tmp_path / "stale"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({
        "term": 2150,
        "leader_id": "ghost-103",
        "leader_url": "http://192.168.0.103:8788",
        "leader_lease_until_epoch": time.time() + 300,
        "nodes": {},
    }), encoding="utf-8")
    c = _offline_cluster(tmp_path, name="stale")
    assert c.leader_id is None
    assert c.leader_url is None
    assert c.leader_lease_until == 0.0


def test_follower_relay_cannot_reissue_ghost_lease(tmp_path):
    """转述只能传递同一个绝对到期时间，不能每跳重新签发 now+lease。"""
    c = _offline_cluster(tmp_path, name="relay")
    with c.lock:
        c.term = 100
        assert c._adopt_leader_locked(_ghost_record(), 100,
                                      refresh_lease=True) is True
        signed = c.leader_lease_until
        heartbeat_at = c._last_leader_heartbeat_at
        assert signed > time.time()
        assert heartbeat_at > 0
        time.sleep(0.05)
        for _ in range(6):
            relayed = dict(_ghost_record(),
                           lease_until_epoch=c.leader_lease_until)
            assert c._adopt_leader_locked(relayed, 100,
                                          refresh_lease=False) is True
            assert c.leader_lease_until <= signed, \
                "跟随者转述把幽灵租约往后推了"
            assert c._last_leader_heartbeat_at == heartbeat_at, \
                "转述被误当成主控本人心跳"


def test_ghost_leader_cleared_without_direct_heartbeat(tmp_path):
    """即使有人不断把租约撑长，没有主控本人心跳也必须判定为幽灵并清掉。"""
    c = _offline_cluster(tmp_path, name="ghostclear")
    with c.lock:
        c.term = 100
        c._adopt_leader_locked(_ghost_record(), 100, refresh_lease=True)
        assert c.leader_id == "ghost-103"
        c._last_leader_heartbeat_at = time.time() - 100
        c.leader_lease_until = time.time() + 100   # 互相喂出来的长租约
    c._maintenance_once()
    with c.lock:
        assert c.leader_id is None
        assert c.leader_lease_until == 0.0


def test_handle_heartbeat_signs_lease_only_for_real_leader(tmp_path):
    """只有 body.leader is True 的主控心跳能签租约；follower 互探不能。"""
    c = _offline_cluster(tmp_path, name="hb")
    peer = {"id": "peer-1", "name": "peer", "url": "http://127.0.0.1:9999",
            "incarnation": 10, "priority": 1, "electable": True}
    with c.lock:
        c.term = 10
        code, resp = c.handle_heartbeat(
            {"node": peer, "term": 10, "leader": False})
        assert code == 200
        assert resp["is_leader"] is False
        assert resp["node_id"] == c.node_id
        assert c.leader_lease_until == 0.0, "跟随者心跳不能签发租约"

        code, resp = c.handle_heartbeat(
            {"node": peer, "term": 10, "leader": True})
        assert code == 200
        assert c.leader_id == "peer-1"
        assert c.leader_lease_until > time.time()
        assert c._last_leader_heartbeat_at > 0


def test_reply_is_from_leader_distinguishes_real_and_relayed():
    """新版用 is_leader，老版本用 role 兜底；两者都不能把 follower 当主控。"""
    assert hc._reply_is_from_leader({"is_leader": True}) is True
    assert hc._reply_is_from_leader(
        {"is_leader": False, "leader": {"id": "ghost"}}) is False
    assert hc._reply_is_from_leader({"role": "leader"}) is True
    assert hc._reply_is_from_leader({"role": "follower"}) is False
    assert hc._reply_is_from_leader(None) is False


def test_register_reply_with_mismatched_leader_is_rejected(tmp_path):
    """「我 role=leader，但我认为 leader 是 .103」这种不一致状态不能灌回来。

    现场旧版本节点就是这样：自己短暂当选，leader_id 却仍被同伴快照带偏到
    已下线的 .103；它给 register 返回 200，新节点一注册就把幽灵读回磁盘。
    """
    c = _offline_cluster(tmp_path, name="mismatch")
    snap = {
        "term": 100, "role": "leader",
        "self": {"id": "peer-101", "url": "http://192.168.0.101:8788"},
        "leader": dict(_ghost_record(), lease_until_epoch=time.time() + 5),
        "nodes": [], "events": [], "progress": {}, "leases": {},
    }
    with c.lock:
        c.term = 100
        c._merge_snapshot_locked(snap, from_leader=True, sender_id="peer-101")
        assert c.leader_id is None
        assert c.leader_lease_until == 0.0
        # 换成「本人签发」的一致快照，正常采纳
        ok = dict(snap)
        ok["leader"] = {"id": "peer-101", "name": "peer",
                        "url": "http://192.168.0.101:8788",
                        "incarnation": 10,
                        "lease_until_epoch": time.time() + 5}
        c._merge_snapshot_locked(ok, from_leader=True, sender_id="peer-101")
        assert c.leader_id == "peer-101"


def test_leader_heartbeat_rejects_identity_mismatch(tmp_path):
    """用旧 node.json 的身份（声明 .103）从 .110 发主控心跳 —— 必须拒绝。

    现场最终取证：幽灵是 handle_heartbeat 的 `leader: True` 心跳写进来的，
    一台 192.168.0.110 的机器沿用旧 Mac 身份，url 指向早已下线的 .103。
    伪造身份的请求连 term 都不许推进，否则会把本机选举一遍遍打断。
    """
    c = _offline_cluster(tmp_path, name="spoof")
    ghost_node = dict(_ghost_record())
    with c.lock:
        c.term = 100
        c._become_leader_locked()
        assert c.role == "leader"
    code, resp = c.handle_heartbeat(
        {"node": ghost_node, "term": 9999, "leader": True},
        peer_ip="192.168.0.110")
    assert code == 200
    assert resp["term"] == 100
    with c.lock:
        assert c.term == 100, "伪造心跳推进了 term"
        assert c.role == "leader"
        assert c.leader_id == c.node_id
        assert "ghost-103" not in c.nodes
    # 来源 IP 与自述一致时才正常采纳
    c2 = _offline_cluster(tmp_path, name="spoof-ok")
    with c2.lock:
        c2.term = 100
        c2.handle_heartbeat(
            {"node": ghost_node, "term": 100, "leader": True},
            peer_ip="192.168.0.103")
        assert c2.leader_id == "ghost-103"


def test_node_url_host_matches_only_checks_ip_literals():
    assert hc._node_url_host_matches(
        "http://192.168.0.103:8788", "192.168.0.103") is True
    assert hc._node_url_host_matches(
        "http://192.168.0.103:8788", "192.168.0.101") is False
    assert hc._node_url_host_matches("http://127.0.0.1:8788", "127.0.0.1") is True
    assert hc._node_url_host_matches("http://192.168.0.9:8788",
                                     "::ffff:192.168.0.9") is True
    assert hc._node_url_host_matches("http://myhost.local:8788", "10.0.0.1") is True


def test_follower_snapshot_cannot_refresh_ghost_last_seen(tmp_path):
    """存活只认本机直接观测，不认跟随者转述的 last_seen。

    否则两台 follower 互相把幽灵刷成「刚刚」，它永远占一票，quorum 被抬高，
    选举就一直凑不够多数（现场 term 从 2170 一路涨到 2259）。
    """
    c = _offline_cluster(tmp_path, name="lastseen")
    ghost = dict(_ghost_record())
    with c.lock:
        c.term = 100
        assert c._update_node_locked(ghost, touch=True) is True
        stale = time.time() - 100
        c.nodes["ghost-103"]["last_seen_epoch"] = stale
        snap = {"term": 100,
                "nodes": [dict(ghost, last_seen_epoch=time.time())],
                "events": [], "progress": {}, "leases": {}}
        c._merge_snapshot_locked(snap, from_leader=False)
        assert abs(c.nodes["ghost-103"]["last_seen_epoch"] - stale) < 1
        # 主控本人发来的快照才有权刷新存活时间
        c._merge_snapshot_locked(snap, from_leader=True, sender_id="ghost-103")
        assert c.nodes["ghost-103"]["last_seen_epoch"] > stale + 50


def test_unreachable_leader_is_cleared_even_with_long_lease(tmp_path):
    """主控地址一直拨不通，就算租约被转述撑得很长也得清掉。"""
    c = _offline_cluster(tmp_path, name="unreachable")
    with c.lock:
        c.term = 100
        c._adopt_leader_locked(_ghost_record(), 100, refresh_lease=True)
        c._leader_fail_at = time.time() - 100
        c._last_leader_heartbeat_at = time.time()
        c.leader_lease_until = time.time() + 100
    c._maintenance_once()
    with c.lock:
        assert c.leader_id is None
        assert c.leader_lease_until == 0.0


def test_old_follower_replies_cannot_plant_ghost_leader(tmp_path, monkeypatch):
    """模拟现场：旧节点回复 role=follower、leader 指向已下线的 .103。"""
    c = _offline_cluster(tmp_path, name="oldreply")
    resp = {
        "ok": True, "term": 100, "role": "follower",
        "leader": dict(_ghost_record(), lease_until_epoch=time.time() + 5),
        "nodes": [], "events": [], "progress": {}, "leases": {},
    }
    monkeypatch.setattr(hc, "http_json", lambda *a, **k: (200, resp))
    with c.lock:
        c.term = 100
    c._peer_heartbeat(
        [{"id": "peer-101", "url": "http://192.168.0.101:8788"}], {})
    c._follower_heartbeat("http://192.168.0.103:8788", {})
    with c.lock:
        assert c.leader_id is None, "跟随者回复把幽灵主控种回来了"
        assert c.leader_lease_until == 0.0


# --------------------------------------------------------------------------- #
# 优先级写在 config.json 里：默认 1，想让某台优先当家就写 2
# --------------------------------------------------------------------------- #

def test_leader_priority_is_read_from_config_json(tmp_path, monkeypatch):
    """每台机器只改自己的 config.json 就能声明优先级，不用在别人机器上配。"""
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.delenv("HIVE_CLUSTER_PRIORITY", raising=False)

    def fresh():
        return hc.Config(state_dir=d, name="n", discover=False,
                         hive_dir=tmp_path / "repo")

    assert fresh().leader_priority == 1, "不写就是 OSPF 默认值 1"
    (d / "config.json").write_text(
        json.dumps({"leader_priority": 2}), encoding="utf-8")
    assert fresh().leader_priority == 2
    monkeypatch.setenv("HIVE_CLUSTER_PRIORITY", "7")
    assert fresh().leader_priority == 7, "环境变量优先于 config.json"
