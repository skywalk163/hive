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
