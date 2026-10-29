#!/usr/bin/env python3
"""P1：hived 的本机 MCP HTTP 入口（127.0.0.1:8790）。

一个进程服务本机多个 agent：每个 agent 一个 token；读工具走 ReadCache，
写工具复用现有 HiveMcp/Worker（claim 仍以 Git 为真相）。

只依赖标准库；Streamable HTTP 用 POST + JSON（可选 SSE）实现。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import mcp_auth
from hive import HiveError
from mcp_cache import ReadCache
from mcp_server import HiveMcp, McpServer, ensure_clone

HTTP_PROTOCOL_VERSION = "2025-03-26"
WRITE_TOOLS = {
    "hive_claim", "hive_submit", "hive_block", "hive_unblock", "hive_abort",
    "hive_extend_lease", "hive_progress", "hive_land", "hive_close",
    "hive_retry", "hive_assign", "hive_dispatch", "hive_gc", "hive_watchdog",
}


class McpHttpService:
    def __init__(self, hive_dir: str | Path, *, port: int = 8790,
                 bind: str = "127.0.0.1", state_dir: str | Path | None = None,
                 token_path: str | Path | None = None,
                 write_dir: str | Path | None = None,
                 read_dir: str | Path | None = None,
                 protocol_version: str = HTTP_PROTOCOL_VERSION):
        self.hive_dir = Path(hive_dir).expanduser().resolve()
        self.port = int(port)
        self.bind = bind
        self.state_dir = Path(state_dir).expanduser() if state_dir else (
            Path.home() / ".hive" / "cluster")
        self.token_path = Path(token_path).expanduser() if token_path else (
            self.state_dir / "mcp_tokens.json")
        self.write_dir = Path(write_dir).expanduser() if write_dir else (
            Path.home() / ".hive" / "clones" / "mcp-hived")
        self.read_dir = Path(read_dir).expanduser() if read_dir else (
            Path.home() / ".hive" / "clones" / "mcp-hived-read")
        self.protocol_version = protocol_version
        self.cache = ReadCache(self.read_dir)
        self._impls: dict[tuple[str, str], HiveMcp] = {}
        self._impls_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._stop = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.last_error = ""

    # -- 生命周期 ---------------------------------------------------------- #
    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.bind in ("0.0.0.0", "", "::") else self.bind
        return f"http://{host}:{self.port}/mcp"

    def start(self) -> None:
        for src, dst, label in ((self.hive_dir, self.write_dir, "write"),
                                (self.hive_dir, self.read_dir, "read")):
            try:
                ensure_clone(src, dst)
            except Exception as exc:  # noqa: BLE001  MCP 是可选项，不拖垮集群
                self.last_error = f"{label} clone: {exc}"
                self._log(f"mcp: {label} clone 失败（继续启动）: {exc}")
        try:
            self.cache.start()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"cache: {exc}"
            self._log(f"mcp: 缓存启动失败（继续）: {exc}")

        handler, server_cls = _build_handler(self)
        try:
            self._server = server_cls((self.bind, self.port), handler)
        except OSError as exc:
            self.last_error = f"listen: {exc}"
            self._log(f"mcp: 监听 {self.bind}:{self.port} 失败（继续）: {exc}")
            return
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="hive-mcp-http", daemon=True)
        self._thread.start()
        self._log(f"mcp: 已启动 {self.url}（token {self.token_path}）")

    def stop(self) -> None:
        self._stop.set()
        self.cache.stop()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        self._log("mcp: 已停止")

    def status(self) -> dict:
        return {
            "enabled": self._server is not None,
            "url": self.url,
            "port": self.port,
            "write_dir": str(self.write_dir),
            "read_dir": str(self.read_dir),
            "cache": self.cache.status(),
            "last_error": self.last_error,
        }

    # -- 请求处理 ---------------------------------------------------------- #
    def _impl(self, agent: str, role: str) -> HiveMcp:
        key = (agent, role)
        with self._impls_lock:
            impl = self._impls.get(key)
            if impl is None:
                impl = HiveMcp(self.write_dir, agent, role, cache=self.cache)
                self._impls[key] = impl
            return impl

    def handle_rpc(self, msg, token: str):
        who = mcp_auth.resolve(token, self.token_path)
        if not who:
            raise PermissionError("token 无效或已吊销")
        impl = self._impl(who["agent"], who["role"])
        server = McpServer(impl, protocol_version=self.protocol_version)
        if isinstance(msg, list):
            out = []
            for item in msg:
                resp = self._handle_one(server, item)
                if resp is not None:
                    out.append(resp)
            return out
        return self._handle_one(server, msg)

    def _handle_one(self, server: McpServer, msg):
        if not isinstance(msg, dict):
            return server.error(None, -32600, "JSON-RPC 必须是对象")
        write = (msg.get("method") == "tools/call"
                 and isinstance(msg.get("params"), dict)
                 and msg["params"].get("name") in WRITE_TOOLS)
        if write:
            # 多个 agent 共用 write clone；写串行，读仍走缓存不阻塞。
            with self._write_lock:
                return server.handle(msg)
        return server.handle(msg)

    @staticmethod
    def _log(text: str) -> None:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


class _Handler(BaseHTTPRequestHandler):
    server_version = "hive-mcp/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        # 默认 access log 会刷屏；错误才打
        if getattr(self, "_last_code", 200) >= 400:
            sys.stderr.write("[hive-mcp] %s\n" % (fmt % args))

    @property
    def svc(self) -> McpHttpService:
        return self.server.service  # type: ignore[attr-defined]

    def _send(self, code: int, payload, *, ctype: str = "application/json",
              session: str = "", extra: dict | None = None):
        self._last_code = code
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if session:
            self.send_header("Mcp-Session-Id", session)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _auth_token(self) -> str:
        raw = (self.headers.get("Authorization") or "").strip()
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return (self.headers.get("X-Hive-Mcp-Token") or "").strip()

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 4 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            raise ValueError("空请求体")
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, "", extra={"Allow": "POST, GET, DELETE, OPTIONS"})

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send(200, {"ok": True, "service": "hive-mcp",
                             "status": self.svc.status()})
            return
        if path != "/mcp":
            self._send(404, {"ok": False, "error": "未知资源"})
            return
        if not mcp_auth.resolve(self._auth_token(), self.svc.token_path):
            self._send(401, {"ok": False, "error": "token 无效"})
            return
        # Streamable HTTP 的 GET 是服务器→客户端 SSE 通道；P1 只保持心跳。
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(b": hive-mcp ready\n\n")
            self.wfile.flush()
            while not self.svc._stop.wait(15):
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/mcp":
            self._send(404, {"ok": False, "error": "未知资源"})
            return
        if not mcp_auth.resolve(self._auth_token(), self.svc.token_path):
            self._send(401, {"ok": False, "error": "token 无效"})
            return
        self._send(200, {"ok": True})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/mcp":
            self._send(404, {"ok": False, "error": "未知资源"})
            return
        token = self._auth_token()
        if not mcp_auth.resolve(token, self.svc.token_path):
            self._send(401, {"ok": False, "error": "token 无效或已吊销"})
            return
        try:
            msg = self._read_json()
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": str(exc)}})
            return
        session = (self.headers.get("Mcp-Session-Id") or "").strip() or uuid.uuid4().hex
        is_init = (isinstance(msg, dict) and msg.get("method") == "initialize")
        try:
            resp = self.svc.handle_rpc(msg, token)
        except PermissionError as exc:
            self._send(401, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001  协议层不让客户端看到裸栈
            self._send(500, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32603,
                                       "message": f"{type(exc).__name__}: {exc}"}},
                       session=session if is_init else "")
            return
        if resp is None:
            self._send(202, "", session=session if is_init else "")
            return
        accept = (self.headers.get("Accept") or "").lower()
        if "text/event-stream" in accept and "application/json" not in accept:
            body = f"event: message\ndata: {json.dumps(resp, ensure_ascii=False)}\n\n"
            self._send(200, body, ctype="text/event-stream",
                       session=session if is_init else "")
        else:
            self._send(200, resp, session=session if is_init else "")


def _build_handler(service: McpHttpService):
    class Handler(_Handler):
        pass

    # ThreadingHTTPServer 把 service 挂在 server 上，Handler 通过 self.server.service 取
    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, addr, h):
            super().__init__(addr, h)
            self.service = service

    return Handler, Server


def make_server(service: McpHttpService):
    handler, server_cls = _build_handler(service)
    return server_cls((service.bind, service.port), handler)
