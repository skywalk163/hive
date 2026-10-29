#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive 端口规划 —— 所有常驻服务的默认端口只在这里定义一次。

为什么单独一个模块
------------------
2026-10-28 曾出现「手动跑 8787、服务起 8791」的错位，根因是 serve.py / 脚本 /
hive.py 各写各的默认值。集群上线后又多了 HTTP 8788 和 UDP 8789，如果继续散落，
下一个人一定会把 8790 当看板端口、或把 8789 的 UDP 和某个 TCP 服务搞混。

现在的规则：

* **代码默认值** → `hive_ports.py`；
* **环境变量覆盖** → 每个服务一个名字（看板保留历史 `PORT`）；
* **运维文档** → `docs/端口与服务规划.md`；
* **查看/体检** → `hive ports [--check] [--json]`。

端口段：
  8787  TCP  看板 HTTP（只绑 127.0.0.1，默认不对外）
  8788  TCP  集群 HTTP（节点间注册/心跳/投票/进度）
  8789  UDP  集群广播发现（不是 TCP；只开 TCP 也能用静态 seed）
  8790  TCP  MCP HTTP（hived → agent；只绑 127.0.0.1）
  8791-8798  预留给 hive 内部服务，当前不分配
  8799  TCP  测试/冒烟专用（ci-smoke / 手工验证，不用于生产）

外部依赖（不归 hive 分配，但排障时会看到）：
  3000   Gitea（中心仓）
  3080   DSH Web
  18443  LightMem kernel
  18444  LightMem MCP
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# 单一来源
# --------------------------------------------------------------------------- #

DASHBOARD_PORT = 8787
DASHBOARD_ENV = "HIVE_DASHBOARD_PORT"
DASHBOARD_LEGACY_ENV = "PORT"

CLUSTER_HTTP_PORT = 8788
CLUSTER_HTTP_ENV = "HIVE_CLUSTER_PORT"

CLUSTER_DISCOVERY_PORT = 8789
CLUSTER_DISCOVERY_ENV = "HIVE_CLUSTER_DISCOVERY_PORT"

MCP_HTTP_PORT = 8790
MCP_HTTP_ENV = "HIVE_MCP_PORT"

SMOKE_PORT = 8799
SMOKE_ENV = "SMOKE_PORT"

RESERVED_FIRST = 8791
RESERVED_LAST = 8798
RESERVED_RANGE = (RESERVED_FIRST, RESERVED_LAST)


@dataclass(frozen=True)
class PortSpec:
    key: str
    service: str
    port: int
    protocol: str
    bind: str
    env: str
    note: str
    legacy_env: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "service": self.service,
            "port": self.port,
            "protocol": self.protocol,
            "bind": self.bind,
            "env": self.env,
            "legacy_env": self.legacy_env,
            "note": self.note,
        }


SPECS = (
    PortSpec("dashboard", "看板 HTTP", DASHBOARD_PORT, "tcp", "127.0.0.1",
             DASHBOARD_ENV, "浏览器/看板服务；默认只绑本机，远程用 SSH 隧道",
             DASHBOARD_LEGACY_ENV),
    PortSpec("cluster", "集群 HTTP", CLUSTER_HTTP_PORT, "tcp", "0.0.0.0",
             CLUSTER_HTTP_ENV, "注册/心跳/投票/进度/租约；跨机需放行"),
    PortSpec("discovery", "集群广播发现", CLUSTER_DISCOVERY_PORT, "udp", "0.0.0.0",
             CLUSTER_DISCOVERY_ENV, "UDP 广播；仅同网段自动发现用，可关闭"),
    PortSpec("mcp", "MCP HTTP", MCP_HTTP_PORT, "tcp", "127.0.0.1",
             MCP_HTTP_ENV, "hived 的本机 MCP 入口；只绑 loopback，不对外"),
    PortSpec("smoke", "测试/冒烟", SMOKE_PORT, "tcp", "127.0.0.1",
             SMOKE_ENV, "ci-smoke 与手工验证专用；不要拿它跑生产服务"),
)

_EXTERNAL = (
    {"port": 3000, "protocol": "tcp", "service": "Gitea", "owner": "外部"},
    {"port": 3080, "protocol": "tcp", "service": "DSH Web", "owner": "外部"},
    {"port": 18443, "protocol": "tcp", "service": "LightMem kernel", "owner": "外部"},
    {"port": 18444, "protocol": "tcp", "service": "LightMem MCP", "owner": "外部"},
)


def _parse_port(raw, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if 1 <= value <= 65535:
        return value
    return default


def effective_port(key: str) -> int:
    """取某个服务当前生效的端口（env > 默认）。"""
    spec = by_key(key)
    raw = os.environ.get(spec.env)
    if raw is None and spec.legacy_env:
        raw = os.environ.get(spec.legacy_env)
    return _parse_port(raw, spec.port) if raw is not None else spec.port


def by_key(key: str) -> PortSpec:
    for spec in SPECS:
        if spec.key == key:
            return spec
    raise KeyError(f"未知端口 key: {key}")


def port_plan() -> list[dict]:
    rows = []
    for spec in SPECS:
        row = spec.as_dict()
        row["effective"] = effective_port(spec.key)
        row["reserved"] = False
        rows.append(row)
    rows.append({
        "key": "reserved",
        "service": "hive 内部预留",
        "port": f"{RESERVED_FIRST}-{RESERVED_LAST}",
        "protocol": "-",
        "bind": "-",
        "env": "-",
        "legacy_env": "",
        "note": "预留给后续 hive 服务；分配前先改本模块和端口规划文档",
        "effective": None,
        "reserved": True,
    })
    return rows


def check_ports(host: str = "127.0.0.1") -> list[dict]:
    """尝试绑定每个端口，返回占用情况（只用于体检，不修改任何服务）。"""
    out = []
    for spec in SPECS:
        port = effective_port(spec.key)
        free = False
        error = ""
        sock = None
        try:
            family = socket.AF_INET
            if spec.protocol == "udp":
                sock = socket.socket(family, socket.SOCK_DGRAM)
            else:
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.2)
            sock.bind((host, port))
            free = True
        except OSError as exc:
            error = str(exc)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        out.append({"key": spec.key, "service": spec.service,
                    "port": port, "protocol": spec.protocol,
                    "bind": spec.bind, "free": free, "error": error})
    return out


def external_ports() -> list[dict]:
    return [dict(row) for row in _EXTERNAL]


def format_plan(rows: list[dict] | None = None) -> str:
    rows = rows or port_plan()
    lines = [
        "hive 端口规划（默认值只在 hive_ports.py 定义一次）",
        "",
        f"{'KEY':<11} {'PORT':<12} {'PROTO':<6} {'BIND':<10} {'ENV':<30} SERVICE",
    ]
    for row in rows:
        port = row.get("port")
        eff = row.get("effective")
        if eff is not None and eff != port:
            port = f"{eff} (默认 {port})"
        lines.append(
            f"{str(row.get('key', '')):<11} {str(port):<12} "
            f"{str(row.get('protocol', '')):<6} {str(row.get('bind', '')):<10} "
            f"{str(row.get('env', '')):<30} {row.get('service', '')}")
        if row.get("note"):
            lines.append(f"{'':<11} └ {row['note']}")
    lines += ["", "外部依赖（不归 hive 分配）："]
    for row in external_ports():
        lines.append(f"  {row['port']:<6} {row['protocol']:<4} {row['service']}")
    lines += ["", "查看占用：hive ports --check；完整说明：docs/端口与服务规划.md"]
    return "\n".join(lines)
