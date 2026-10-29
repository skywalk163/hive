#!/usr/bin/env python3
"""P1：stdio ↔ hived MCP HTTP 桥。

给只支持 stdio 的客户端/脚本用：一行一个 JSON-RPC，转发到
``http://127.0.0.1:8790/mcp``，把响应按一行一个 JSON 打回 stdout。
"""

from __future__ import annotations

import json
import sys
from urllib import error as urlerror
from urllib import request as urlrequest

DEFAULT_URL = "http://127.0.0.1:8790/mcp"


def _post(url: str, token: str, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urlrequest.Request(url, data=data, headers=headers, method="POST")
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as resp:
            raw = resp.read()
            return getattr(resp, "status", resp.getcode()), raw
    except urlerror.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return None, str(exc).encode("utf-8")


def serve_stdio(url: str = DEFAULT_URL, token: str = "") -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": str(exc)}},
                             ensure_ascii=True), flush=True)
            continue
        code, raw = _post(url, token, msg)
        if code == 200 and raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                data = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32603, "message": "响应不是 JSON"}}
            if isinstance(data, list):
                for item in data:
                    print(json.dumps(item, ensure_ascii=True), flush=True)
            else:
                print(json.dumps(data, ensure_ascii=True), flush=True)
        elif code is None:
            rid = msg.get("id") if isinstance(msg, dict) else None
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32603,
                                        "message": f"无法连接 hived MCP: {raw.decode('utf-8', 'replace')}"}},
                             ensure_ascii=True), flush=True)
        # 202/204（notification）不输出
    return 0


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="stdio → hived MCP HTTP 桥")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--token", default="")
    args = p.parse_args(argv)
    return serve_stdio(args.url, args.token)


if __name__ == "__main__":
    raise SystemExit(main())