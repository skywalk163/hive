#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蜂巢 Hive 看板的零依赖静态服务器。

用法：
    python3 serve.py --status sample-status.json --port 8787

只使用 Python 3 标准库：argparse / json / pathlib / http.server / sys / urllib.parse。
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent

# 静态文件白名单：路径 -> (文件名, MIME)。不在白名单里的一律 404，天然防目录穿越。
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/sample-status.json": ("sample-status.json", "application/json; charset=utf-8"),
    "/ci.json": ("ci.json", "application/json; charset=utf-8"),
}

API_PATH = "/api/status"
API_CONFIG = "/api/config"
CONFIG_PATH = BASE_DIR / "settings.json"
NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


def build_handler(status_path):
    """用闭包把状态文件路径注入请求处理器（避免全局变量）。"""

    class HiveHandler(BaseHTTPRequestHandler):
        server_version = "hive-dashboard/1.0"
        protocol_version = "HTTP/1.1"

        # ---- 底层发送工具 ----
        def _send(self, code, body, ctype, extra_headers=None):
            self._last_code = code
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", NO_STORE)
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _send_file(self, filename, ctype):
            path = BASE_DIR / filename
            try:
                body = path.read_bytes()
            except OSError:
                self._send_json(500, {"error": "静态文件读取失败: %s" % filename})
                return
            self._send(200, body, ctype)

        # ---- 路由 ----
        def do_GET(self):  # noqa: N802 （BaseHTTPRequestHandler 的命名约定）
            path = urlparse(self.path).path
            if path == API_PATH:
                self.serve_status()
                return
            if path == API_CONFIG:
                self.serve_config_get()
                return
            entry = STATIC_FILES.get(path)
            if entry is None:
                self._send_json(404, {"error": "未找到资源: %s" % path})
                return
            self._send_file(entry[0], entry[1])

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == API_CONFIG:
                self.serve_config_post()
                return
            self._send_json(404, {"error": "未找到资源: %s" % path})

        # ---- /api/config：系统只读信息 + 服务端共享看板默认（GET/POST） ----
        def system_info(self):
            agents_dir = BASE_DIR.parent / "agents"
            agents = sorted(p.stem for p in agents_dir.glob("*.json")) if agents_dir.is_dir() else []
            return {
                "status_file": status_path,
                "dashboard_dir": str(BASE_DIR),
                "hub": os.environ.get("HIVE_HUB", ""),
                "agents": agents,
            }

        def load_dashboard_config(self):
            try:
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except ValueError:
                return {}

        def serve_config_get(self):
            self._send_json(200, {
                "system": self.system_info(),
                "dashboard": self.load_dashboard_config(),
            })

        def serve_config_post(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError as exc:
                self._send_json(400, {"error": "请求体不是合法 JSON: %s" % exc})
                return
            incoming = body.get("dashboard") if isinstance(body, dict) else None
            if not isinstance(incoming, dict):
                self._send_json(400, {"error": "dashboard 必须是对象"})
                return
            cur = self.load_dashboard_config()
            cur.update(incoming)   # 合并后持久化到服务端共享默认
            try:
                CONFIG_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                self._send_json(500, {"error": "配置写入失败: %s" % exc})
                return
            self._send_json(200, {"system": self.system_info(), "dashboard": cur})

        # ---- /api/status：每次请求都重新读盘 ----
        def serve_status(self):
            try:
                raw = Path(status_path).read_text(encoding="utf-8")
            except FileNotFoundError:
                self._send_json(503, {"error": "状态文件不存在: %s" % status_path})
                return
            except OSError as exc:
                self._send_json(503, {"error": "状态文件读取失败: %s" % exc})
                return
            try:
                data = json.loads(raw)
            except ValueError as exc:
                self._send_json(500, {"error": "状态文件不是合法 JSON: %s" % exc})
                return
            if not isinstance(data, list):
                self._send_json(500, {"error": "状态文件根节点应为数组"})
                return
            self._send_json(200, data)

        # ---- 日志：静音常规访问，只保留 4xx/5xx ----
        def log_message(self, fmt, *args):
            if getattr(self, "_last_code", 200) >= 400:
                sys.stderr.write("[hive-dashboard] %s\n" % (fmt % args))

    return HiveHandler


def main(argv=None):
    parser = argparse.ArgumentParser(description="蜂巢 Hive 看板静态服务器（零依赖）")
    parser.add_argument("--status", default="sample-status.json",
                        help="hive status --json 导出的状态文件路径（默认 sample-status.json）")
    parser.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    args = parser.parse_args(argv)

    status_path = str(Path(args.status).expanduser())
    handler = build_handler(status_path)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print("http://%s:%d" % ("127.0.0.1" if args.host in ("0.0.0.0", "") else args.host, args.port))
    print("状态文件: %s" % status_path)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
