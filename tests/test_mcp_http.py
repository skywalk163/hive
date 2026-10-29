"""P1：hived 的 MCP HTTP 入口（token、initialize、tools/list、tools/call）。"""
from __future__ import annotations

import json
import time
from urllib import error as urlerror
from urllib import request as urlrequest

import mcp_auth
from mcp_http import McpHttpService


def _post(url, token, payload):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urlrequest.Request(url, data=data, headers=headers, method="POST")
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read().decode())
    except urlerror.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read().decode() or "{}")


def _wait(fn, timeout=8.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(0.1)
    return last


def test_mcp_http_read_and_auth(world, tmp_path):
    world.add_agent("fb", mode="manual")
    world.add_task("T-0001", repo="hive")
    token_file = tmp_path / "tokens.json"
    token = mcp_auth.enroll("fb", "worker", path=token_file)

    svc = McpHttpService(
        world.hive1, port=0, state_dir=tmp_path / "state",
        token_path=token_file,
        write_dir=tmp_path / "write", read_dir=tmp_path / "read",
    )
    svc.start()
    try:
        url = f"http://127.0.0.1:{svc.port}/mcp"
        assert _wait(lambda: svc.cache.status().get("cards"), timeout=10)

        code, headers, body = _post(url, token, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26",
                       "capabilities": {}, "clientInfo": {"name": "t"}},
        })
        assert code == 200, body
        assert body["result"]["protocolVersion"] == "2025-03-26"
        assert headers.get("Mcp-Session-Id")

        code, _, body = _post(url, token, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        assert code == 200, body
        names = {t["name"] for t in body["result"]["tools"]}
        assert "hive_claim" in names and "hive_land" not in names

        code, _, body = _post(url, token, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "hive_whoami", "arguments": {}},
        })
        assert code == 200, body
        envelope = json.loads(body["result"]["content"][0]["text"])
        assert envelope["status"] == "success", envelope
        assert envelope["details"]["agent"] == "fb"

        code, _, body = _post(url, "bad-token", {
            "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {},
        })
        assert code == 401
    finally:
        svc.stop()


def test_agent_enroll_revoke_cli(world, tmp_path):
    import os

    token_file = tmp_path / "tokens-cli.json"
    env = {**os.environ, "HIVE_MCP_TOKENS": str(token_file), "HIVE_TESTING": "1"}
    p = world.hive("agent", "enroll", "--name", "fb", "--role", "worker", env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    token = ""
    for line in p.stdout.splitlines():
        if line.startswith("token: "):
            token = line.split(":", 1)[1].strip()
            break
    assert token
    rec = mcp_auth.resolve(token, path=token_file)
    assert rec and rec["agent"] == "fb" and rec["role"] == "worker"

    p = world.hive("agent", "revoke", "--name", "fb", env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert mcp_auth.resolve(token, path=token_file) is None


def test_stdio_bridge(world, tmp_path):
    import os
    import subprocess
    import sys
    from conftest import HIVE

    world.add_agent("fb", mode="manual")
    token_file = tmp_path / "tokens-bridge.json"
    token = mcp_auth.enroll("fb", "worker", path=token_file)
    svc = McpHttpService(
        world.hive1, port=0, state_dir=tmp_path / "state",
        token_path=token_file,
        write_dir=tmp_path / "write-b", read_dir=tmp_path / "read-b",
    )
    svc.start()
    try:
        msg = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "initialize",
                          "params": {"protocolVersion": "2025-03-26",
                                     "capabilities": {}, "clientInfo": {"name": "t"}}})
        p = subprocess.run(
            [sys.executable, str(HIVE), "--dir", str(world.hive1),
             "mcp", "connect", "--stdio", "--url", f"http://127.0.0.1:{svc.port}/mcp",
             "--token", token],
            input=msg + "\n", capture_output=True, text=True, timeout=30,
            env={**os.environ, "HIVE_TESTING": "1"})
        assert p.returncode == 0, p.stderr
        lines = [l for l in p.stdout.splitlines() if l.strip()]
        assert lines, p.stderr
        body = json.loads(lines[-1])
        assert body["result"]["protocolVersion"] == "2025-03-26"
    finally:
        svc.stop()


def test_mcp_http_claim_write_path(world, tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_STATE", str(tmp_path / "agent-state"))
    world.add_agent("fb", mode="manual", worktrees=str(tmp_path / "wt"))
    world.add_task("T-0001", verify=["true"])
    token_file = tmp_path / "tokens-claim.json"
    token = mcp_auth.enroll("fb", "worker", path=token_file)
    svc = McpHttpService(
        world.hive1, port=0, state_dir=tmp_path / "state",
        token_path=token_file,
        write_dir=tmp_path / "write-c", read_dir=tmp_path / "read-c",
    )
    svc.start()
    try:
        url = f"http://127.0.0.1:{svc.port}/mcp"
        code, _, body = _post(url, token, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hive_claim",
                       "arguments": {"task_id": "T-0001"}},
        })
        assert code == 200, body
        envelope = json.loads(body["result"]["content"][0]["text"])
        assert envelope["status"] == "success", envelope
        assert envelope["details"]["task_id"] == "T-0001"
        assert (tmp_path / "wt" / "T-0001").exists()
    finally:
        svc.stop()
