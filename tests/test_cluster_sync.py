"""P0：repo.updated 事件、land 事件载荷、RepoSyncAgent 的安全快进。"""
from __future__ import annotations

import time

import hive
import hive_cluster as hc
from conftest import sh


def _head(path) -> str:
    return sh(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()


def test_cluster_event_roundtrip(tmp_path):
    cfg = hc.Config(state_dir=tmp_path / "c", name="a", host="127.0.0.1",
                    port=0, advertise="127.0.0.1", discover=False, seeds=[],
                    heartbeat_interval=0.08, peer_timeout=0.5,
                    dead_timeout=1.2, election_min=0.3, election_jitter=0.1,
                    leader_lease=0.7, lease_grace=2.0,
                    hive_dir=tmp_path / "repo")
    c = hc.Cluster(cfg)
    c.start()
    try:
        body = c.publish_event("repo.updated", task="T-0001",
                               data={"repos": [{"name": "light", "sha": "abc"}]})
        ev = body.get("event") or {}
        assert ev.get("type") == "repo.updated"
        assert ev.get("task") == "T-0001"
        assert ev.get("data", {}).get("repos", [{}])[0].get("name") == "light"

        snap = c.snapshot()
        assert any(e.get("type") == "repo.updated" for e in snap.get("events") or [])

        code, _ = c.handle_event({"type": "not-allowed"})
        assert code == 400
    finally:
        c.stop()


def test_sync_agent_fast_forwards(world, tmp_path):
    world.add_agent("fb", mode="manual")
    repo = world.code_clone("fb")
    other = world.code_clone("other")
    (other / "sync.txt").write_text("from event\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=other, env=world.git_env)
    sh(["git", "commit", "-q", "-m", "sync event"], cwd=other, env=world.git_env)
    sh(["git", "push", "-q", "origin", "main"], cwd=other, env=world.git_env)
    target = _head(repo.parent / "light-other")
    assert target != _head(repo)

    cfg = hc.Config(state_dir=tmp_path / "sync-state", hive_dir=world.hive1,
                    discover=False, port=0)

    class FakeCluster:
        config = cfg

        @staticmethod
        def snapshot():
            return {"events": []}

    agent = hc.RepoSyncAgent(FakeCluster())
    agent._sync_repo("light", target)

    assert _head(repo) == target
    status = agent.status()["repos"]["light"]
    assert status["status"] in ("fast-forwarded", "up-to-date")
    assert status["sha"] == target


def test_after_land_emits_repo_event(world, monkeypatch):
    world.add_agent("fb", mode="manual")
    h = hive.Hive(str(world.hive1))
    captured: dict = {}

    def _fake_event(etype, task="", data=None):
        captured.update({"type": etype, "task": task, "data": data or {}})
        return True

    monkeypatch.setattr(hive, "cluster_publish_event", _fake_event)
    monkeypatch.setattr(hive.Hive, "_sync_one_repo",
                        lambda self, name, path: {"status": "up-to-date"})

    h._after_land({"task": "T-0001", "repo": "light", "head": "deadbeef"})
    assert captured["type"] == "repo.updated"
    assert captured["task"] == "T-0001"
    names = {r.get("name") for r in captured["data"].get("repos") or []}
    assert "light" in names
    assert "hive" in names


def test_cluster_starts_mcp_http(world, tmp_path):
    import json
    import time
    from urllib import request as urlrequest

    import mcp_auth
    import hive_cluster as hc

    world.add_agent("fb", mode="manual")
    token_file = tmp_path / "state" / "mcp_tokens.json"
    token = mcp_auth.enroll("fb", "worker", path=token_file)
    cfg = hc.Config(
        state_dir=tmp_path / "state", name="mcp-node", host="127.0.0.1",
        port=0, advertise="127.0.0.1", discover=False, seeds=[],
        heartbeat_interval=0.08, peer_timeout=0.5, dead_timeout=1.2,
        election_min=0.3, election_jitter=0.1, leader_lease=0.7,
        lease_grace=2.0, hive_dir=world.hive1, electable=True,
        sync_enabled=False, mcp_enabled=True, mcp_port=0,
        mcp_write_dir=tmp_path / "write", mcp_read_dir=tmp_path / "read",
    )
    c = hc.Cluster(cfg)
    c.start()
    try:
        end = time.time() + 8
        while time.time() < end and not (c.mcp_service and c.mcp_service.port):
            time.sleep(0.1)
        assert c.mcp_service is not None, c.mcp_service
        url = f"http://127.0.0.1:{c.mcp_service.port}/mcp"
        req = urlrequest.Request(
            url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-03-26",
                                        "capabilities": {}, "clientInfo": {"name": "t"}}}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            method="POST")
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        assert body["result"]["protocolVersion"] == "2025-03-26"
    finally:
        c.stop()


# ---- T-0541：事件消费路径节流 ------------------------------------------- #

def _stub_agent(tmp_path, monkeypatch, calls, *, interval=None):
    """造一个不打真实 git 的 RepoSyncAgent，返回 (agent, 假 cluster)。"""
    cfg = hc.Config(state_dir=tmp_path / "sync-state", hive_dir=tmp_path / "repo",
                    discover=False, port=0)
    monkeypatch.setattr(cfg, "sync_enabled", True, raising=False)

    class FakeCluster:
        config = cfg
        events = []

        @classmethod
        def snapshot(cls):
            return {"events": list(cls.events)}

    class FakeHive:
        @staticmethod
        def _sync_one_repo(name, path):
            calls.append(name)
            return {"status": "up-to-date"}

    agent = hc.RepoSyncAgent(FakeCluster())
    if interval is not None:
        agent.event_min_interval_s = interval
    monkeypatch.setattr(agent, "_repos", lambda: {
        "light": tmp_path / "light", "hive": tmp_path / "hive"})
    monkeypatch.setattr(agent, "_hive_obj", lambda: FakeHive())
    monkeypatch.setattr(agent, "_git_rev", lambda path, ref: "a" * 12)
    return agent, FakeCluster


def _emit(cluster, event_id, repos):
    cluster.events.append({
        "id": str(event_id), "type": "repo.updated", "data": {"repos": repos},
    })


def test_event_storm_throttled_per_repo(tmp_path, monkeypatch):
    """同一 repo 的 N 条事件在最小间隔内只跑 1 次 git。"""
    calls = []
    agent, cluster = _stub_agent(tmp_path, monkeypatch, calls, interval=180)
    for i in range(10):
        _emit(cluster, i, [{"name": "light", "sha": "b" * 12}])
        agent._consume_events()

    assert calls == ["light"], calls
    assert agent.status()["last_event_id"] == "9"


def test_event_throttle_independent_per_repo(tmp_path, monkeypatch):
    """不同 repo 互不阻塞：同轮两条仓的事件都同步。"""
    calls = []
    agent, cluster = _stub_agent(tmp_path, monkeypatch, calls, interval=180)
    _emit(cluster, 1, [{"name": "light", "sha": "b" * 12},
                       {"name": "hive", "sha": "c" * 12}])
    agent._consume_events()
    assert sorted(calls) == ["hive", "light"], calls


def test_event_throttle_expires(tmp_path, monkeypatch):
    """间隔到达后同一 repo 重新可同步，不是永久钉死。"""
    calls = []
    agent, cluster = _stub_agent(tmp_path, monkeypatch, calls, interval=1)
    _emit(cluster, 1, [{"name": "light", "sha": "b" * 12}])
    agent._consume_events()
    assert calls == ["light"]

    # 把游标时间推到 2 秒前，模拟间隔到期。
    agent._state["repos"]["light"]["synced_at"] = hc._iso(time.time() - 2)
    _emit(cluster, 2, [{"name": "light", "sha": "c" * 12}])
    agent._consume_events()
    assert calls == ["light", "light"], calls


def test_event_throttle_disabled_by_zero(tmp_path, monkeypatch):
    """interval=0 关闭节流，恢复旧行为（每条事件都同步）。"""
    calls = []
    agent, cluster = _stub_agent(tmp_path, monkeypatch, calls, interval=0)
    for i in range(3):
        _emit(cluster, i, [{"name": "light", "sha": "b" * 12}])
        agent._consume_events()
    assert len(calls) == 3, calls


def test_event_terminal_state_skipped(tmp_path, monkeypatch):
    """ahead/diverged 且 target 未变时，事件不再跑 git。"""
    calls = []
    agent, cluster = _stub_agent(tmp_path, monkeypatch, calls, interval=180)
    agent._state["repos"]["light"] = {
        "status": "ahead", "target": "b" * 12, "sha": "a" * 12,
        "synced_at": hc._iso(time.time() - 999),
    }
    _emit(cluster, 1, [{"name": "light", "sha": "b" * 12}])
    agent._consume_events()
    assert calls == [], calls

    # target 变了就要放行。
    _emit(cluster, 2, [{"name": "light", "sha": "d" * 12}])
    agent._consume_events()
    assert calls == ["light"], calls


def test_inflight_blocks_reentrant_sync(tmp_path, monkeypatch):
    """在飞标志阻止同一 repo 的重入调用。"""
    calls = []
    agent, _ = _stub_agent(tmp_path, monkeypatch, calls, interval=0)
    agent._inflight.add("light")
    assert agent._event_need_sync("light", "b" * 12) is False
    assert agent._event_need_sync("hive", "b" * 12) is True
    agent._inflight.discard("light")

    # _sync_repo 本身也要挡住在飞调用。
    agent._inflight.add("light")
    agent._sync_repo("light", "b" * 12)
    assert calls == [], calls


def test_first_sync_not_throttled(tmp_path, monkeypatch):
    """没有历史游标的仓（首次同步）不能被节流挡掉。"""
    calls = []
    agent, cluster = _stub_agent(tmp_path, monkeypatch, calls, interval=180)
    _emit(cluster, 1, [{"name": "light", "sha": "b" * 12}])
    agent._consume_events()
    assert calls == ["light"], calls


def test_status_exposes_throttle_config(tmp_path, monkeypatch):
    calls = []
    agent, _ = _stub_agent(tmp_path, monkeypatch, calls, interval=42)
    st = agent.status()
    assert st["event_min_interval_s"] == 42
    assert st["inflight"] == []
