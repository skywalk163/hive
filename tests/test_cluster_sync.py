"""P0：repo.updated 事件、land 事件载荷、RepoSyncAgent 的安全快进。"""
from __future__ import annotations

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
