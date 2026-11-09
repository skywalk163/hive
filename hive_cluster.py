#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive 集群 —— 常驻、可自愈、主节点自动选举的在线协调层。

为什么需要它
------------
hive 的既有模型是「Git-as-a-Database」：claim / result / status 都靠 git 分支同步，
它给出了强原子性，但有两个天生的慢点：

* 一台机器上 agent 的进度，别的机器要等下一次 `git fetch` 才看得到；
* 总控/看板进程不常驻，靠人或者 cron 驱动，主机一停服务就断。

这一层把「谁在线、谁在干活、谁是主节点、任务进度到哪了」放到一个常驻的
HTTP/UDP 集群里。它**不替代 git**——claim 的原子性、result 的审计、land 的合流
仍然以 git 为唯一真相；集群只是把「在线状态」和「进度通知」做得像 OSPF 一样快。

设计要点
--------
1. 每个节点都跑一个 `hive cluster serve`，互相发现、注册、心跳。
2. 局域网内用 UDP 广播发现，静态 seed 作为兜底（跨网段/广播被禁时用）。
3. 任期（term）是 fencing token：每次选主 term+1；旧主节点的写入带旧 term，
   新主节点可以明确拒绝，避免「僵尸主节点」。
4. 化身号（incarnation）：节点每次启动严格递增；低化身号的记录不会被接受，
   重启后的节点能干净地覆盖关于自己的过期死亡传闻。
5. 主节点选举采用「当前可见活节点多数」；2 节点集群在主节点退出后，剩下的一台
   自己就是多数，因此能接管。这是可用性优先的取舍：网络分区时可能出现短暂双主，
   恢复后由 term 收敛，详见 docs/集群服务.md。
6. 租约（lease）是**建议性**的，持久化在集群状态里；新主节点上任后给在飞租约
   一个恢复宽限期（默认 120 秒），避免「刚换主就把活重派」的抖动。真正的 claim
   仍然由 git 分支裁决。

只用 Python 标准库。HTTP 监听默认 8788，UDP 发现默认 8789。
"""

from __future__ import annotations

import argparse
import copy
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# 常量与配置
# --------------------------------------------------------------------------- #

PROTOCOL = 1
CLUSTER_VERSION = "1.0"

# 端口默认值只在 hive_ports.py 定义一次；缺模块时退回内置值。
try:
    from hive_ports import (
        CLUSTER_HTTP_PORT as DEFAULT_PORT,
        CLUSTER_HTTP_ENV as CLUSTER_PORT_ENV,
        CLUSTER_DISCOVERY_PORT as DEFAULT_DISCOVERY_PORT,
        CLUSTER_DISCOVERY_ENV as CLUSTER_DISCOVERY_ENV,
        MCP_HTTP_PORT as DEFAULT_MCP_PORT,
        MCP_HTTP_ENV as MCP_PORT_ENV,
    )
except ImportError:  # pragma: no cover
    DEFAULT_PORT = 8788
    CLUSTER_PORT_ENV = "HIVE_CLUSTER_PORT"
    DEFAULT_DISCOVERY_PORT = 8789
    CLUSTER_DISCOVERY_ENV = "HIVE_CLUSTER_DISCOVERY_PORT"
    DEFAULT_MCP_PORT = 8790
    MCP_PORT_ENV = "HIVE_MCP_PORT"

# 子进程「无窗」（T-0206）：`_git_rev` / `_remote_head` / `_pid_alive` 都是
# 短命 console 子进程，父进程无控制台时它们会自建可见窗口（周期性闪窗）。
try:
    from hive_compat import no_window_kwargs
except ImportError:  # pragma: no cover
    def no_window_kwargs() -> dict:
        return {}

DEFAULT_CLUSTER_ID = "hive"
EVENT_LIMIT = 500
# 聊天（跨机留言）在内存里保留多少条；磁盘 chat.jsonl 只追加不删，
# 进程启动时读回最后 CHAT_LIMIT 条。留言是低频的人类/agent 文本，2000 条够回溯很久。
CHAT_LIMIT = 2000
CHAT_MAX_TEXT = 8000
# leader 多久从 Git 卡面补一次新卡到 store（秒）。启动时那一次摄入不够：
# 卡是随时新增的，副本必须能持续收敛，不能靠一次性快照。
STORE_RECONCILE_S = 60.0
# 落后节点当选后，最多花多久从同伴追平 store 再开放写（秒）。追平期间
# claim/submit 一律 503 E_RECONCILING，宁可短暂拒绝，也不让「落后主控」
# 拿旧 commit_seq 起一段新 oplog（T-0295 与当选后 reconcile 配套）。
STORE_RECONCILE_WIN_S = 20.0
CHAT_SCOPE_RE = re.compile(r"^(hall|task:T-\d{3,6}|agent:[A-Za-z0-9._-]{1,64})$")
MAX_BODY = 2 * 1024 * 1024
DEFAULT_STATE = Path.home() / ".hive" / "cluster"
DEFAULT_LEASE_GRACE = 120.0

# 这些默认值可被构造函数参数或环境变量覆盖；测试会用更短的超时。
DEFAULT_HEARTBEAT_S = 1.0
DEFAULT_PEER_TIMEOUT_S = 4.0
DEFAULT_DEAD_TIMEOUT_S = 15.0
DEFAULT_ELECTION_MIN_S = 3.0
DEFAULT_ELECTION_JITTER_S = 0.8
DEFAULT_LEADER_LEASE_S = 5.0
DEFAULT_DISCOVERY_INTERVAL_S = 5.0

# OSPF 的 DR/BDR 接口优先级是 0..255 的 8bit 无符号值：默认 1，越大越优先，
# 0 = 放弃选举资格（只能当 DRother）。取值域照搬，避免有人填 999 把排序搅乱。
MIN_LEADER_PRIORITY = 0
MAX_LEADER_PRIORITY = 255
DEFAULT_LEADER_PRIORITY = 1
# 名次之间至少差这么多秒。election_jitter 若被人为设成 0，所有节点会同一刻
# 发起选举，优先级就退化成「谁的网络快谁当」——给一个下限把这层语义保住。
MIN_ELECTION_STEP_S = 0.05


def normalize_leader_priority(value,
                              default: int = DEFAULT_LEADER_PRIORITY) -> int:
    """把任意来源的优先级规整到 OSPF 的 0..255。

    CLI 入口会先严格校验并报错；这里负责让「已落盘的 config.json / 环境变量 /
    别的节点广播过来的值」不至于破坏排序：解析不了回默认，越界 clamp 到边界。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_LEADER_PRIORITY, min(MAX_LEADER_PRIORITY, n))


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ClusterError(Exception):
    """集群层的可预期错误（命令行直接打印，不抛 traceback）。

    code 是 store 业务错误的机器可读分类，经 handle_store 透到 HTTP 响应里，
    调用方不必靠解析中文 error 文案区分「缺卡 / 被领走 / 集群不可用」。
    """

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _now() -> float:
    return time.time()


def _iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else _now(),
                                  timezone.utc).astimezone().isoformat(
                                      timespec="seconds")


def _epoch(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _log(msg: str) -> None:
    print(f"[hive-cluster {datetime.now():%H:%M:%S}] {msg}",
          file=sys.stderr, flush=True)


def _chat_id() -> str:
    """消息 id 必须全网唯一：节点 + 微秒 + 随机后缀，重复投递靠它去重。"""
    return "%s-%d-%s" % (socket.gethostname().split(".")[0], int(_now() * 1e6),
                         uuid.uuid4().hex[:6])


def _norm_scope(value) -> str:
    """频道：hall（大厅） / task:T-0231（某张卡的讨论） / agent:fb88-dsh（私信）。

    不合法的频道不报错，统一落到 hall —— 留言是给人用的，别让人因为
    写错一个冒号就发不出去。
    """
    text = str(value or "").strip()
    return text if CHAT_SCOPE_RE.match(text) else "hall"


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    """同目录临时文件 + os.replace，保证读者永远看不到半截 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _slug(value: str) -> str:
    return SAFE_NAME.sub("-", str(value or "node")).strip("-") or "node"


def _host_port(raw: str, default_port: int = DEFAULT_PORT):
    """把 URL / host:port / 裸 host 解析成 (host, port, scheme)。"""
    raw = str(raw or "").strip()
    if not raw:
        return "", default_port, "http"
    if raw.startswith("http://") or raw.startswith("https://"):
        u = urlparse(raw)
        return u.hostname or "", u.port or default_port, u.scheme
    if raw.startswith("["):
        host, _, rest = raw[1:].partition("]")
        try:
            port = int(rest.lstrip(":") or default_port)
        except ValueError:
            port = default_port
        return host, port, "http"
    host, sep, port_s = raw.rpartition(":")
    if sep and port_s.isdigit():
        return host, int(port_s), "http"
    return raw, default_port, "http"


def _base_url(host: str, port: int, scheme: str = "http") -> str:
    host = str(host or "127.0.0.1")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}"


def normalize_url(raw: str, default_port: int = DEFAULT_PORT) -> str:
    host, port, scheme = _host_port(raw, default_port)
    if not host:
        return ""
    return _base_url(host, port, scheme)


def _guess_lan_ip() -> str:
    """猜本机局域网地址。UDP connect 不会发包，只是让内核选出口地址。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def _clamp_progress(value):
    if value is None or value == "":
        return None
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None


def _tail_text(text: str, lines: int = 12) -> str:
    parts = [p for p in str(text or "").splitlines() if p.strip()]
    return "\n".join(parts[-lines:])


# --------------------------------------------------------------------------- #
# 配置 / 身份 / 持久状态
# --------------------------------------------------------------------------- #

class Config:
    """运行配置。优先级：显式参数 > 环境变量 > config.json > 默认值。"""

    def __init__(self, **kw):
        self.state_dir = Path(
            kw.get("state_dir") or os.environ.get("HIVE_CLUSTER_STATE")
            or DEFAULT_STATE).expanduser()
        saved = _read_json(self.state_dir / "config.json", {}) or {}
        if not isinstance(saved, dict):
            saved = {}

        self.cluster_id = str(
            kw.get("cluster_id") or os.environ.get("HIVE_CLUSTER_ID")
            or saved.get("cluster_id") or DEFAULT_CLUSTER_ID)
        self.name = str(
            kw.get("name") or os.environ.get("HIVE_CLUSTER_NAME")
            or saved.get("name") or socket.gethostname().split(".")[0] or "node")
        self.bind_host = str(
            kw.get("host") or os.environ.get("HIVE_CLUSTER_BIND")
            or saved.get("host") or "0.0.0.0")
        # 0 是合法值（测试用临时端口），所以不能用 `or` 链把 0 当成「没给」。
        port = kw.get("port")
        if port is None:
            port = os.environ.get("HIVE_CLUSTER_PORT")
        if port is None:
            port = saved.get("port")
        self.port = int(DEFAULT_PORT if port is None else port)
        self.advertise = str(
            kw.get("advertise") or os.environ.get("HIVE_CLUSTER_ADVERTISE")
            or saved.get("advertise") or "")
        self.discovery_port = int(
            kw.get("discovery_port") or os.environ.get(CLUSTER_DISCOVERY_ENV)
            or saved.get("discovery_port") or DEFAULT_DISCOVERY_PORT)
        if kw.get("discover") is None:
            self.discover = _env_bool("HIVE_CLUSTER_DISCOVER", bool(saved.get("discover", True)))
        else:
            self.discover = bool(kw.get("discover"))
        self.token = str(
            kw.get("token") or os.environ.get("HIVE_CLUSTER_TOKEN")
            or saved.get("token") or "")
        self.lease_grace = float(
            kw.get("lease_grace") or os.environ.get("HIVE_CLUSTER_LEASE_GRACE")
            or saved.get("lease_grace") or DEFAULT_LEASE_GRACE)
        # 只有 coordinator/electable 节点参选；verify-only 机器设 0 做 observer。
        if kw.get("electable") is None:
            self.electable = _env_bool("HIVE_CLUSTER_ELECTABLE",
                                       bool(saved.get("electable", True)))
        else:
            self.electable = bool(kw.get("electable"))
        # 首选主控：集群里「本来应该由它当主控」的那台（写名字或 node_id 都行）。
        # 主控挂掉重新选主时，这个节点最先发起选举，把主控权拿回来。
        # 空字符串 = 不设偏好，选主行为和以前完全一致（向后兼容）。
        self.preferred_leader = str(
            kw.get("preferred_leader")
            or os.environ.get("HIVE_CLUSTER_PREFERRED_LEADER")
            or saved.get("preferred_leader") or "").strip()
        # OSPF 式主控优先级：**值大者优先**当主控，默认 1（等于谁都一样）。
        # 和 preferred_leader 的分工：preferred 是「人硬指定某台」，priority 是
        # 「每台自己声明我该排第几」—— 后者写在自述里随心跳广播，全局可见，
        # 不用像 preferred 那样在每台机器上各配一遍「谁是首选」。
        #
        # 0 沿用 OSPF 的语义（DR priority 0 = 不参选）：这台可以当从属、可以被
        # 投票，但自己永不主动发起选举。留这个口子是因为「只读副本 / 弱机器」
        # 不该有机会当主控，而 electable=false 又太绝对（连被选举都不行）。
        raw_prio = kw.get("leader_priority")
        if raw_prio is None:
            raw_prio = os.environ.get("HIVE_CLUSTER_PRIORITY")
        if raw_prio is None:
            raw_prio = saved.get("leader_priority")
        self.leader_priority = normalize_leader_priority(
            DEFAULT_LEADER_PRIORITY if raw_prio is None else raw_prio)
        # 抢主控时是否允许比现任主控落后（见 promoted 相关逻辑）：
        # store 只是缓存、git 才是事实账，所以允许，但要留下审计痕迹。
        self.allow_forced_promote = _env_bool(
            "HIVE_CLUSTER_ALLOW_FORCE_PROMOTE",
            bool(saved.get("allow_forced_promote", True)))
        # 投票闸门的「严格模式」：store 落后的候选人一律拒票。
        # 默认**关闭** —— store 是可重建缓存、git 才是事实账（见 StoreReconciler），
        # 拿缓存序号当选举资格，在分区时必然全员互拒票：各节点 commit_seq 本来
        # 就不一致，于是只能等同伴被 dead_timeout 判死、quorum 退化成 1 才选出
        # 「孤身主控」，把无主窗口拉长还放大风暴（T-0295）。默认改成「投赞成票
        # + 留痕 vote_store_lag + 当选后先 reconcile 再服务写」；确实要「宁可没有
        # 主控也不接受落后节点」的运维，用本开关把老行为打开。
        if kw.get("strict_store_freshness") is None:
            self.strict_store_freshness = _env_bool(
                "HIVE_CLUSTER_STRICT_STORE_FRESHNESS",
                bool(saved.get("strict_store_freshness", False)))
        else:
            self.strict_store_freshness = bool(kw.get("strict_store_freshness"))
        if kw.get("sync_enabled") is None:
            self.sync_enabled = _env_bool("HIVE_CLUSTER_SYNC",
                                          bool(saved.get("sync", True)))
        else:
            self.sync_enabled = bool(kw.get("sync_enabled"))
        if kw.get("mcp_enabled") is None:
            self.mcp_enabled = _env_bool("HIVE_MCP_HTTP",
                                         bool(saved.get("mcp", False)))
        else:
            self.mcp_enabled = bool(kw.get("mcp_enabled"))
        mcp_port = kw.get("mcp_port")
        if mcp_port is None:
            mcp_port = os.environ.get(MCP_PORT_ENV)
        if mcp_port is None:
            mcp_port = saved.get("mcp_port")
        self.mcp_port = int(mcp_port if mcp_port is not None else DEFAULT_MCP_PORT)
        self.mcp_bind = str(
            kw.get("mcp_bind") or os.environ.get("HIVE_MCP_BIND")
            or saved.get("mcp_bind") or "127.0.0.1")
        if kw.get("store_enabled") is None:
            self.store_enabled = _env_bool("HIVE_STORE",
                                           bool(saved.get("store_enabled", False)))
        else:
            self.store_enabled = bool(kw.get("store_enabled"))
        if kw.get("coordinator_enabled") is None:
            self.coordinator_enabled = _env_bool(
                "HIVE_COORDINATOR", bool(saved.get("coordinator", False)))
        else:
            self.coordinator_enabled = bool(kw.get("coordinator_enabled"))
        self.claim_backend = str(
            kw.get("claim_backend") or os.environ.get("HIVE_CLAIM_BACKEND")
            or saved.get("claim_backend") or "git").strip().lower()
        if self.claim_backend not in ("git", "store"):
            self.claim_backend = "git"
        self.store_path = Path(
            kw.get("store_path") or os.environ.get("HIVE_STORE_PATH")
            or saved.get("store_path")
            or (self.state_dir / "hive.db")
        ).expanduser()
        self.mcp_write_dir = Path(
            kw.get("mcp_write_dir") or os.environ.get("HIVE_MCP_WRITE_DIR")
            or saved.get("mcp_write_dir")
            or (Path.home() / ".hive" / "clones" / "mcp-hived")
        ).expanduser()
        self.mcp_read_dir = Path(
            kw.get("mcp_read_dir") or os.environ.get("HIVE_MCP_READ_DIR")
            or saved.get("mcp_read_dir")
            or (Path.home() / ".hive" / "clones" / "mcp-hived-read")
        ).expanduser()

        seeds = kw.get("seeds")
        if seeds is None:
            env_seeds = os.environ.get("HIVE_CLUSTER_SEEDS", "")
            seeds = ([s for s in re.split(r"[,\s]+", env_seeds) if s] if env_seeds
                     else saved.get("seeds") or [])
        # 显式 seed / 环境变量是「追加」，不要把 config.json 里已经记下的旧 seed
        # 冲掉；用户想删 seed 就直接编辑 config.json。
        merged_seeds = list(saved.get("seeds") or []) + list(seeds or [])
        self.seeds = [normalize_url(s) for s in dict.fromkeys(
            str(x).strip() for x in merged_seeds) if s]

        self.heartbeat_interval = float(
            kw.get("heartbeat_interval") or _env_float("HIVE_CLUSTER_HEARTBEAT", DEFAULT_HEARTBEAT_S))
        self.peer_timeout = float(
            kw.get("peer_timeout") or _env_float("HIVE_CLUSTER_PEER_TIMEOUT", DEFAULT_PEER_TIMEOUT_S))
        self.dead_timeout = float(
            kw.get("dead_timeout") or _env_float("HIVE_CLUSTER_DEAD_TIMEOUT", DEFAULT_DEAD_TIMEOUT_S))
        self.election_min = float(
            kw.get("election_min") or _env_float("HIVE_CLUSTER_ELECTION_MIN", DEFAULT_ELECTION_MIN_S))
        self.election_jitter = float(
            kw.get("election_jitter") or _env_float("HIVE_CLUSTER_ELECTION_JITTER", DEFAULT_ELECTION_JITTER_S))
        self.leader_lease = float(
            kw.get("leader_lease") or _env_float("HIVE_CLUSTER_LEADER_LEASE", DEFAULT_LEADER_LEASE_S))
        self.discovery_interval = float(
            kw.get("discovery_interval") or _env_float("HIVE_CLUSTER_DISCOVERY_INTERVAL",
                                                       DEFAULT_DISCOVERY_INTERVAL_S))
        self.hive_dir = Path(
            kw.get("hive_dir") or os.environ.get("HIVE_DIR") or Path.cwd()
        ).expanduser().resolve()

    @property
    def pid_path(self) -> Path:
        return self.state_dir / "cluster.pid"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "cluster.log"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    def save(self) -> None:
        """持久化非敏感的启动参数与 seed；token 只在用户显式写 config.json 时保留。"""
        saved = _read_json(self.state_dir / "config.json", {}) or {}
        if not isinstance(saved, dict):
            saved = {}
        saved.update({
            "cluster_id": self.cluster_id,
            "name": self.name,
            "port": self.port,
            "discovery_port": self.discovery_port,
            "discover": self.discover,
            "host": self.bind_host,
            "advertise": self.advertise,
            "seeds": list(dict.fromkeys(self.seeds)),
            "lease_grace": self.lease_grace,
            "electable": bool(self.electable),
            "leader_priority": int(self.leader_priority),
            "preferred_leader": self.preferred_leader,
            "allow_forced_promote": bool(self.allow_forced_promote),
            "strict_store_freshness": bool(self.strict_store_freshness),
            "sync": bool(self.sync_enabled),
            "mcp": bool(self.mcp_enabled),
            "mcp_port": self.mcp_port,
            "mcp_bind": self.mcp_bind,
            "store_enabled": bool(self.store_enabled),
            "coordinator": bool(self.coordinator_enabled),
            "claim_backend": self.claim_backend,
            "store_path": str(self.store_path),
            "mcp_write_dir": str(self.mcp_write_dir),
            "mcp_read_dir": str(self.mcp_read_dir),
        })
        if self.token:
            # 0600 权限；本机看板/CLI 需要用它访问 /api/cluster/state。
            saved["token"] = self.token
        _write_json(self.state_dir / "config.json", saved)


class Identity:
    """节点身份：node_id 稳定，incarnation 每次启动严格递增。"""

    def __init__(self, state_dir: Path, name: str):
        self.path = Path(state_dir) / "node.json"
        prev = _read_json(self.path, {}) or {}
        if not isinstance(prev, dict):
            prev = {}
        same = prev.get("name") == name and prev.get("id")
        if same:
            self.node_id = str(prev["id"])
        else:
            self.node_id = f"{_slug(name)}-{uuid.uuid4().hex[:8]}"
        now_ms = int(_now() * 1000)
        if same:
            self.incarnation = max(int(prev.get("incarnation") or 0) + 1, now_ms)
        else:
            self.incarnation = max(int(prev.get("incarnation") or 0) + 1, now_ms)
        self.created = prev.get("created") if same else _iso()
        _write_json(self.path, {
            "id": self.node_id,
            "name": name,
            "incarnation": self.incarnation,
            "created": self.created,
            "last_started": _iso(),
        })


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #

# 跳过环境里的 HTTP(S)_PROXY：集群节点在同一局域网，走代理只会超时。
_OPENER = urlrequest.build_opener(urlrequest.ProxyHandler({}))


def http_json(url: str, method: str = "GET", payload=None,
              timeout: float = 1.5, token: str = ""):
    """返回 (status_code, dict|None)。网络错误返回 (None, None)，绝不抛。"""
    data = None
    headers = {"Accept": "application/json", "User-Agent": f"hive-cluster/{CLUSTER_VERSION}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["X-Hive-Cluster-Token"] = token
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=max(0.1, float(timeout))) as resp:
            raw = resp.read(MAX_BODY)
            code = getattr(resp, "status", resp.getcode())
    except urlerror.HTTPError as exc:
        try:
            raw = exc.read(MAX_BODY)
        except Exception:  # noqa: BLE001
            raw = b""
        code = exc.code
    except Exception:  # noqa: BLE001  URLError / timeout / reset 都当节点不可达
        return None, None
    if not raw:
        return code, None
    try:
        return code, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return code, None


# --------------------------------------------------------------------------- #
# UDP 发现
# --------------------------------------------------------------------------- #

class Discovery:
    """局域网广播发现：新节点发 discover，已有节点回 announce。"""

    def __init__(self, cluster: "Cluster"):
        self.cluster = cluster
        self.port = cluster.config.discovery_port
        self.sock = None
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> bool:
        if not self.cluster.config.discover:
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", self.port))
            sock.settimeout(0.5)
            self.sock = sock
        except OSError as exc:
            _log(f"UDP 发现端口 {self.port} 不可用（{exc}），改用静态 seed")
            return False
        self._thread = threading.Thread(target=self._loop, name="hive-cluster-udp",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if (msg.get("hive") != "discover"
                    or msg.get("cluster") != self.cluster.config.cluster_id
                    or msg.get("node") == self.cluster.node_id):
                continue
            try:
                reply = json.dumps(self.cluster.discovery_reply(),
                                   ensure_ascii=False).encode("utf-8")
                self.sock.sendto(reply, addr)
            except OSError:
                continue

    def discover(self, timeout: float = 0.8):
        """广播一轮并收集 announce；超时返回已收到的列表。"""
        if not self.cluster.config.discover:
            return []
        msg = {"hive": "discover", "proto": PROTOCOL,
               "cluster": self.cluster.config.cluster_id,
               "node": self.cluster.node_id}
        raw = json.dumps(msg).encode("utf-8")
        out = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", 0))
            sock.settimeout(0.15)
            targets = [("255.255.255.255", self.port), ("127.0.0.1", self.port)]
            for target in targets:
                try:
                    sock.sendto(raw, target)
                except OSError:
                    pass
            deadline = _now() + max(0.05, float(timeout))
            while _now() < deadline:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    m = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if (m.get("hive") == "announce"
                        and m.get("cluster") == self.cluster.config.cluster_id
                        and (m.get("node") or {}).get("id") != self.cluster.node_id):
                    m["_addr"] = addr[0]
                    out.append(m)
        finally:
            sock.close()
        return out


# --------------------------------------------------------------------------- #
# 集群本体
# --------------------------------------------------------------------------- #

class RepoSyncAgent:
    """P0 轻量代码同步器：消费 repo.updated 事件 + 5 分钟游标补偿。

    不参与一致性；只做安全快进（复用 hive.Hive._sync_one_repo 的 ff-only 边界）。
    没有 git 仓/没有 hive 模块时自动降级为 no-op，不影响集群本身。
    """

    def __init__(self, cluster):
        self.cluster = cluster
        self.config = cluster.config
        self.state_path = self.config.state_dir / "sync_cursor.json"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._hive = None
        self._hive_failed = False
        self._last_error = ""
        self._last_run = 0.0
        saved = _read_json(self.state_path, {}) or {}
        self._state = saved if isinstance(saved, dict) else {}
        self._state.setdefault("repos", {})
        self._last_event_id = str(self._state.get("last_event_id") or "")
        self._last_reconcile = 0.0

    # -- 生命周期 ---------------------------------------------------------- #
    def start(self) -> None:
        if not self.config.sync_enabled:
            _log("sync: 已关闭（HIVE_CLUSTER_SYNC=0）")
            return
        if not (self.config.hive_dir / ".git").exists():
            _log(f"sync: {self.config.hive_dir} 不是 git 仓，跳过")
            return
        self._thread = threading.Thread(target=self._loop, name="hive-repo-sync",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": bool(self.config.sync_enabled),
                "running": bool(self._thread and self._thread.is_alive()),
                "last_event_id": self._last_event_id,
                "last_run": _iso(self._last_run) if self._last_run else "",
                "last_error": self._last_error,
                "repos": copy.deepcopy(self._state.get("repos") or {}),
            }

    # -- 主循环 ------------------------------------------------------------ #
    def _loop(self) -> None:
        # 启动先补偿一轮，避免“关机期间漏掉的事件”等 5 分钟。
        self._safe_tick(reconcile=True)
        while not self._stop.wait(15):
            self._safe_tick(reconcile=(_now() - self._last_reconcile) >= 300)

    def _safe_tick(self, reconcile: bool = False) -> None:
        try:
            self._consume_events()
            if reconcile:
                self._reconcile()
            self._last_run = _now()
        except Exception as exc:  # noqa: BLE001  同步失败不能拖垮集群
            self._last_error = f"{type(exc).__name__}: {exc}"
            _log(f"sync: 本轮失败（忽略）: {self._last_error}")

    # -- 事件 -------------------------------------------------------------- #
    def _consume_events(self) -> None:
        snap = self.cluster.snapshot()
        events = [e for e in (snap.get("events") or [])
                  if e.get("type") == "repo.updated"]
        if not events:
            return
        # 找到上次消费的位置；找不到（事件被截断/leader 重启）就从头扫一遍，
        # 靠 sha 对比保证幂等。
        start = 0
        if self._last_event_id:
            for idx, ev in enumerate(events):
                if str(ev.get("id")) == self._last_event_id:
                    start = idx + 1
                    break
        for ev in events[start:]:
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            for item in data.get("repos") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if name:
                    self._sync_repo(name, str(item.get("sha") or ""))
            self._last_event_id = str(ev.get("id") or self._last_event_id)
            self._save()

    # -- 补偿 -------------------------------------------------------------- #
    def _reconcile(self) -> None:
        self._last_reconcile = _now()
        for name, path in self._repos().items():
            target = self._remote_head(path)
            if not target:
                continue
            cur = (self._state.get("repos") or {}).get(name) or {}
            if cur.get("sha") == target:
                if cur.get("status") != "up-to-date":
                    cur["status"] = "up-to-date"
                    cur["updated_at"] = _iso()
                    cur["error"] = ""
                    self._state.setdefault("repos", {})[name] = cur
                    self._save()
                continue
            self._sync_repo(name, target)

    def _repos(self) -> dict:
        h = self._hive_obj()
        if h is None:
            return {}
        try:
            return {str(k): Path(v) for k, v in h.repos().items()}
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"repos: {exc}"
            return {}

    def _hive_obj(self):
        if self._hive is not None:
            return self._hive
        if self._hive_failed:
            return None
        try:
            import hive as hive_mod
            self._hive = hive_mod.Hive(self.config.hive_dir)
        except Exception as exc:  # noqa: BLE001
            self._hive_failed = True
            self._last_error = f"hive import: {exc}"
            _log(f"sync: 无法加载 hive.Hive（同步停用）: {exc}")
        return self._hive

    # -- 单仓 -------------------------------------------------------------- #
    def _sync_repo(self, name: str, target: str = "") -> None:
        with self._lock:
            repos = self._repos()
            path = repos.get(name)
            if not path:
                self._record(name, "", "missing", target, "本机没有登记这个仓")
                return
            h = self._hive_obj()
            if h is None:
                return
            try:
                res = h._sync_one_repo(name, path)
            except Exception as exc:  # noqa: BLE001
                self._record(name, str(path), "error", target, str(exc))
                return
            status = str(res.get("status") or "unknown")
            actual = self._git_rev(path, "HEAD")
            rec = {
                "path": str(path),
                "status": status,
                "target": target,
                "sha": actual,
                "updated_at": _iso(),
                "detail": res.get("reason") or res.get("dirty") or "",
            }
            if status in ("up-to-date", "fast-forwarded"):
                rec["synced_at"] = _iso()
            self._record(name, str(path), status, target, "", rec)
            _log(f"sync: {name} {status} {str(actual)[:12]}"
                 + (f" target={str(target)[:12]}" if target else ""))

    def _record(self, name: str, path: str, status: str, target: str,
                error: str = "", rec: dict | None = None) -> None:
        with self._lock:
            item = rec or {
                "path": path, "status": status, "target": target,
                "updated_at": _iso(), "detail": error,
            }
            item["error"] = error
            self._state.setdefault("repos", {})[name] = item
            self._save()

    def _save(self) -> None:
        self._state["last_event_id"] = self._last_event_id
        self._state["updated_at"] = _iso()
        try:
            _write_json(self.state_path, self._state)
        except OSError as exc:  # noqa: BLE001
            self._last_error = f"save: {exc}"

    # -- git 小工具 -------------------------------------------------------- #
    @staticmethod
    def _git_rev(repo: Path, ref: str) -> str:
        try:
            p = subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                               capture_output=True, text=True, timeout=5,
                               **no_window_kwargs())
            return p.stdout.strip() if p.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    @staticmethod
    def _remote_head(repo: Path) -> str:
        try:
            p = subprocess.run(
                ["git", "-C", str(repo), "ls-remote", "origin",
                 "refs/heads/main", "refs/heads/master"],
                capture_output=True, text=True, timeout=15,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                **no_window_kwargs())
            if p.returncode != 0:
                return ""
            for line in p.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] in ("refs/heads/main",
                                                    "refs/heads/master"):
                    return parts[0]
        except (OSError, subprocess.SubprocessError):
            return ""
        return ""


class ArchiveAgent:
    """P2b：把 store 里 accepted 的结果补归档到 Git res/<tid>。

    正常 submit 已经写了 Git，这里多数时候只是 mark_archived；只有“store 有结果、
    Git 没有”（例如 worker 在 push 前挂了）才真正补写。只在 leader 跑，避免重复。
    """

    def __init__(self, cluster):
        self.cluster = cluster
        self.config = cluster.config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run = 0.0
        self.last_error = ""

    def start(self) -> None:
        if self.cluster.store is None:
            return
        self._thread = threading.Thread(target=self._loop, name="hive-archive",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict:
        store = self.cluster.store
        return {"running": bool(self._thread and self._thread.is_alive()),
                "last_run": _iso(self.last_run) if self.last_run else "",
                "last_error": self.last_error,
                "pending": len(store.pending_results()) if store else 0}

    def _loop(self) -> None:
        while not self._stop.wait(10):
            if self.cluster.role != "leader" or self.cluster.store is None:
                continue
            try:
                self.tick()
                if self.cluster.store is not None:
                    self.cluster.store.compact(keep=1000)
                self.last_run = _now()
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                _log(f"archive: 本轮失败（忽略）: {self.last_error}")

    def tick(self) -> int:
        from hive import Hive, commit_file_to_ref
        store = self.cluster.store
        if store is None:
            return 0
        h = Hive(self.config.hive_dir)
        n = 0
        for res in store.pending_results():
            tid = str(res.get("task_id") or "")
            if not tid:
                continue
            git_res = h.result_info(tid)
            sha = ""
            if isinstance(git_res, dict):
                sha = str(git_res.get("head") or "")
            else:
                payload = res.get("payload")
                if not isinstance(payload, dict) or not payload:
                    continue
                try:
                    commit_file_to_ref(
                        h.dir, f"results/{tid}.json",
                        json.dumps(payload, ensure_ascii=False, indent=2),
                        f"res/{tid}", "origin/main",
                        f"result {tid}: archived from store", force=True)
                    sha = str(payload.get("head") or "")
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{tid}: {exc}"
                    continue
            code, resp = self.cluster.handle_store(
                "archive", {"task": tid, "sha": sha, "term": self.cluster.term})
            if code == 200 and isinstance(resp, dict) and resp.get("ok"):
                n += 1
            else:
                self.last_error = f"{tid}: archive store {code} {resp}"
        return n


class CoordinatorAgent:
    """P2b：leader 在 hived 内跑单轮 orchestrate；默认关闭，避免和外部 orchestrate 重复。"""

    def __init__(self, cluster, interval: float = 30.0):
        self.cluster = cluster
        self.config = cluster.config
        self.interval = max(5.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run = 0.0
        self.last_error = ""

    def start(self) -> None:
        if not self.config.coordinator_enabled:
            return
        self._thread = threading.Thread(target=self._loop, name="hive-coordinator",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def status(self) -> dict:
        return {"enabled": bool(self.config.coordinator_enabled),
                "running": bool(self._thread and self._thread.is_alive()),
                "last_run": _iso(self.last_run) if self.last_run else "",
                "last_error": self.last_error}

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if self.cluster.role != "leader":
                continue
            try:
                from hive import Hive
                h = Hive(self.config.hive_dir)
                h.cmd_orchestrate(once=True, interval=int(self.interval))
                self.last_run = _now()
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                _log(f"coordinator: 本轮失败（忽略）: {self.last_error}")


class Cluster:
    def __init__(self, config: Config):
        self.config = config
        self.identity = Identity(config.state_dir, config.name)
        self.node_id = self.identity.node_id
        self.incarnation = self.identity.incarnation
        self.started_at = _iso()
        self.started_epoch = _now()

        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._http: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self.discovery: Discovery | None = None
        self.sync_agent: RepoSyncAgent | None = None
        self.mcp_service = None
        self.store = None
        self.archive_agent: ArchiveAgent | None = None
        self.coordinator_agent: CoordinatorAgent | None = None

        self.role = "follower"
        self.term = 0
        self.voted_for: str | None = None
        self.leader_id: str | None = None
        self.leader_url: str | None = None
        self.leader_lease_until = 0.0
        self.lease_grace_until = 0.0
        self.nodes: dict[str, dict] = {}
        self.events: deque = deque(maxlen=EVENT_LIMIT)
        self._event_ids: set[str] = set()
        self.event_seq = 0
        self.progress: dict[str, dict] = {}
        self.leases: dict[str, dict] = {}
        self.chat: deque = deque(maxlen=CHAT_LIMIT)
        self._chat_ids: set[str] = set()
        self._chat_last_epoch = 0.0
        self.generation = 0
        self.updated_at = _iso()

        self._dirty = False
        self._last_persist = 0.0
        self._last_election = 0.0
        self._leader_lost_at: float | None = None
        self._leader_fail_at: float | None = None
        self._election_inflight = False
        self._peer_errors: dict[str, float] = {}
        self._expired_reported: set[str] = set()
        self._last_store_sync = 0.0
        self._last_store_reconcile = 0.0
        # 落后当选 → 先对齐 store 再服务写。_store_lag_target 是投票时从同伴
        # 听说的最高 commit_seq；_store_serving_ready=False 期间所有 store 写
        # 一律 503 E_RECONCILING（读不受影响）。
        self._store_serving_ready = True
        self._store_lag_target = 0
        self._store_reconcile_thread: threading.Thread | None = None

        self.port = config.port
        self.advertise_host = config.advertise or (
            "" if config.bind_host in ("0.0.0.0", "", "::") else config.bind_host)
        self._load_state()
        self._load_chat()
        self._put_self_node()

    # -- 生命周期 ---------------------------------------------------------- #
    @property
    def url(self) -> str:
        return _base_url(self.advertise_host or _guess_lan_ip(), self.port)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return self._stop.wait(timeout)

    def start(self) -> "Cluster":
        if self.config.store_enabled or self.config.claim_backend == "store":
            try:
                from hive_store import Store
                self.store = Store(self.config.store_path, self.node_id)
                self._ingest_store_cards()
                self.archive_agent = ArchiveAgent(self)
                self.archive_agent.start()
            except Exception as exc:  # noqa: BLE001  store 失败不阻止集群起
                self.store = None
                _log(f"store: 打开失败（忽略）: {exc}")
        if self.config.coordinator_enabled:
            self.coordinator_agent = CoordinatorAgent(self)
            self.coordinator_agent.start()
        ThreadingHTTPServer.allow_reuse_address = True
        try:
            self._http = ThreadingHTTPServer(
                (self.config.bind_host, self.config.port), build_handler(self))
        except OSError as exc:
            raise ClusterError(
                f"监听 {self.config.bind_host}:{self.config.port} 失败：{exc}") from exc
        self._http.daemon_threads = True
        self.port = self._http.server_address[1]
        if not self.advertise_host:
            self.advertise_host = _guess_lan_ip()
        # 真实端口写回 config.json：`hive cluster status` 不带 --port 时也能找对地方
        self.config.port = self.port
        self.config.save()
        with self.lock:
            self._put_self_node_locked()
            self._mark_dirty_locked()
            self._persist_locked()
        self._write_pid()

        self._http_thread = threading.Thread(
            target=self._http.serve_forever, name="hive-cluster-http", daemon=True)
        self._http_thread.start()

        self.discovery = Discovery(self)
        self.discovery.start()
        self.sync_agent = RepoSyncAgent(self)
        self.sync_agent.start()
        if self.config.mcp_enabled:
            try:
                from mcp_http import McpHttpService
                self.mcp_service = McpHttpService(
                    self.config.hive_dir,
                    port=self.config.mcp_port,
                    bind=self.config.mcp_bind,
                    state_dir=self.config.state_dir,
                    write_dir=self.config.mcp_write_dir,
                    read_dir=self.config.mcp_read_dir,
                )
                # clone/缓存可能慢，别让 hived 的集群启动等它；后台起，失败只影响 MCP。
                threading.Thread(target=self.mcp_service.start,
                                 name="hive-mcp-start", daemon=True).start()
            except Exception as exc:  # noqa: BLE001  MCP 是可选入口
                _log(f"mcp: 启动失败（忽略）: {exc}")

        for target, name in ((self._loop_heartbeat, "hive-cluster-heartbeat"),
                             (self._loop_maintenance, "hive-cluster-maintenance"),
                             (self._loop_discovery, "hive-cluster-discovery"),
                             (self._loop_chat_sync, "hive-cluster-chat")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        _log(f"节点 {self.config.name} 启动：{self.node_id} term={self.term} "
             f"{self.url}（发现端口 {self.config.discovery_port}）")
        threading.Thread(target=self._bootstrap, name="hive-cluster-bootstrap",
                         daemon=True).start()
        return self

    def _ingest_store_cards(self) -> int:
        """把 Git 卡面同步进 store，返回补摄入的新卡数。

        多机协同的长期不变式：**Git 里有的卡，leader store 里也得有，
        而且必须走 op 日志**。旧实现只对全量调 ingest_cards（直接写库、
        不产生 op），于是出现过 leader 138 张 / follower 134 张、两边
        commit_seq 却都是 43 的情况——follower 的追平条件
        `target > commit_seq` 永远不成立，差异永远追不上，最后只能靠
        claim 报「没有任务」才被人发现。

        * 缺行 → ingest_card(commit=True)：产生 op、推进 commit_seq，
          follower 下次心跳就能靠 /op/since 追平（全集群一起补齐）。
        * 已有行 → 仍用 ingest_cards 只补卡面定义，不碰运行时
          state/owner/retries，也不制造重复 op。
        """
        if self.store is None:
            return 0
        try:
            from hive import Hive
            if not (self.config.hive_dir / ".git").exists():
                return 0
            cards = Hive(self.config.hive_dir).cards()
            existing = self.store.tasks()
            known = {t: c for t, c in cards.items() if t in existing}
            missing = {t: c for t, c in cards.items() if t not in existing}
            if known:
                self.store.ingest_cards(known)
            for tid, card in sorted(missing.items()):
                self.store.ingest_card(tid, card, commit=True)
            if missing:
                _log(f"store: 补摄入 {len(missing)} 张新卡"
                     f"（op 已推进，follower 可追平）")
            return len(missing)
        except Exception as exc:  # noqa: BLE001
            _log(f"store: 摄入任务卡失败（忽略）: {exc}")
            return 0

    def _maybe_reconcile_store_cards(self) -> int:
        """leader 低频补摄入启动后新增的卡，返回本轮补了几张。

        只做 leader、按 STORE_RECONCILE_S 节流；读的是本机 Git 卡面
        （若本机工作区滞后，follower 侧 claim 的按需补摄入是第二道防线）。
        """
        if self.role != "leader" or self.store is None:
            return 0
        now_epoch = _now()
        if now_epoch - self._last_store_reconcile < STORE_RECONCILE_S:
            return 0
        self._last_store_reconcile = now_epoch
        return self._ingest_store_cards()

    def _store_catch_up(self, base: str) -> None:
        """新节点/重连：先 snapshot，再拉已 commit 的 op。"""
        if self.store is None or not base:
            return
        base = base.rstrip("/")

        def _snapshot() -> bool:
            code, body = http_json(
                base + "/api/cluster/store/snapshot", "GET", None, 10.0,
                self.config.token)
            if code != 200 or not isinstance(body, dict):
                return False
            snap = body.get("snapshot")
            if not isinstance(snap, dict):
                return False
            self.store.import_snapshot(snap)
            return True

        try:
            local = self.store.commit_seq()
            if local <= 0:
                _snapshot()
                local = self.store.commit_seq()
            code, body = http_json(
                base + f"/api/cluster/op/since?since={local}", "GET", None,
                5.0, self.config.token)
            if code == 409 and isinstance(body, dict) and body.get("snapshot_required"):
                if _snapshot():
                    local = self.store.commit_seq()
                    code, body = http_json(
                        base + f"/api/cluster/op/since?since={local}", "GET",
                        None, 5.0, self.config.token)
            if code != 200 or not isinstance(body, dict):
                return
            ops = [op for op in (body.get("ops") or [])
                   if isinstance(op, dict) and op.get("committed")]
            self.store.truncate_uncommitted()
            n = 0
            for op in ops:
                self.store.apply_op(op)
                n += 1
            if n:
                _log(f"store: 从 {base} 追平 {n} 条 op（commit_seq="
                     f"{self.store.commit_seq()}）")
        except Exception as exc:  # noqa: BLE001
            _log(f"store: 追平失败（忽略）: {exc}")

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            if self.role == "leader":
                self._resign_peers("shutdown")
            with self.lock:
                if self.role == "leader":
                    self.role = "follower"
                    self.leader_id = None
                    self.leader_url = None
                    self.leader_lease_until = 0.0
                    self._emit_locked("leader_step_down", message="shutdown")
                self._persist_locked()
                self.cond.notify_all()
        except Exception as exc:  # noqa: BLE001 停止路径尽力而为
            _log(f"停止时保存状态失败（忽略）：{exc}")
        if self.coordinator_agent is not None:
            try:
                self.coordinator_agent.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.archive_agent is not None:
            try:
                self.archive_agent.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.mcp_service is not None:
            try:
                self.mcp_service.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.sync_agent is not None:
            self.sync_agent.stop()
        if self.discovery is not None:
            self.discovery.stop()
        if self._http is not None:
            try:
                self._http.shutdown()
                self._http.server_close()
            except OSError:
                pass
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=2.0)
        self._remove_pid()
        if self.store is not None:
            try:
                self.store.close_conn()
            except Exception:  # noqa: BLE001
                pass
            self.store = None
        _log(f"节点 {self.config.name} 已停止")

    def run_forever(self) -> int:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return 0

    def _write_pid(self) -> None:
        try:
            self.config.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.config.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

    def _remove_pid(self) -> None:
        try:
            if self.config.pid_path.exists():
                if self.config.pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    self.config.pid_path.unlink()
        except OSError:
            pass

    # -- 状态恢复 / 持久化 ------------------------------------------------- #
    def _load_state(self) -> None:
        raw = _read_json(self.config.state_path, {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        self.term = int(raw.get("term") or 0)
        self.voted_for = raw.get("voted_for")
        self.leader_id = raw.get("leader_id")
        self.leader_url = raw.get("leader_url")
        self.leader_lease_until = float(raw.get("leader_lease_until_epoch") or 0.0)
        self.lease_grace_until = float(raw.get("lease_grace_until_epoch") or 0.0)
        self.generation = int(raw.get("generation") or 0)
        self.event_seq = int(raw.get("event_seq") or 0)
        self.updated_at = raw.get("updated_at") or _iso()

        nodes = raw.get("nodes")
        if isinstance(nodes, dict):
            for nid, rec in nodes.items():
                if isinstance(rec, dict):
                    self.nodes[str(nid)] = self._normalize_node(rec)
        # 一个 IP:port 只能有一个身份。磁盘里若残留「同一 URL、两个 node_id」
        # 的旧记录（机器改过名、或历史 leader 快照被写回），保留 incarnation
        # 最新的那个，其余删掉 —— 否则重启后两个身份各算一票，两边都能凑到
        # 多数、各自宣布当选，就是反复复发的「幽灵节点」。
        self._dedupe_node_urls_locked()

        events = raw.get("events")
        if isinstance(events, list):
            for ev in events:
                if isinstance(ev, dict) and ev.get("id"):
                    self.events.append(ev)
                    self._event_ids.add(str(ev["id"]))

        progress = raw.get("progress")
        if isinstance(progress, dict):
            self.progress = {str(k): v for k, v in progress.items() if isinstance(v, dict)}
        leases = raw.get("leases")
        if isinstance(leases, dict):
            self.leases = {str(k): v for k, v in leases.items() if isinstance(v, dict)}

        # 重启后不能沿用上一世的「主节点」身份：租约过期就清掉，等心跳/选举。
        if self.leader_lease_until <= _now():
            self.leader_id = None
            self.leader_url = None
            self.leader_lease_until = 0.0
        # 自己重启后不能沿用「上一世自己还是主节点」的身份：等心跳/选举重新确认。
        if self.leader_id == self.node_id:
            self.leader_id = None
            self.leader_url = None
            self.leader_lease_until = 0.0
        self.role = "follower"

    def _mark_dirty_locked(self) -> None:
        """状态变化：推进 generation（长轮询/看板的新鲜度）并唤醒等待者。"""
        self.generation += 1
        self.updated_at = _iso()
        self._dirty = True
        try:
            self.cond.notify_all()
        except RuntimeError:      # 没拿到锁时 notify 会抛，调用方本应持锁
            pass

    def _state_payload_locked(self) -> dict:
        return {
            "protocol": PROTOCOL,
            "cluster_id": self.config.cluster_id,
            "node": self.node_id,
            "incarnation": self.incarnation,
            "term": self.term,
            "voted_for": self.voted_for,
            "leader_id": self.leader_id,
            "leader_url": self.leader_url,
            "leader_lease_until": _iso(self.leader_lease_until) if self.leader_lease_until else "",
            "leader_lease_until_epoch": self.leader_lease_until,
            "lease_grace_until": _iso(self.lease_grace_until) if self.lease_grace_until else "",
            "lease_grace_until_epoch": self.lease_grace_until,
            "nodes": copy.deepcopy(self.nodes),
            "events": list(self.events),
            "event_seq": self.event_seq,
            "progress": copy.deepcopy(self.progress),
            "leases": copy.deepcopy(self.leases),
            "generation": self.generation,
            "updated_at": self.updated_at,
            # 进程没跑时 `hive cluster status` 读 state.json 也能拿到和 HTTP 一样的视图
            "snapshot": self._snapshot_locked(),
            "saved_at": _iso(),
        }

    def _persist_locked(self) -> None:
        data = self._state_payload_locked()
        self._dirty = False
        self._last_persist = _now()
        _write_json(self.config.state_path, data)

    def _persist(self, force: bool = False) -> None:
        with self.lock:
            if not force and not self._dirty:
                return
            if not force and _now() - self._last_persist < 1.0:
                return
            try:
                data = self._state_payload_locked()
                self._dirty = False
                self._last_persist = _now()
            except Exception:  # noqa: BLE001
                data = None
        if data is not None:
            try:
                _write_json(self.config.state_path, data)
            except OSError as exc:
                _log(f"状态落盘失败（忽略）：{exc}")

    # -- 节点记录 ---------------------------------------------------------- #
    def _local_agents(self) -> list[dict]:
        out = []
        agents_dir = self.config.hive_dir / "agents"
        try:
            paths = sorted(agents_dir.glob("*.json"))
        except OSError:
            return out
        for p in paths:
            if p.name.startswith("."):
                continue
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(cfg, dict):
                continue
            out.append({
                "name": cfg.get("name") or p.stem,
                "mode": cfg.get("mode") or ("manual" if cfg.get("manual") else "exec"),
                "host": cfg.get("host") or "",
                "capabilities": cfg.get("capabilities") or [],
            })
        return out

    def _self_node(self) -> dict:
        return {
            "id": self.node_id,
            "name": self.config.name,
            "host": self.advertise_host or self.config.bind_host,
            "port": self.port,
            "url": self.url,
            "incarnation": self.incarnation,
            "started_at": self.started_at,
            "joined_at": self.started_at,
            "version": CLUSTER_VERSION,
            "protocol": PROTOCOL,
            "cluster_id": self.config.cluster_id,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "agents": self._local_agents(),
            "capabilities": [],
            "electable": bool(self.config.electable),
            # 随自述广播出去：别的节点据此排选举顺序，不必各自配置「谁是首选」。
            "priority": int(self.config.leader_priority),
            "last_store_seq": self.store.commit_seq() if getattr(self, "store", None) else 0,
            "hive_dir": str(self.config.hive_dir),
        }

    def _put_self_node(self) -> None:
        with self.lock:
            self._put_self_node_locked()

    def _put_self_node_locked(self) -> None:
        rec = self._self_node()
        rec["last_seen"] = _iso()
        rec["last_seen_epoch"] = _now()
        self.nodes[self.node_id] = rec
        self._mark_dirty_locked()

    @staticmethod
    def _normalize_node(rec: dict) -> dict:
        out = dict(rec or {})
        out["id"] = str(out.get("id") or "")
        out["name"] = str(out.get("name") or out["id"])
        out["incarnation"] = int(out.get("incarnation") or 0)
        out["last_seen_epoch"] = float(out.get("last_seen_epoch")
                                       or _epoch(out.get("last_seen")) or 0.0)
        out["last_seen"] = out.get("last_seen") or (
            _iso(out["last_seen_epoch"]) if out["last_seen_epoch"] else "")
        out.setdefault("electable", True)
        try:
            out["port"] = int(out.get("port") or 0)
        except (TypeError, ValueError):
            out["port"] = 0
        if not out.get("url") and out.get("host") and out["port"]:
            out["url"] = _base_url(out["host"], out["port"])
        return out

    def _node_status_locked(self, rec: dict, now_epoch: float | None = None) -> str:
        if rec.get("id") == self.node_id:
            return "alive"
        age = (now_epoch or _now()) - float(rec.get("last_seen_epoch") or 0.0)
        if age <= self.config.peer_timeout:
            return "alive"
        if age <= self.config.dead_timeout:
            return "suspect"
        return "dead"

    def _dedupe_node_urls_locked(self) -> None:
        """磁盘加载后清理「同一 URL、多个 node_id」的重复身份。

        一台机器一个端口只能跑一个节点进程。两个不同 id 却共用同一个
        http://host:port，必然是一台机器改过名 / 旧实例记录没删干净。留着它
        的后果很具体：两个身份各算一票，两边都能凑到多数、各自宣布当选；
        而 ``_adopt_leader_locked`` 每次收到「leader 是 X」的快照都会给 X
        ``touch=True`` 续命，所以单发 leave 删掉，下一拍又活。

        保留规则：本机自己那条绝对不动；其余按 incarnation 取最新，同值取
        node_id 字典序大的（确定性）。被删掉的身份若正是当前 leader，一并把
        leader 指向清掉，让重启后重新选举。
        """
        by_url: dict[str, list[str]] = {}
        for nid, rec in self.nodes.items():
            url = str(rec.get("url") or "").rstrip("/")
            if url:
                by_url.setdefault(url, []).append(nid)
        for url, nids in by_url.items():
            if len(nids) < 2:
                continue
            keep = max(nids, key=lambda n: (
                n == self.node_id,
                int(self.nodes[n].get("incarnation") or 0),
                n,
            ))
            for nid in nids:
                if nid == keep:
                    continue
                self.nodes.pop(nid, None)
                if self.leader_id == nid:
                    self.leader_id = None
                    self.leader_url = None
                    self.leader_lease_until = 0.0
                _log(f"集群状态加载：丢弃与 {url} 重复的旧身份 {nid}")

    def _update_node_locked(self, rec: dict, touch: bool = True,
                            joined: bool = False) -> bool:
        """合入一个节点记录。低化身号直接丢弃；返回是否写入。"""
        if not isinstance(rec, dict) or not rec.get("id"):
            return False
        nid = str(rec["id"])
        if nid == self.node_id:
            return False
        incoming = self._normalize_node(rec)
        # 同一个 IP:port 不可能同时属于两个活节点。心跳/快照若把一个 URL 相同、
        # id 不同的旧身份送回来，多半是历史幽灵（配合 _adopt_leader_locked 的
        # touch=True 会一直续命）。对方是本机、或对方化身号不比新来的旧时，
        # 一律拒绝写入；只有确实更新的实例才允许替换，并顺手清掉旧身份。
        url = str(incoming.get("url") or "").rstrip("/")
        if url:
            for other_id, other in list(self.nodes.items()):
                if other_id == nid:
                    continue
                if str(other.get("url") or "").rstrip("/") != url:
                    continue
                newer = (int(incoming.get("incarnation") or 0)
                         > int(other.get("incarnation") or 0))
                if newer and other_id != self.node_id:
                    self.nodes.pop(other_id, None)
                    if self.leader_id == other_id:
                        self.leader_id = None
                        self.leader_url = None
                        self.leader_lease_until = 0.0
                    continue
                return False
        known = self.nodes.get(nid)
        if known is not None:
            if incoming["incarnation"] < int(known.get("incarnation") or 0):
                return False
            if incoming["incarnation"] > int(known.get("incarnation") or 0):
                self._emit_locked(
                    "node_restarted", node_id=nid,
                    message=f"{incoming.get('name') or nid} incarnation "
                            f"{known.get('incarnation')} -> {incoming['incarnation']}")
        merged = dict(known or {})
        merged.update(incoming)
        if touch:
            merged["last_seen"] = _iso()
            merged["last_seen_epoch"] = _now()
        else:
            # 远端快照里的 last_seen 可能比我们本地刚收到的心跳更旧；
            # 只能取较新的那个，否则一次状态合并会把刚确认的 alive 又打回 dead。
            old_seen = float((known or {}).get("last_seen_epoch") or 0.0)
            new_seen = max(old_seen, float(incoming.get("last_seen_epoch") or 0.0))
            merged["last_seen_epoch"] = new_seen
            merged["last_seen"] = _iso(new_seen) if new_seen else ""
        merged["id"] = nid
        if joined or known is None:
            merged["joined_at"] = merged.get("joined_at") or _iso()
        self.nodes[nid] = merged
        if known is None:
            self._emit_locked("node_join", node_id=nid,
                              message=f"{merged.get('name') or nid} 加入")
        self._mark_dirty_locked()
        return True

    def _public_node_locked(self, rec: dict, now_epoch: float | None = None) -> dict:
        # `_status` 是内部缓存，不下发给对端/看板
        out = {k: v for k, v in copy.deepcopy(rec).items()
               if not str(k).startswith("_")}
        if str(rec.get("id")) == self.node_id:
            # 自己永远是 alive，last_seen 也按「刚刚」算；否则把自己的记录原样
            # 发给对端，会把对端刚收到的心跳覆盖成旧时间。
            out["last_seen_epoch"] = _now()
            out["last_seen"] = _iso()
        out["status"] = self._node_status_locked(rec, now_epoch)
        out["role"] = ("leader" if rec.get("id") == self.leader_id
                       else ("candidate" if rec.get("id") == self.node_id
                             and self.role == "candidate" else "follower"))
        out["term"] = self.term if rec.get("id") == self.node_id else int(rec.get("term") or 0)
        return out

    def _leader_record_locked(self) -> dict | None:
        if not self.leader_id:
            return None
        if self.leader_id == self.node_id:
            rec = self.nodes.get(self.node_id) or self._self_node()
        else:
            rec = self.nodes.get(self.leader_id)
        if not rec:
            return None
        out = self._public_node_locked(rec)
        out["lease_until"] = _iso(self.leader_lease_until) if self.leader_lease_until else ""
        out["lease_until_epoch"] = self.leader_lease_until
        return out

    # -- 事件 -------------------------------------------------------------- #
    def _emit_locked(self, etype: str, node_id: str = "", task: str = "",
                     message: str = "", data: dict | None = None) -> dict:
        self.event_seq += 1
        ev = {
            "id": f"{self.term}.{self.node_id}.{self.event_seq}",
            "term": self.term,
            "seq": self.event_seq,
            "ts": _iso(),
            "epoch": _now(),
            "type": etype,
            "node": node_id or self.node_id,
            "task": task,
            "message": message,
            "data": data or {},
        }
        self.events.append(ev)
        self._event_ids.add(ev["id"])
        self._trim_event_ids_locked()
        self._mark_dirty_locked()
        return ev

    def _emit(self, etype: str, **kw) -> dict:
        with self.lock:
            return self._emit_locked(etype, **kw)

    def _trim_event_ids_locked(self) -> None:
        if len(self._event_ids) > EVENT_LIMIT * 2:
            self._event_ids = {str(e.get("id")) for e in self.events if e.get("id")}

    def _merge_event_locked(self, ev: dict) -> bool:
        if not isinstance(ev, dict) or not ev.get("id"):
            return False
        eid = str(ev["id"])
        if eid in self._event_ids:
            return False
        self.events.append(ev)
        self._event_ids.add(eid)
        self._trim_event_ids_locked()
        self._mark_dirty_locked()
        return True

    # -- 聊天 / 跨机留言 --------------------------------------------------- #
    #
    # 为什么不用 git 送留言：worker 的 worktree、MCP 的独占 clone 每轮都会
    # `reset --hard origin/main`，写在仓里的留言随时被冲掉；而 `git push`
    # 一分钟只能走几次，聊天要的是秒级。所以留言走集群：内存列表 + 本地
    # chat.jsonl 追加日志（每个节点各自一份，天然分片、不会冲突），
    # follower 每 2 秒向主节点增量拉一次（leader 是唯一的写入汇聚点）。
    def _chat_path(self) -> Path:
        return Path(self.config.state_dir) / "chat.jsonl"

    def _load_chat(self) -> None:
        path = self._chat_path()
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            _log(f"chat: 读取 {path} 失败（忽略）: {exc}")
            return
        for line in lines[-CHAT_LIMIT:]:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict) or not msg.get("id"):
                continue
            mid = str(msg["id"])
            if mid in self._chat_ids:
                continue
            self.chat.append(msg)
            self._chat_ids.add(mid)
            self._chat_last_epoch = max(self._chat_last_epoch,
                                        float(msg.get("epoch") or 0.0))

    def _chat_append_locked(self, msg: dict) -> bool:
        """落一条消息：内存 + 磁盘追加。同 id 视为重复（多节点回传时常见）。"""
        mid = str(msg.get("id") or "")
        if not mid or mid in self._chat_ids:
            return False
        item = {
            "id": mid,
            "ts": str(msg.get("ts") or _iso()),
            "epoch": float(msg.get("epoch") or _now()),
            "from": str(msg.get("from") or "")[:80],
            "node": str(msg.get("node") or self.node_id),
            "node_name": str(msg.get("node_name") or self.config.name),
            "scope": _norm_scope(msg.get("scope")),
            "kind": str(msg.get("kind") or "note"),
            "text": str(msg.get("text") or "")[:CHAT_MAX_TEXT],
            "reply_to": str(msg.get("reply_to") or ""),
        }
        self.chat.append(item)
        self._chat_ids.add(mid)
        if len(self._chat_ids) > CHAT_LIMIT * 2:
            self._chat_ids = {str(m.get("id")) for m in self.chat}
        self._chat_last_epoch = max(self._chat_last_epoch, item["epoch"])
        try:
            with self._chat_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError as exc:
            _log(f"chat: 追加写盘失败: {exc}")
        self._mark_dirty_locked()
        return True

    def chat_meta(self) -> dict:
        with self.lock:
            return {"last_epoch": self._chat_last_epoch, "count": len(self.chat)}

    def chat_list(self, since: float = 0.0, scope: str = "",
                  limit: int = 200) -> list[dict]:
        scopes = {s.strip() for s in (scope or "").split(",") if s.strip()}
        with self.lock:
            items = [m for m in self.chat
                     if float(m.get("epoch") or 0) > since
                     and (not scopes or str(m.get("scope")) in scopes)]
        items.sort(key=lambda m: (float(m.get("epoch") or 0), str(m.get("id"))))
        if limit > 0:
            items = items[-limit:]
        return copy.deepcopy(items)

    def handle_chat_post(self, body: dict) -> tuple[int, dict]:
        """发一条留言。follower 转发给主节点（保证全网同一份日志），
        主节点不可达时本地留存——本地看板照样看得见，等它当上主节点时再服务别人。"""
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        text = str(body.get("text") or "").strip()
        if not text:
            return 400, {"ok": False, "error": "text 不能为空"}
        if len(text) > CHAT_MAX_TEXT:
            return 400, {"ok": False,
                         "error": f"text 太长（{len(text)} > {CHAT_MAX_TEXT}）"}
        with self.lock:
            role, leader_url = self.role, str(self.leader_url or "")
        # 复制模式：别的节点把自己写的、主节点还没有的留言原样送过来，
        # 保留原 id / 时间戳（按 id 去重，重复投递无害）。
        if body.get("replicate") and body.get("id"):
            msg = {
                "id": str(body["id"]),
                "ts": str(body.get("ts") or _iso()),
                "epoch": float(body.get("epoch") or _now()),
                "from": str(body.get("from") or "")[:80],
                "node": str(body.get("node") or ""),
                "node_name": str(body.get("node_name") or ""),
                "scope": _norm_scope(body.get("scope")),
                "kind": str(body.get("kind") or "note"),
                "text": text,
                "reply_to": str(body.get("reply_to") or ""),
            }
            with self.lock:
                created = self._chat_append_locked(msg)
                if created:
                    self._persist()
            return 200, {"ok": True, "message": msg, "replicated": created}
        if role != "leader" and leader_url and not body.get("forwarded"):
            fwd = dict(body)
            fwd["forwarded"] = True
            # 记下真正的发起方：主节点只是代写，不该把留言算到自己头上
            fwd["origin_node"] = self.node_id
            fwd["origin_name"] = self.config.name
            try:
                code, resp = http_json(
                    leader_url.rstrip("/") + "/api/cluster/chat", "POST", fwd,
                    2.0, self.config.token)
            except Exception:  # noqa: BLE001  主节点刚挂：落到本地
                code, resp = 0, None
            if code == 200 and isinstance(resp, dict) and resp.get("message"):
                with self.lock:
                    self._chat_append_locked(resp["message"])
                return 200, {"ok": True, "message": resp["message"],
                             "forwarded": True}
        msg = {
            "id": _chat_id(),
            "ts": _iso(),
            "epoch": _now(),
            "from": str(body.get("from") or "").strip()[:80],
            "node": str(body.get("origin_node") or self.node_id),
            "node_name": str(body.get("origin_name") or self.config.name),
            "scope": _norm_scope(body.get("scope")),
            "kind": str(body.get("kind") or "note"),
            "text": text,
            "reply_to": str(body.get("reply_to") or ""),
        }
        with self.lock:
            self._chat_append_locked(msg)
            self._persist()
        return 200, {"ok": True, "message": msg, "forwarded": False}

    def _chat_pull_once(self) -> None:
        with self.lock:
            role = self.role
            base = str(self.leader_url or "")
            since = self._chat_last_epoch
        if role == "leader" or not base:
            return
        url = "%s/api/cluster/chat?since=%.6f&limit=%d" % (
            base.rstrip("/"), since, CHAT_LIMIT)
        code, body = http_json(url, "GET", None, 3.0, self.config.token)
        if code != 200 or not isinstance(body, dict):
            return
        msgs = body.get("messages") or []
        if not isinstance(msgs, list):
            return
        with self.lock:
            n = 0
            for m in msgs:
                if isinstance(m, dict) and self._chat_append_locked(m):
                    n += 1
            if n:
                self._persist()
        if n:
            _log(f"chat: 从主节点同步 {n} 条")
        # 反方向：主节点没有、我这儿有的（通常是我这边短暂失去主节点时发的）。
        # 送过去让全网都能看到，不丢消息。
        try:
            leader_last = float(body.get("last_epoch") or 0.0)
        except (TypeError, ValueError):
            leader_last = 0.0
        with self.lock:
            orphans = [m for m in self.chat
                       if float(m.get("epoch") or 0) > leader_last
                       and str(m.get("node") or "") == self.node_id][-20:]
        for m in orphans:
            payload = dict(m)
            payload["replicate"] = True
            http_json(base.rstrip("/") + "/api/cluster/chat", "POST", payload,
                      3.0, self.config.token)

    def _loop_chat_sync(self) -> None:
        """留言不要等 git fetch：每 2 秒向主节点要一次增量。"""
        while not self._stop.wait(2.0):
            try:
                self._chat_pull_once()
            except Exception as exc:  # noqa: BLE001  网络抖一下不算故障
                _log(f"chat: 同步失败（忽略）: {exc}")

    # -- 进度 / 租约 -------------------------------------------------------- #
    @staticmethod
    def _progress_newer(new: dict, old: dict) -> bool:
        return (int(new.get("term") or 0), int(new.get("gen") or 0),
                float(new.get("updated_epoch") or 0.0)) > (
                    int(old.get("term") or 0), int(old.get("gen") or 0),
                    float(old.get("updated_epoch") or 0.0))

    def _merge_progress_locked(self, tid: str, item: dict) -> bool:
        if not tid or not isinstance(item, dict):
            return False
        old = self.progress.get(tid)
        if old is not None and not self._progress_newer(item, old):
            return False
        self.progress[tid] = copy.deepcopy(item)
        self._mark_dirty_locked()
        return True

    def _apply_progress_locked(self, payload: dict, leader: bool) -> dict:
        tid = str(payload.get("task") or payload.get("tid") or "").strip()
        if not tid:
            raise ClusterError("进度更新缺少 task")
        now_epoch = _now()
        incoming_term = int(payload.get("term") or 0)
        old = self.progress.get(tid) or {}
        gen = int(payload.get("gen") or 0) or int(old.get("gen") or 0) + 1
        item = {
            "task": tid,
            "agent": str(payload.get("agent") or ""),
            "node_id": str(payload.get("node_id") or payload.get("origin") or self.node_id),
            "origin": str(payload.get("origin") or payload.get("node_id") or self.node_id),
            "state": str(payload.get("state") or payload.get("push_state") or ""),
            "progress": _clamp_progress(payload.get("progress")),
            "step": str(payload.get("step") or ""),
            "message": str(payload.get("message") or ""),
            "updated_at": str(payload.get("updated_at") or _iso()),
            "updated_epoch": float(payload.get("updated_epoch") or now_epoch),
            "term": max(incoming_term, self.term),
            "leader_id": self.leader_id or (self.node_id if leader else ""),
            "gen": gen,
        }
        self._merge_progress_locked(tid, item)
        self._emit_locked(
            "task_progress", node_id=item["node_id"], task=tid,
            message=item["message"] or item["step"] or item["state"],
            data={k: item.get(k) for k in ("state", "progress", "step", "agent")})
        return item

    @staticmethod
    def _lease_newer(new: dict, old: dict) -> bool:
        return (int(new.get("term") or 0), int(new.get("gen") or 0),
                float(new.get("updated_epoch") or 0.0)) > (
                    int(old.get("term") or 0), int(old.get("gen") or 0),
                    float(old.get("updated_epoch") or 0.0))

    def _merge_lease_locked(self, tid: str, item: dict) -> bool:
        if not tid or not isinstance(item, dict):
            return False
        old = self.leases.get(tid)
        if old is not None and not self._lease_newer(item, old):
            return False
        merged = copy.deepcopy(item)
        if old is not None:
            merged["grace_until_epoch"] = max(
                float(merged.get("grace_until_epoch") or 0.0),
                float(old.get("grace_until_epoch") or 0.0))
            merged["grace_until"] = (_iso(merged["grace_until_epoch"])
                                     if merged.get("grace_until_epoch") else "")
        self.leases[tid] = merged
        self._mark_dirty_locked()
        return True

    def _apply_lease_locked(self, payload: dict, leader: bool) -> dict:
        tid = str(payload.get("task") or "").strip()
        if not tid:
            raise ClusterError("租约更新缺少 task")
        now_epoch = _now()
        old = self.leases.get(tid) or {}
        incoming_term = int(payload.get("term") or 0)
        lease_until_epoch = float(payload.get("lease_until_epoch")
                                  or _epoch(payload.get("lease_until")) or 0.0)
        if lease_until_epoch <= 0:
            lease_until_epoch = now_epoch + int(payload.get("lease_minutes") or 180) * 60
        gen = int(payload.get("gen") or 0) or int(old.get("gen") or 0) + 1
        item = {
            "task": tid,
            "agent": str(payload.get("agent") or old.get("agent") or ""),
            "node_id": str(payload.get("node_id") or self.node_id),
            "host": str(payload.get("host") or ""),
            "claimed_at": str(payload.get("claimed_at") or old.get("claimed_at") or _iso()),
            "lease_until": _iso(lease_until_epoch),
            "lease_until_epoch": lease_until_epoch,
            "term": max(incoming_term, self.term),
            "leader_id": self.leader_id or (self.node_id if leader else ""),
            "gen": gen,
            "updated_at": str(payload.get("updated_at") or _iso()),
            "updated_epoch": float(payload.get("updated_epoch") or now_epoch),
            "released": bool(payload.get("released") or False),
            "grace_until": str(old.get("grace_until") or ""),
            "grace_until_epoch": float(old.get("grace_until_epoch") or 0.0),
            "recovered_by": str(old.get("recovered_by") or ""),
        }
        self._merge_lease_locked(tid, item)
        self._emit_locked(
            "lease_released" if item["released"] else "lease_claimed",
            node_id=item["node_id"], task=tid,
            message=f"{item['agent'] or '-'} {'释放' if item['released'] else '持有'}"
                    f"，至 {item['lease_until']}")
        return item

    def _recover_leases_locked(self) -> None:
        """新主上任：给在飞租约一个恢复宽限期，避免刚换主就重派。"""
        grace_until = _now() + self.config.lease_grace
        changed = 0
        for tid, item in list(self.leases.items()):
            if item.get("released"):
                continue
            item = dict(item)
            item["grace_until_epoch"] = grace_until
            item["grace_until"] = _iso(grace_until)
            item["recovered_by"] = self.node_id
            item["term"] = self.term
            item["leader_id"] = self.node_id
            item["gen"] = int(item.get("gen") or 0) + 1
            item["updated_at"] = _iso()
            item["updated_epoch"] = _now()
            self.leases[tid] = item
            changed += 1
        if changed:
            self.lease_grace_until = grace_until
            self._emit_locked(
                "lease_grace_started",
                message=f"新主接管，{changed} 张在飞租约进入 "
                        f"{int(self.config.lease_grace)}s 恢复宽限期")

    # -- 快照 -------------------------------------------------------------- #
    def _snapshot_locked(self, include_events: bool = True) -> dict:
        now_epoch = _now()
        nodes = [self._public_node_locked(n, now_epoch)
                 for n in sorted(self.nodes.values(),
                                 key=lambda x: str(x.get("name") or x.get("id")))]
        alive = [n for n in nodes if n.get("status") == "alive"]
        voters = [n for n in nodes if n.get("status") in ("alive", "suspect")]
        return {
            "protocol": PROTOCOL,
            "cluster_id": self.config.cluster_id,
            "term": self.term,
            "role": self.role,
            "leader": self._leader_record_locked(),
            "leader_lease_until": _iso(self.leader_lease_until)
            if self.leader_lease_until else "",
            "leader_lease_until_epoch": self.leader_lease_until,
            "lease_grace_until": _iso(self.lease_grace_until)
            if self.lease_grace_until else "",
            "lease_grace_until_epoch": self.lease_grace_until,
            "nodes": nodes,
            "events": copy.deepcopy(list(self.events)) if include_events else [],
            "progress": copy.deepcopy(self.progress),
            "leases": copy.deepcopy(self.leases),
            "generation": self.generation,
            "updated_at": self.updated_at,
            "self": self._public_node_locked(self.nodes.get(self.node_id) or self._self_node(),
                                             now_epoch),
            "alive": len(alive),
            "known": len(nodes),
            "quorum": self._quorum_locked(),
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()

    def _quorum_locked(self) -> int:
        voters = len(self._alive_voter_ids_locked())
        return 1 if voters <= 1 else voters // 2 + 1

    def _alive_voter_ids_locked(self) -> list[str]:
        ids = [self.node_id] if self.config.electable else []
        for nid, rec in self.nodes.items():
            if (nid != self.node_id and self._node_status_locked(rec) == "alive"
                    and rec.get("electable", True)):
                ids.append(nid)
        return ids

    def _node_priority_locked(self, nid: str) -> int:
        """某个可参选节点的 OSPF 优先级。

        自己的用配置值而不是节点表 —— 节点表里那条是上次落盘的快照，
        刚改完优先级还没广播出去时会读到旧值，导致「明明改成 2 了却不生效」。
        """
        if nid == self.node_id:
            return normalize_leader_priority(self.config.leader_priority)
        rec = self.nodes.get(nid) or {}
        return normalize_leader_priority(rec.get("priority"))

    def _priority_zero_excludes_locked(self) -> bool:
        """priority 0 此刻是否意味着「禁赛」。

        OSPF 里 0 是**硬资格**（放弃 DR/BDR 竞选），不只等于「懒得先开口」：
        除了自己不发起选举，别的节点也不该把票投给它。唯一例外是全员都填 0，
        那时退化成人人 1（见 _election_delay_locked），0 就不再是禁赛标志，
        否则一个节点都不肯发起选举、集群永远没有主控。
        """
        voters = self._alive_voter_ids_locked()
        return any(self._node_priority_locked(n) > 0 for n in voters)

    def _election_delay_locked(self) -> float:
        """本机在「当前无主控」时该等多久才发起选举。

        OSPF 的 DR 选举：优先级数值大的先当，同优先级按 Router ID 定序；
        **不抢占**已经在位的 DR。这里完全照搬，所以：
          - 只有当前没有主控时才调用（调用点在维护循环里，由 leader 租约决定）；
          - 排序键是 (-priority, node_id)，名次越靠前等得越短。
        优先级 0 = 不参选（OSPF 里 priority 0 的接口永远不会成为 DR），
        所以返回一个「永远等不到」的值；但若全集群都是 0，就退化成人人 1，
        否则会没有节点愿意发起选举、集群永远没有主控。
        """
        voters = sorted(self._alive_voter_ids_locked())
        # 同优先级用 node_id 兜底定序，和 OSPF 用 Router ID 打平手一个道理。
        ordered = sorted(voters, key=lambda n: (-self._node_priority_locked(n), n))
        rank = ordered.index(self.node_id) if self.node_id in ordered else 0

        # 0 = 放弃竞选资格：只要集群里还有正常优先级的节点，就永不发起选举。
        # 全员 0 时退化成人人 1（_priority_zero_excludes_locked 返回假），不拦。
        if (self._node_priority_locked(self.node_id) <= 0
                and self._priority_zero_excludes_locked()):
            return float("inf")

        step = max(self.config.election_jitter, MIN_ELECTION_STEP_S)
        delay = self.config.election_min + rank * step

        pref = self._preferred_leader_id_locked()
        if pref and self._node_priority_locked(pref) <= 0:
            # 被 preferred 指定的那台自己弃了权，还让全集群给它让路会导致
            # 没人肯发起选举 —— 视同没配这个偏好，退回按优先级来。
            pref = ""
        if not pref or pref not in voters:
            # 没配偏好，或首选节点当前不在线 / 不参选：按优先级规则来。
            # 这里必须退回原逻辑，否则一个永远离线的 preferred 会让整个集群
            # 每次选主都白等一个 window。
            return delay
        if pref == self.node_id:
            # 人工硬指定：立刻可竞选，压过一切优先级。
            return 0.0
        # 其它节点让出一整个选举窗口，别跟被指定的那台同轮打平。
        return delay + self.config.election_min

    def _preferred_leader_id_locked(self) -> str:
        """把配置里的 preferred_leader 解析成节点 id。

        配置里人写的是记得住的名字（fb98 / laiweilideMacBook-Air），
        而节点表里 id 是随机串，所以每次都要现解析一遍。
        解析不出来就返回空 → 调用方退回默认选主行为。
        """
        want = (self.config.preferred_leader or "").strip()
        if not want:
            return ""
        if want in self.nodes:
            return want
        for nid, rec in self.nodes.items():
            if str(rec.get("name") or "") == want:
                return nid
        return ""

    # -- 合并远端状态 ------------------------------------------------------- #
    def _merge_snapshot_locked(self, snap: dict, from_leader: bool = False) -> None:
        if not isinstance(snap, dict):
            return
        if isinstance(snap.get("state"), dict):
            snap = snap["state"]
        term = int(snap.get("term") or 0)
        if term > self.term:
            self.term = term
            self.voted_for = None
            self.event_seq = 0
            if self.role == "leader":
                self.role = "follower"
                self.leader_id = None
                self.leader_url = None
            self._mark_dirty_locked()
        leader = snap.get("leader")
        if isinstance(leader, dict) and leader.get("id"):
            self._adopt_leader_locked(leader, term, refresh_lease=from_leader)
        for rec in snap.get("nodes") or []:
            if isinstance(rec, dict):
                self._update_node_locked(rec, touch=False)
        for ev in snap.get("events") or []:
            self._merge_event_locked(ev)
        progress = snap.get("progress")
        if isinstance(progress, dict):
            for tid, item in progress.items():
                if isinstance(item, dict):
                    self._merge_progress_locked(str(tid), item)
        leases = snap.get("leases")
        if isinstance(leases, dict):
            for tid, item in leases.items():
                if isinstance(item, dict):
                    self._merge_lease_locked(str(tid), item)
        self._mark_dirty_locked()

    def _merge_body_state_locked(self, body: dict) -> None:
        for key in ("progress", "leases"):
            data = body.get(key)
            if isinstance(data, dict):
                for tid, item in data.items():
                    if not isinstance(item, dict):
                        continue
                    if key == "progress":
                        self._merge_progress_locked(str(tid), item)
                    else:
                        self._merge_lease_locked(str(tid), item)
        for ev in body.get("events") or []:
            self._merge_event_locked(ev)

    # -- 主节点 ------------------------------------------------------------ #
    def _adopt_leader_locked(self, rec: dict, term: int,
                             refresh_lease: bool = True) -> bool:
        if not isinstance(rec, dict) or not rec.get("id"):
            return False
        lid = str(rec["id"])
        if lid == self.node_id:
            return True
        if term < self.term:
            return False
        known = self.nodes.get(lid)
        if known and int(rec.get("incarnation") or 0) < int(known.get("incarnation") or 0):
            return False  # 过期化身号：别让旧实例的死亡传闻/主节点身份复活
        higher_term = term > self.term
        if higher_term:
            self.term = term
            self.voted_for = None
            self.event_seq = 0
        if self.role == "leader":
            # 同任期的双主：id 大的赢；更高任期则无条件让位。
            if not higher_term and lid < self.node_id:
                return False
            self._emit_locked("leader_step_down",
                              message=f"让位给 term={term} 的 {rec.get('name') or lid}")
            self.role = "follower"
        self._update_node_locked(rec, touch=True)
        changed = self.leader_id != lid
        self.leader_id = lid
        self.leader_url = str(rec.get("url") or self.leader_url or "")
        if refresh_lease:
            self.leader_lease_until = _now() + self.config.leader_lease
        else:
            # 从同伴快照里「听说」的主节点不能当直接心跳：沿用对方记录的租约，
            # 绝不能续成 now+lease，否则两个跟随者会互相把已死的旧主保活。
            remote_lease = float(rec.get("lease_until_epoch") or 0.0)
            if remote_lease > 0:
                self.leader_lease_until = min(
                    remote_lease, _now() + self.config.leader_lease)
            elif not self.leader_lease_until:
                self.leader_lease_until = 0.0
        self._leader_lost_at = None
        self._leader_fail_at = None
        if changed:
            self._emit_locked("leader_seen", message=f"主节点 -> {rec.get('name') or lid}")
        self._mark_dirty_locked()
        return True

    def _clear_leader_locked(self, reason: str) -> None:
        if not self.leader_id:
            return
        old = self.leader_id
        self.leader_id = None
        self.leader_url = None
        self.leader_lease_until = 0.0
        self._leader_lost_at = _now()
        self._emit_locked("leader_lost", message=f"{old} 失联：{reason}")
        self._mark_dirty_locked()

    def _become_leader_locked(self) -> None:
        if self.store is not None:
            try:
                self.store.truncate_uncommitted()
            except Exception:  # noqa: BLE001
                pass
        old_role = self.role
        self.role = "leader"
        self.leader_id = self.node_id
        self.leader_url = self.url
        self.leader_lease_until = _now() + self.config.leader_lease
        self.voted_for = self.node_id
        self._leader_lost_at = None
        self._leader_fail_at = None
        self.event_seq = 0
        self._recover_leases_locked()
        if self.store is not None:
            threading.Thread(target=self._broadcast_truncate,
                             name="hive-store-truncate", daemon=True).start()
        self._emit_locked(
            "leader_elected",
            message=f"{self.config.name} 成为主节点，term={self.term}"
                    + ("" if old_role == "candidate" else f"（原 {old_role}）"))
        self._mark_dirty_locked()
        # 落后当选：先对齐 store 再服务写请求（与投票闸门配套，缺一半就是
        # 拿旧 commit_seq 起新 oplog 的分叉风险，见 T-0295）。
        self._arm_store_reconcile_locked()

    def _arm_store_reconcile_locked(self) -> bool:
        """落后当选时把「写请求」关到 store 追平为止；返回是否进入对齐态。

        调用方持有 self.lock。没有 store、或本机不比投票人落后时立刻放行
        （纯 git 模式完全不受影响）。
        """
        self._store_serving_ready = True
        if self.store is None:
            return False
        want = int(self._store_lag_target or 0)
        have = int(self.store.commit_seq() or 0)
        if want <= have:
            return False
        targets = [str(rec.get("url")) for nid, rec in self.nodes.items()
                   if nid != self.node_id and rec.get("url")]
        self._store_serving_ready = False
        self._emit_locked(
            "store_reconcile_started",
            message=f"当选后先对齐 store：{have} -> {want}",
            data={"have": have, "want": want, "peers": len(targets)})
        thread = threading.Thread(
            target=self._reconcile_store_after_win,
            args=(targets, want), name="hive-store-reconcile", daemon=True)
        self._store_reconcile_thread = thread
        thread.start()
        return True

    def _reconcile_store_after_win(self, targets: list[str], want: int) -> None:
        """当选后从同伴拉 snapshot/op，追到 want 再开放写。

        追不平也**不能**永久关着写闸 —— 那等于把「无主」换成「有主但瘫痪」，
        比 T-0295 想修的毛病更糟。所以给一个有界的窗口，超时后放行并留下
        store_reconcile_failed 事件，让运维/看板能看见这次降级。
        """
        deadline = _now() + STORE_RECONCILE_WIN_S
        try:
            while not self._stop.is_set() and _now() < deadline:
                for base in list(targets):
                    if self.store is None:
                        break
                    try:
                        self._store_catch_up(base)
                    except Exception as exc:  # noqa: BLE001
                        _log(f"store: 从 {base} 对齐失败（继续试）: {exc!r}")
                    if int(self.store.commit_seq() or 0) >= want:
                        break
                if self.store is not None and int(self.store.commit_seq() or 0) >= want:
                    break
                if self._stop.wait(0.5):
                    break
        finally:
            with self.lock:
                self._store_serving_ready = True
                have = int(self.store.commit_seq() or 0) if self.store else 0
                ok = have >= want
                self._emit_locked(
                    "store_reconciled" if ok else "store_reconcile_failed",
                    message=(f"store 已对齐到 {have}（目标 {want}）" if ok else
                             f"store 未追平（{have}/{want}），已放行写请求但数据落后"),
                    data={"have": have, "want": want, "ok": ok})
                self._persist_locked()

    def _leader_public_locked(self) -> dict | None:
        return self._leader_record_locked()

    # -- 选主 -------------------------------------------------------------- #
    def _sync_term_from_peers(self) -> None:
        """把本机任期追到集群里看到的最高值。

        重启过的节点 term 往往落后一整截（在位主控 term=32，本机还停在 15）。
        这时直接 term+1 仍然比对方小，投票会被 stale_term 拒掉，
        现象是「敲了 promote，什么也没发生」—— 比报错难查得多。
        health 端点免 token 且带 term，正好用来做这一趟。
        """
        peers = [rec for rec in list(self.nodes.values())
                 if rec.get("id") != self.node_id and rec.get("url")]
        if not peers:
            return
        best = self.term
        with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
            futs = [pool.submit(http_json,
                                str(p["url"]).rstrip("/") + "/api/cluster/health",
                                "GET", None, 0.8, self.config.token) for p in peers]
            for fut in as_completed(futs):
                try:
                    code, resp = fut.result()
                except Exception:  # noqa: BLE001  单个节点不通不影响夺权
                    continue
                if code == 200 and isinstance(resp, dict):
                    best = max(best, int(resp.get("term") or 0))
        if best > self.term:
            with self.lock:
                self._adopt_higher_term_locked(best)
            _log(f"夺权前对齐任期：{best}")

    def _start_election(self, force: bool = False) -> bool:
        """发起一轮选举。force=True 是人工 promote 通道。

        返回是否真的发起了 —— 已经是主控 / 不可参选 / 被节流都返回 False，
        调用方可以据此给出准确提示，而不是假装成功。
        """
        if force:
            self._sync_term_from_peers()
        with self.lock:
            if self._stop.is_set() or self._election_inflight:
                return False
            if not self.config.electable:
                return False
            # 双保险：0 是硬资格，自动通道靠 delay=inf 挡过一次，这里再挡一次。
            # 防的是旧配置、老代码、或全员 0 退化边界时序让弃权节点抢跑。
            if (self._node_priority_locked(self.node_id) <= 0
                    and self._priority_zero_excludes_locked()):
                return False
            if self.role == "leader" and self.leader_lease_until > _now():
                return False
            # 失败后不要每 0.2s 就 term+1：至少隔一个选举超时再试。
            # 人工 promote 是明确意图，不受这个节流限制 —— 否则用户点了之后
            # 还要等一个 window 才生效，会以为「没反应」。
            if (not force and self._last_election
                    and _now() - self._last_election < self.config.election_min):
                return False
            self._election_inflight = True
            self.term += 1
            self.voted_for = self.node_id
            self.role = "candidate"
            self.leader_id = None
            self.leader_url = None
            self.leader_lease_until = 0.0
            self.event_seq = 0
            self._last_election = _now()
            self._emit_locked("election_started",
                              message=f"{self.config.name} 竞选 term={self.term}")
            self._persist_locked()
            term = self.term
            voters = self._alive_voter_ids_locked()
            quorum = 1 if len(voters) <= 1 else len(voters) // 2 + 1
            peers = [rec for nid, rec in self.nodes.items()
                     if nid != self.node_id and nid in voters and rec.get("url")]
            body = {"term": term, "candidate": self._self_node(),
                    "last_event": self.events[-1]["id"] if self.events else "",
                    "last_store_seq": self.store.commit_seq() if self.store else 0}
            if force:
                # 让对端知道这是人工夺权，可以跳过「store 落后不许当选」那道闸。
                body["force"] = True

        votes = {self.node_id}
        # 投票人回给我们的 store 位点：当选后要先追到这里再服务写（T-0295）。
        store_ahead = 0
        try:
            if peers:
                with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
                    futs = {pool.submit(http_json,
                                        str(p["url"]).rstrip("/") + "/api/cluster/vote",
                                        "POST", body, 0.8, self.config.token): p
                            for p in peers}
                    for fut in as_completed(futs):
                        code, resp = fut.result()
                        if not isinstance(resp, dict):
                            continue
                        if resp.get("term") and int(resp["term"]) > self.term:
                            with self.lock:
                                self._adopt_higher_term_locked(int(resp["term"]),
                                                               resp.get("leader"))
                            continue
                        if code == 200 and resp.get("granted"):
                            votes.add(str(futs[fut].get("id")))
                            try:
                                store_ahead = max(store_ahead,
                                                  int(resp.get("store_seq") or 0))
                            except (TypeError, ValueError):
                                pass
        finally:
            with self.lock:
                self._election_inflight = False
        with self.lock:
            if (self.role == "candidate" and self.term == term
                    and len(votes) >= quorum):
                # 落后当选也要赢，但赢下来先对齐 store 再服务写：不让新主控拿
                # 旧 commit_seq 起一段新 oplog（那才是「孤身称主」的真危险）。
                self._store_lag_target = max(
                    store_ahead,
                    self.store.commit_seq() if self.store else 0)
                self._become_leader_locked()
                self._persist_locked()
                became = True
            else:
                became = False
        if became:
            _log(f"当选主节点：term={term}（{len(votes)}/{quorum} 票）")
            self._heartbeat_once()
        else:
            _log(f"本轮选举未获多数：term={term}（{len(votes)}/{quorum} 票）")
        return became

    def _adopt_higher_term_locked(self, term: int, leader=None) -> None:
        if term <= self.term:
            return
        self.term = term
        self.voted_for = None
        self.event_seq = 0
        self.role = "follower"
        self.leader_id = None
        self.leader_url = None
        self.leader_lease_until = 0.0
        self._leader_lost_at = _now()
        self._emit_locked("term_advanced", message=f"任期推进到 {term}")
        self._mark_dirty_locked()
        if isinstance(leader, dict):
            self._adopt_leader_locked(leader, term)

    # -- 心跳 / 维护循环 ---------------------------------------------------- #
    def _loop_heartbeat(self) -> None:
        while not self._stop.wait(max(0.05, self.config.heartbeat_interval)):
            try:
                self._heartbeat_once()
            except Exception as exc:  # noqa: BLE001  单次心跳失败不能杀循环
                _log(f"心跳循环异常（继续）：{exc!r}")

    def _heartbeat_once(self) -> None:
        with self.lock:
            role = self.role
            # leader 连 dead 节点也要探：两台机器重启/失联后互判 dead 时，若双方
            # 都只给 alive/suspect 发心跳，就谁也不先说话 —— 各自以「1 节点多数」
            # 当选后永久双主（2026-10-31 实测：fb98 重启自选 term=6 与 0.100 撞车，
            # 互相标记 dead、不再互发心跳，只能人工发一次心跳才解）。探 dead 的
            # 代价只是对一个不可达地址的短超时请求，换来同 term 双主能在下一跳自愈。
            statuses = (("alive", "suspect", "dead") if role == "leader"
                        else ("alive", "suspect"))
            peers = [dict(rec) for nid, rec in self.nodes.items()
                     if nid != self.node_id and rec.get("url")
                     and self._node_status_locked(rec) in statuses]
            leader_url = self.leader_url
            leader_id = self.leader_id
            if role == "leader":
                self.leader_lease_until = _now() + self.config.leader_lease
                body = {"node": self._self_node(), "term": self.term,
                        "leader": True, "leader_url": self.url,
                        "lease_until": _iso(self.leader_lease_until),
                        "progress": copy.deepcopy(self.progress),
                        "leases": copy.deepcopy(self.leases)}
                # 心跳更新 leader_lease_until 但不推进 generation：长轮询等的是
                # 真实状态变化，不是「主节点还活着」这件事。
            else:
                body = {"node": self._self_node(), "term": self.term,
                        "leader": False,
                        "progress": copy.deepcopy(self.progress),
                        "leases": copy.deepcopy(self.leases)}
        if role == "leader":
            self._leader_heartbeat(peers, body)
        elif role == "follower":
            # 跟随者之间也互发轻量心跳：否则主节点一死，B/C 只能靠主节点转发
            # 的 last_seen 判断对方死活，可能同时把对方判死、各自以「1 节点多数」
            # 当选，造成可避免的双主。
            gossip = [p for p in peers
                      if str(p.get("id")) != str(leader_id or "")]
            if gossip:
                self._peer_heartbeat(gossip, body)
            if leader_url:
                self._follower_heartbeat(leader_url, body)

    def _leader_heartbeat(self, peers: list[dict], body: dict) -> None:
        if not peers:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
            futs = {pool.submit(http_json,
                                str(p["url"]).rstrip("/") + "/api/cluster/heartbeat",
                                "POST", body, 0.9, self.config.token): p
                    for p in peers}
            for fut in as_completed(futs):
                peer = futs[fut]
                code, resp = fut.result()
                if not isinstance(resp, dict):
                    with self.lock:
                        self._peer_errors[str(peer.get("id"))] = _now()
                    continue
                with self.lock:
                    rec = self.nodes.get(str(peer.get("id")))
                    if rec is not None:
                        rec["last_seen"] = _iso()
                        rec["last_seen_epoch"] = _now()
                    self._merge_body_state_locked(resp)
                    if resp.get("leader") and int(resp.get("term") or 0) >= self.term:
                        leader = resp["leader"]
                        if isinstance(leader, dict) and leader.get("id") != self.node_id:
                            self._adopt_leader_locked(leader, int(resp.get("term") or 0))
                    if int(resp.get("term") or 0) > self.term:
                        self._adopt_higher_term_locked(int(resp["term"]), resp.get("leader"))
                    self._mark_dirty_locked()

    def _follower_heartbeat(self, leader_url: str, body: dict) -> None:
        code, resp = http_json(str(leader_url).rstrip("/") + "/api/cluster/heartbeat",
                               "POST", body, 1.0, self.config.token)
        with self.lock:
            if isinstance(resp, dict):
                self._leader_fail_at = None
                self._merge_snapshot_locked(resp, from_leader=True)
                leader = resp.get("leader")
                if isinstance(leader, dict):
                    self._adopt_leader_locked(leader, int(resp.get("term") or self.term))
                self._mark_dirty_locked()
            else:
                self._leader_fail_at = _now()
                if (self.leader_lease_until and _now() > self.leader_lease_until
                        and self.role != "leader"):
                    self._clear_leader_locked("心跳超时")
        target = int((resp or {}).get("store_commit_seq") or 0) if isinstance(resp, dict) else 0
        if (self.store is not None and target > self.store.commit_seq()
                and _now() - self._last_store_sync >= 5):
            self._last_store_sync = _now()
            threading.Thread(target=self._store_catch_up, args=(leader_url,),
                             name="hive-store-catchup", daemon=True).start()

    def _peer_heartbeat(self, peers: list[dict], body: dict) -> None:
        """跟随者之间的状态交换（发现 + 活跃度 + 进度/租约的旁路同步）。"""
        with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
            futs = {pool.submit(http_json,
                                str(p["url"]).rstrip("/") + "/api/cluster/heartbeat",
                                "POST", body, 0.7, self.config.token): p
                    for p in peers}
            for fut in as_completed(futs):
                peer = futs[fut]
                code, resp = fut.result()
                if not isinstance(resp, dict):
                    continue
                with self.lock:
                    rec = self.nodes.get(str(peer.get("id")))
                    if rec is not None:
                        rec["last_seen"] = _iso()
                        rec["last_seen_epoch"] = _now()
                    leader = resp.get("leader")
                    direct = (isinstance(leader, dict)
                              and str(leader.get("id")) == str(peer.get("id")))
                    self._merge_snapshot_locked(resp, from_leader=direct)
                    self._mark_dirty_locked()

    def _loop_maintenance(self) -> None:
        while not self._stop.wait(0.2):
            try:
                self._maintenance_once()
            except Exception as exc:  # noqa: BLE001
                _log(f"维护循环异常（继续）：{exc!r}")

    def _maintenance_once(self) -> None:
        need_election = False
        with self.lock:
            now_epoch = _now()
            for nid, rec in list(self.nodes.items()):
                status = self._node_status_locked(rec, now_epoch)
                old = rec.get("_status")
                if old != status:
                    rec["_status"] = status
                    if status == "dead" and old is not None:
                        self._emit_locked("node_down", node_id=nid,
                                          message=f"{rec.get('name') or nid} 失联")
                    elif status == "alive" and old in ("suspect", "dead"):
                        self._emit_locked("node_up", node_id=nid,
                                          message=f"{rec.get('name') or nid} 恢复")
            if self.role == "leader":
                self.leader_lease_until = now_epoch + self.config.leader_lease
            elif self.leader_id:
                if self.leader_lease_until and now_epoch > self.leader_lease_until:
                    self._clear_leader_locked("主节点租约过期")
            if not self.leader_id and self.role != "leader":
                if self._leader_lost_at is None:
                    self._leader_lost_at = now_epoch
                delay = self._election_delay_locked()
                if now_epoch - self._leader_lost_at >= delay:
                    need_election = True
            # 租约过期只报告一次，不主动改 git；真正的回收由 hive watchdog 决定
            for tid, lease in list(self.leases.items()):
                if lease.get("released"):
                    continue
                if float(lease.get("lease_until_epoch") or 0.0) < now_epoch:
                    key = f"{tid}:{lease.get('gen')}"
                    if key not in self._expired_reported:
                        self._expired_reported.add(key)
                        self._emit_locked(
                            "lease_expired", task=tid,
                            message=f"{lease.get('agent') or '-'} 的租约已过期")
        # 落盘不在锁内做（_persist 自己加锁），并且按 1s 节流；维护循环 5Hz，
        # 若每轮都写 state.json 会把磁盘打满。
        self._persist()
        # 长期不变式：启动后新增的卡也要进 store 并让所有副本追平。
        self._maybe_reconcile_store_cards()
        if need_election:
            self._start_election()

    def _loop_discovery(self) -> None:
        while not self._stop.wait(max(0.2, self.config.discovery_interval)):
            try:
                with self.lock:
                    need = not self.leader_id
                if need:
                    self._discover_and_join(timeout=0.7)
            except Exception as exc:  # noqa: BLE001
                _log(f"发现循环异常（继续）：{exc!r}")

    # -- 发现 / 注册 -------------------------------------------------------- #
    def discovery_reply(self) -> dict:
        with self.lock:
            return {
                "hive": "announce",
                "proto": PROTOCOL,
                "cluster": self.config.cluster_id,
                "node": self._self_node(),
                "url": self.url,
                "term": self.term,
                "role": self.role,
                "leader": self._leader_public_locked(),
                "port": self.port,
            }

    def _bootstrap(self) -> None:
        try:
            joined = self._discover_and_join(timeout=0.9)
        except Exception as exc:  # noqa: BLE001
            _log(f"启动发现失败（继续）：{exc!r}")
            joined = False
        if not joined:
            with self.lock:
                if not self.leader_id:
                    # 一台新机器单独启动时立刻具备当选条件；有 seed 但还没握手的，
                    # 给发现/注册留出 election_min 的窗口。
                    alive_peers = [n for nid, n in self.nodes.items()
                                   if nid != self.node_id
                                   and self._node_status_locked(n) == "alive"]
                    if alive_peers:
                        self._leader_lost_at = _now()
                    elif self.config.seeds or self.config.discover:
                        # 配了 seed / 开着广播：先给注册和发现留一个选举超时的窗口，
                        # 别让「刚启动、还没握手」的节点先把自己选成主节点，回头
                        # 再拿更高 term 去顶掉已有的主节点。
                        self._leader_lost_at = _now()
                    else:
                        # 真正的单机 / 明确关发现 / 无 seed：立即具备当选条件。
                        self._leader_lost_at = _now() - self.config.election_min - 1
                    self._mark_dirty_locked()

    def _discover_and_join(self, timeout: float = 0.8) -> bool:
        announces = []
        if self.discovery is not None:
            announce = self.discovery.discover(timeout)
            announces.extend(announce or [])
        # 广播的 announce 先合并，再尝试注册到 leader
        for a in announces:
            node = a.get("node") if isinstance(a.get("node"), dict) else None
            if node:
                with self.lock:
                    self._update_node_locked(node, touch=True)
            leader = a.get("leader")
            if isinstance(leader, dict) and leader.get("url"):
                if self._register_to(str(leader["url"])):
                    return True
            url = a.get("url") or (normalize_url(node["url"]) if node and node.get("url") else "")
            if url and self._register_to(str(url)):
                return True
        for seed in self.config.seeds:
            if self._register_to(seed):
                return True
        return False

    def _register_to(self, url: str) -> bool:
        base = normalize_url(url, self.config.port)
        if not base:
            return False
        base = base.rstrip("/")
        code, resp = http_json(base + "/api/cluster/register", "POST",
                               {"node": self._self_node()}, 1.2,
                               self.config.token)
        if code == 200 and isinstance(resp, dict) and resp.get("ok"):
            with self.lock:
                state = resp.get("state")
                if isinstance(state, dict):
                    self._merge_snapshot_locked(state, from_leader=True)
                leader = (state or {}).get("leader") if isinstance(state, dict) else None
                if isinstance(leader, dict):
                    self._adopt_leader_locked(leader, int((state or {}).get("term") or 0))
                self._mark_dirty_locked()
            _log(f"已注册到主节点 {base}")
            if self.store is not None:
                threading.Thread(target=self._store_catch_up, args=(base,),
                                 name="hive-store-catchup", daemon=True).start()
            return True
        # 409：对方知道主节点，重试一次
        if isinstance(resp, dict):
            leader = resp.get("leader")
            if isinstance(leader, dict) and leader.get("url"):
                alt = normalize_url(str(leader["url"]), self.config.port).rstrip("/")
                if alt and alt != base:
                    code2, resp2 = http_json(
                        alt + "/api/cluster/register", "POST",
                        {"node": self._self_node()}, 1.2, self.config.token)
                    if code2 == 200 and isinstance(resp2, dict) and resp2.get("ok"):
                        with self.lock:
                            self._merge_snapshot_locked(
                                resp2.get("state") or {}, from_leader=True)
                            self._mark_dirty_locked()
                        _log(f"已注册到主节点 {alt}")
                        if self.store is not None:
                            threading.Thread(target=self._store_catch_up,
                                             args=(alt,), name="hive-store-catchup",
                                             daemon=True).start()
                        return True
            node = resp.get("node")
            if isinstance(node, dict):
                with self.lock:
                    self._update_node_locked(node, touch=True)
        return False

    # -- 优雅退出 / 主节点交接 --------------------------------------------- #
    def _resign_peers(self, reason: str) -> None:
        with self.lock:
            peers = [dict(rec) for nid, rec in self.nodes.items()
                     if nid != self.node_id and rec.get("url")
                     and self._node_status_locked(rec) in ("alive", "suspect")]
            body = {"node": self._self_node(), "term": self.term,
                    "node_id": self.node_id, "reason": reason}
        for peer in peers:
            try:
                http_json(str(peer["url"]).rstrip("/") + "/api/cluster/resign",
                          "POST", body, 0.5, self.config.token)
            except Exception:  # noqa: BLE001
                pass

    # -- 服务端处理 -------------------------------------------------------- #
    def handle_register(self, body: dict) -> tuple[int, dict]:
        node = body.get("node") if isinstance(body, dict) else None
        if not isinstance(node, dict) or not node.get("id"):
            return 400, {"ok": False, "error": "register 需要 node.id"}
        with self.lock:
            if self.role != "leader":
                return 409, {"ok": False, "leader": self._leader_public_locked(),
                             "term": self.term, "node": self._self_node()}
            known = self.nodes.get(str(node["id"]))
            self._update_node_locked(node, touch=True, joined=known is None)
            if known is None:
                self._emit_locked("node_registered",
                                  message=f"{node.get('name') or node['id']} 注册")
            self._persist_locked()
            return 200, {"ok": True, "state": self._snapshot_locked()}

    def handle_heartbeat(self, body: dict) -> tuple[int, dict]:
        node = body.get("node") if isinstance(body, dict) else None
        term = int(body.get("term") or 0) if isinstance(body, dict) else 0
        with self.lock:
            if isinstance(node, dict) and node.get("id"):
                self._update_node_locked(node, touch=True)
            if term > self.term:
                self._adopt_higher_term_locked(term, body.get("leader"))
            if body.get("leader") and isinstance(node, dict):
                self._adopt_leader_locked(node, max(term, self.term))
            self._merge_body_state_locked(body)
            return 200, {
                "ok": True,
                "term": self.term,
                "role": self.role,
                "leader": self._leader_public_locked(),
                "leader_lease_until": _iso(self.leader_lease_until)
                if self.leader_lease_until else "",
                "nodes": [self._public_node_locked(n) for n in self.nodes.values()],
                "events": copy.deepcopy(list(self.events))[-120:],
                "progress": copy.deepcopy(self.progress),
                "leases": copy.deepcopy(self.leases),
                "generation": self.generation,
                "store_commit_seq": (self.store.commit_seq()
                                     if self.store is not None else 0),
            }

    def handle_vote(self, body: dict) -> tuple[int, dict]:
        term = int(body.get("term") or 0)
        cand = body.get("candidate") if isinstance(body.get("candidate"), dict) else {}
        cid = str(cand.get("id") or "")
        if not cid:
            return 400, {"ok": False, "granted": False, "error": "缺少 candidate.id"}
        if not self.config.electable:
            return 200, {"ok": True, "granted": False, "term": self.term,
                         "reason": "observer"}
        if cand.get("electable") is False:
            return 200, {"ok": True, "granted": False, "term": self.term,
                         "reason": "candidate_not_electable"}
        # 0 = 放弃 DR/BDR 竞选资格，是硬资格。原先只靠候选人自己 delay=inf
        # 「自觉不发起」，集群侧没有防线：跑老代码或配错的节点一抢跑，照样能
        # 拿到多数票当上主控。本机自己是正优先级，就说明这轮该由正优先级
        # 节点主导，不能把票投给一个声明弃权的候选人。
        # 本机也是 0 时（全员弃权的退化路径）不拦，否则谁都选不出来。
        if (normalize_leader_priority(cand.get("priority")) <= 0
                and self.config.leader_priority > 0):
            return 200, {"ok": True, "granted": False, "term": self.term,
                         "reason": "candidate_priority_zero"}
        my_seq = self.store.commit_seq() if self.store else 0
        cand_seq = int(cand.get("last_store_seq") or 0)
        # 人工夺权通道：body 里带 force 才放行（且本机没禁用）。
        # 这道闸原本是防止选出数据落后的主节点，但 hive 的 store 只是缓存、
        # git 才是事实账（见 StoreReconciler），落后能在当上主控后从卡面补回来。
        # 保留开关（HIVE_CLUSTER_ALLOW_FORCE_PROMOTE=0 可关掉）是为了让集群
        # 管理员能选择「宁可没主控，也不接受落后节点」这种更严的策略。
        forced = bool(body.get("force")) and self.config.allow_forced_promote
        behind = cand_seq < my_seq
        if behind and not forced and self.config.strict_store_freshness:
            # 严格模式（默认关闭，见 Config.strict_store_freshness）：管理员显式
            # 选择「宁可没有主控，也不接受 store 落后的主」。命中必须留下可追的
            # 原因 —— 否则日志里只剩「未获多数」，没人分得清是网络不通还是闸门
            # 拒票（T-0295）。
            with self.lock:
                self._emit_locked(
                    "vote_store_behind",
                    node_id=cid,
                    message=(f"严格模式拒票：{cand.get('name') or cid} 的 store 落后 "
                             f"{my_seq - cand_seq} 条（{cand_seq}/{my_seq}）"),
                    data={"candidate": cid, "candidate_seq": cand_seq,
                          "my_seq": my_seq, "lag": my_seq - cand_seq,
                          "strict": True})
                self._persist_locked()
            return 200, {"ok": True, "granted": False, "term": self.term,
                         "reason": "store_behind",
                         "candidate_seq": cand_seq, "my_seq": my_seq,
                         "strict": True}
        with self.lock:
            if term < self.term:
                return 200, {"ok": True, "granted": False, "term": self.term,
                             "leader": self._leader_public_locked(),
                             "reason": "stale_term"}
            if term > self.term:
                self._adopt_higher_term_locked(term)
            known = self.nodes.get(cid)
            if known and int(cand.get("incarnation") or 0) < int(known.get("incarnation") or 0):
                return 200, {"ok": True, "granted": False, "term": self.term,
                             "reason": "stale_incarnation"}
            if self.voted_for not in (None, cid):
                return 200, {"ok": True, "granted": False, "term": self.term,
                             "voted_for": self.voted_for, "leader": self._leader_public_locked()}
            if behind and forced:
                # 明知候选人 store 落后也投票，必须留痕：否则事后没人能分辨
                # 这次任期切换是「按规矩选的」还是「人硬点的」。
                self._emit_locked(
                    "leader_forced",
                    message=(f"人工夺权：{cand.get('name') or cid} 的 store 落后 "
                             f"{my_seq - cand_seq} 条（{cand_seq}/{my_seq}），仍投赞成票"))
                _log(f"接受人工夺权候选 {cand.get('name') or cid}："
                     f"store {cand_seq} < {my_seq}，按 force 放行")
            elif behind:
                # 默认行为：store 只是缓存，落后不该被提升为「选举资格」。
                # 分区时各节点 commit_seq 必然不一致，硬拒票会让全员互相否决，
                # 只能等 dead_timeout 把同伴判死、quorum 退化成 1 才「孤身称主」
                # （T-0295）。这里放行，但把落后量记成事件，并把本机 commit_seq
                # 回给候选人 —— 它当选后要先把 store 追到这个位点再服务写。
                self._emit_locked(
                    "vote_store_lag",
                    node_id=cid,
                    message=(f"投赞成票：{cand.get('name') or cid} 的 store 落后 "
                             f"{my_seq - cand_seq} 条（{cand_seq}/{my_seq}），"
                             f"当选后需先 reconcile 到 {my_seq}"),
                    data={"candidate": cid, "candidate_seq": cand_seq,
                          "my_seq": my_seq, "lag": my_seq - cand_seq})
                _log(f"接受落后候选 {cand.get('name') or cid}："
                     f"store {cand_seq} < {my_seq}，留痕 vote_store_lag")
            self.voted_for = cid
            # 投完票先别急着自己也参选：给对方一个把 leader 心跳发过来的窗口。
            self._leader_lost_at = _now()
            self._update_node_locked(cand, touch=True)
            self._mark_dirty_locked()
            self._persist_locked()
            return 200, {"ok": True, "granted": True, "term": self.term,
                         "leader": self._leader_public_locked(),
                         "store_seq": my_seq}

    def handle_resign(self, body: dict) -> tuple[int, dict]:
        node_id = str(body.get("node_id") or (body.get("node") or {}).get("id") or "")
        with self.lock:
            if node_id and node_id == self.leader_id:
                self._clear_leader_locked("主节点主动退出")
                self._persist_locked()
            if node_id and node_id == self.node_id and self.role == "leader":
                self.role = "follower"
                self.leader_id = None
                self.leader_url = None
                self.leader_lease_until = 0.0
                self._emit_locked("leader_step_down", message="resign")
                self._persist_locked()
            return 200, {"ok": True, "term": self.term}

    def handle_promote(self, body: dict) -> tuple[int, dict]:
        """人工把**本机**提升为主控。

        和自动选主的区别，正是「人说了算」需要的那几点：
          * 不看选举延迟、不看 _last_election 节流 —— 点了就该马上动；
          * 允许在 store 落后时当选（force），因为 store 只是缓存、git 才是事实账，
            落后会在当上主控后由 StoreReconciler 从卡面补回来；
          * 先请现任主控主动下台，把「双主」窗口压到最小 —— 它自己降级，
            比等它被更高任期压下去干净。
        """
        force = bool(body.get("force", True))
        if not self.config.electable:
            return 409, {"ok": False, "role": self.role, "node": self.node_id,
                         "error": "本机 electable=false（observer），不能当主控"}

        with self.lock:
            if self.role == "leader" and self.leader_lease_until > _now():
                return 200, {"ok": True, "already": True, "role": "leader",
                             "term": self.term, "node": self.node_id,
                             "previous_leader": self.node_id}
            # 人工 promote 也不越过 OSPF 的硬资格。提前拦在这里，比让选举
            # 发起后被同伴拒票（term 白涨一轮、还留下半截状态）好排查。
            if (self.config.leader_priority <= 0
                    and self._priority_zero_excludes_locked()):
                return 409, {"ok": False, "role": self.role, "node": self.node_id,
                             "error": "本机 priority=0（OSPF 语义：放弃主控竞选资格）。"
                                      "先执行 hive cluster priority --priority 1 "
                                      "把优先级调回 1..255，再 promote"}
            leader_url = self.leader_url
            leader_id = self.leader_id or ""
            me = self.node_id

        resigned = False
        if leader_url and leader_id and leader_id != me:
            code, _ = http_json(str(leader_url).rstrip("/") + "/api/cluster/resign",
                                "POST", {"node_id": leader_id}, 1.5, self.config.token)
            resigned = code == 200

        became = self._start_election(force=force)
        with self.lock:
            return (200 if became else 409), {
                "ok": became, "role": self.role, "term": self.term, "node": me,
                "resigned_previous": resigned, "previous_leader": leader_id,
                "error": "" if became else
                "选举未获多数（peers 不在线，或有节点投了反对票）",
            }

    def handle_leave(self, body: dict) -> tuple[int, dict]:
        node_id = str(body.get("node_id") or (body.get("node") or {}).get("id") or "")
        with self.lock:
            rec = self.nodes.pop(node_id, None) if node_id else None
            if rec is not None:
                self._emit_locked("node_leave", node_id=node_id,
                                  message=f"{rec.get('name') or node_id} 退出")
                if node_id == self.leader_id:
                    self._clear_leader_locked("主节点退出")
                self._mark_dirty_locked()
                self._persist_locked()
            return 200, {"ok": True, "removed": bool(rec)}

    def handle_push(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        term = int(body.get("term") or 0)
        with self.lock:
            if term and term < self.term:
                return 409, {"ok": False, "stale": True, "term": self.term,
                             "leader": self._leader_public_locked()}
            try:
                item = self._apply_progress_locked(body, leader=(self.role == "leader"))
            except ClusterError as exc:
                return 400, {"ok": False, "error": str(exc)}
            leader_url = self.leader_url
            role = self.role
            self._persist_locked()
        if role != "leader" and leader_url and not body.get("forwarded"):
            fwd = dict(body)
            fwd["forwarded"] = True
            fwd["term"] = item["term"]
            code, resp = http_json(str(leader_url).rstrip("/") + "/api/cluster/push",
                                   "POST", fwd, 0.8, self.config.token)
            if code == 200 and isinstance(resp, dict):
                with self.lock:
                    self._merge_snapshot_locked(resp)
                    self._mark_dirty_locked()
                return 200, {"ok": True, "forwarded": True, "item": item,
                             "leader": self._leader_public_locked()}
        return 200, {"ok": True, "forwarded": False, "item": item,
                     "leader": self._leader_public_locked(), "term": self.term}

    def handle_lease(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        term = int(body.get("term") or 0)
        with self.lock:
            if term and term < self.term:
                return 409, {"ok": False, "stale": True, "term": self.term,
                             "leader": self._leader_public_locked()}
            try:
                item = self._apply_lease_locked(body, leader=(self.role == "leader"))
            except ClusterError as exc:
                return 400, {"ok": False, "error": str(exc)}
            leader_url = self.leader_url
            role = self.role
            self._persist_locked()
        if role != "leader" and leader_url and not body.get("forwarded"):
            fwd = dict(body)
            fwd["forwarded"] = True
            fwd["term"] = item["term"]
            code, resp = http_json(str(leader_url).rstrip("/") + "/api/cluster/lease",
                                   "POST", fwd, 0.8, self.config.token)
            if code == 200 and isinstance(resp, dict):
                with self.lock:
                    self._merge_lease_locked(item["task"], resp.get("item") or {})
                    self._mark_dirty_locked()
                return 200, {"ok": True, "forwarded": True, "item": item}
        return 200, {"ok": True, "forwarded": False, "item": item}

    def handle_release(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        tid = str(body.get("task") or "").strip()
        if not tid:
            return 400, {"ok": False, "error": "release 需要 task"}
        payload = dict(body)
        payload["released"] = True
        payload["lease_until"] = _iso(_now())
        payload["lease_until_epoch"] = _now()
        return self.handle_lease(payload)

    def handle_event(self, body: dict) -> tuple[int, dict]:
        """控制面事件（P0：repo.updated）。leader 落事件并广播；follower 转发。"""
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        etype = str(body.get("type") or "").strip()
        if etype != "repo.updated":
            return 400, {"ok": False, "error": f"不支持的事件类型 {etype!r}"}
        task = str(body.get("task") or "")
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        term = int(body.get("term") or 0)
        with self.lock:
            if term and term < self.term:
                return 409, {"ok": False, "stale": True, "term": self.term,
                             "leader": self._leader_public_locked()}
            role = self.role
            leader_url = self.leader_url
            forward_term = self.term
        if role != "leader" and leader_url and not body.get("forwarded"):
            fwd = dict(body)
            fwd["forwarded"] = True
            fwd["term"] = forward_term
            code, resp = http_json(str(leader_url).rstrip("/") + "/api/cluster/event",
                                   "POST", fwd, 1.0, self.config.token)
            if code == 200 and isinstance(resp, dict):
                ev = resp.get("event")
                if isinstance(ev, dict):
                    with self.lock:
                        self._merge_event_locked(ev)
                        self._mark_dirty_locked()
                return 200, {"ok": True, "forwarded": True, "event": ev}
        with self.lock:
            ev = self._emit_locked(etype, task=task, data=data,
                                   message=f"repo.updated {task}".strip())
            self._persist_locked()
        return 200, {"ok": True, "forwarded": False, "event": ev}

    # -- P2 store：leader 单写 + 同步复制 ---------------------------------- #
    def _store_op_for(self, action: str, body: dict) -> dict:
        if self.store is None:
            raise ClusterError("本节点没有启用 store")
        from hive_store import StoreError
        term = int(body.get("term") or self.term)
        tid = str(body.get("task") or body.get("task_id") or "")
        agent = str(body.get("agent") or "")
        op_id = str(body.get("op_id") or "")
        try:
            if action == "claim":
                return self.store.claim(tid, agent, term,
                                        int(body.get("lease_minutes") or 10),
                                        str(body.get("host") or ""), op_id,
                                        commit=False)
            if action == "lease":
                return self.store.renew(tid, agent, str(body.get("lease_id") or ""),
                                        term, int(body.get("minutes") or 10), op_id,
                                        commit=False)
            if action == "release":
                return self.store.release(tid, agent, str(body.get("lease_id") or ""),
                                          term, bool(body.get("requeue", True)), op_id,
                                          commit=False)
            if action == "result":
                return self.store.result(tid, agent, str(body.get("lease_id") or ""),
                                         term, dict(body.get("payload") or {}),
                                         str(body.get("status") or "done"), op_id,
                                         commit=False)
            if action == "assign":
                return self.store.assign(tid, agent, term, op_id, commit=False)
            if action == "ingest":
                # follower 把本机 Git 卡面送过来，补 leader 启动后新增的卡。
                # 走 op 日志，handle_store 会把它复制到所有副本。
                card = body.get("card")
                if not isinstance(card, dict):
                    raise ClusterError("ingest 需要 card 对象", code="E_BAD_REQUEST")
                return self.store.ingest_card(tid, card, term, op_id,
                                              commit=False)
            if action == "close":
                return self.store.close(tid, bool(body.get("done", True)),
                                        str(body.get("note") or ""), term, op_id,
                                        commit=False)
            if action == "archive":
                return self.store.mark_archived(
                    tid, str(body.get("sha") or ""), op_id, term, commit=False)
            if action == "retry":
                return self.store.retry(
                    tid, agent, term, op_id, commit=False,
                    clear_result=bool(body.get("clear_result", True)))
        except StoreError as exc:
            raise ClusterError(str(exc), code=getattr(exc, "code", "")) from exc
        raise ClusterError(f"未知 store 动作 {action}", code="E_UNKNOWN_ACTION")

    def _replicate_op(self, op: dict) -> bool:
        with self.lock:
            peers = [dict(rec) for nid, rec in self.nodes.items()
                     if nid != self.node_id and rec.get("url")
                     and self._node_status_locked(rec) in ("alive", "suspect")]
            quorum = self._quorum_locked()
        ok = 1  # 自己已落盘
        if peers:
            body = {"op": op, "term": self.term}
            with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
                futs = [pool.submit(http_json,
                                    str(p["url"]).rstrip("/") + "/api/cluster/op",
                                    "POST", body, 1.5, self.config.token)
                        for p in peers]
                for fut in as_completed(futs):
                    try:
                        code, resp = fut.result()
                        if code == 200 and isinstance(resp, dict) and resp.get("ok"):
                            ok += 1
                    except Exception:  # noqa: BLE001
                        pass
        return ok >= quorum

    def _commit_op_replicated(self, seq: int) -> bool:
        """leader 本地 commit 之前，先让“多数-1”个 follower commit。"""
        with self.lock:
            peers = [dict(rec) for nid, rec in self.nodes.items()
                     if nid != self.node_id and rec.get("url")
                     and self._node_status_locked(rec) in ("alive", "suspect")]
            quorum = self._quorum_locked()
        need = max(0, quorum - 1)
        if need == 0:
            return True
        ok = 0
        if peers:
            body = {"seq": int(seq), "term": self.term}
            with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
                futs = [pool.submit(http_json,
                                    str(p["url"]).rstrip("/") + "/api/cluster/op/commit",
                                    "POST", body, 1.5, self.config.token)
                        for p in peers]
                for fut in as_completed(futs):
                    try:
                        code, resp = fut.result()
                        if code == 200 and isinstance(resp, dict) and resp.get("ok"):
                            ok += 1
                    except Exception:  # noqa: BLE001
                        pass
        return ok >= need

    def handle_store(self, action: str, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        with self.lock:
            role = self.role
            leader_url = self.leader_url
            term = self.term
        if role != "leader" and leader_url and not body.get("forwarded"):
            fwd = dict(body)
            fwd["forwarded"] = True
            fwd["term"] = term
            code, resp = http_json(
                str(leader_url).rstrip("/") + f"/api/cluster/store/{action}",
                "POST", fwd, 3.0, self.config.token)
            if isinstance(resp, dict):
                resp.setdefault("forwarded", True)
                return code, resp
            return 502, {"ok": False, "error": "转发主节点失败"}
        if role != "leader":
            return 409, {"ok": False, "error": "没有主节点", "term": self.term}
        if self.store is None:
            return 503, {"ok": False, "error": "store 未启用"}
        if not getattr(self, "_store_serving_ready", True):
            # 落后当选后的对齐窗口：读照常，写一律拒绝。返回结构化 code，
            # 让 hive.py 的 _classify_store_error 能识别成「暂时不可用」并让
            # worker 退避重试，而不是把卡当成「被别人抢走」（T-0295）。
            try:
                have = int(self.store.commit_seq() or 0)
            except Exception:  # noqa: BLE001  测试桩没有 commit_seq 也不能炸
                have = 0
            return 503, {"ok": False, "code": "E_RECONCILING",
                         "error": "主控正在对齐 store，暂不服务写请求",
                         "term": self.term,
                         "store_commit_seq": have,
                         "store_lag_target": int(getattr(
                             self, "_store_lag_target", 0) or 0)}
        try:
            out = self._store_op_for(action, body)
        except ClusterError as exc:
            resp = {"ok": False, "error": str(exc), "term": self.term}
            if getattr(exc, "code", ""):
                resp["code"] = exc.code
            return 409, resp
        seq = int(out.get("seq") or 0)
        if seq:
            ops = self.store.ops_since(seq - 1, 1)
            if ops:
                if not self._replicate_op(ops[0]):
                    return 503, {"ok": False, "code": "E_NO_QUORUM",
                                 "error": "未达到多数 prepare，写未确认",
                                 "term": self.term, **out}
            # 先让 follower commit（凑够多数-1），本地最后 commit；避免出现
            # “leader 本地已提交、但多数没有”的分叉。
            if not self._commit_op_replicated(seq):
                return 503, {"ok": False, "code": "E_NO_QUORUM_COMMIT",
                             "error": "prepare 达多数但 commit 未达多数；写未确认",
                             "term": self.term, **out}
            self.store.commit_op(seq)
        return 200, {"ok": True, "term": self.term, **out}

    def handle_store_state(self) -> tuple[int, dict]:
        if self.store is None:
            return 503, {"ok": False, "error": "store 未启用"}
        tasks = self.store.tasks()
        claims = {}
        results = {}
        for tid in tasks:
            claim = self.store.claim_info(tid)
            if claim:
                claims[tid] = claim
            res = self.store.result_info(tid)
            if res:
                results[tid] = res
        return 200, {"ok": True, "summary": self.store.summary(),
                     "tasks": tasks, "claims": claims, "results": results}

    def handle_op(self, body: dict) -> tuple[int, dict]:
        """follower 接收 leader 复制过来的 op；fsync 后返回 ok。"""
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        op = body.get("op")
        if not isinstance(op, dict) or not op.get("op_id"):
            return 400, {"ok": False, "error": "op 缺少 op_id"}
        term = int(body.get("term") or op.get("term") or 0)
        with self.lock:
            if term and term < self.term:
                return 409, {"ok": False, "stale": True, "term": self.term}
            if self.store is None:
                return 503, {"ok": False, "error": "store 未启用"}
            try:
                seq = self.store.stage_op(op)
            except Exception as exc:  # noqa: BLE001
                return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._persist_locked()
        return 200, {"ok": True, "seq": seq, "term": self.term, "staged": True}

    def handle_op_commit(self, body: dict) -> tuple[int, dict]:
        """leader 确认 op 已多数 prepare 后，follower 才把它应用到状态机。"""
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        try:
            seq = int(body.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq <= 0:
            return 400, {"ok": False, "error": "commit 需要 seq"}
        term = int(body.get("term") or 0)
        with self.lock:
            if term and term < self.term:
                return 409, {"ok": False, "stale": True, "term": self.term}
            if self.store is None:
                return 503, {"ok": False, "error": "store 未启用"}
            try:
                self.store.commit_op(seq)
            except Exception as exc:  # noqa: BLE001
                return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._persist_locked()
        return 200, {"ok": True, "seq": seq, "term": self.term}

    def _broadcast_truncate(self) -> None:
        with self.lock:
            peers = [dict(rec) for nid, rec in self.nodes.items()
                     if nid != self.node_id and rec.get("url")]
            term = self.term
        for peer in peers:
            try:
                http_json(str(peer["url"]).rstrip("/") + "/api/cluster/op/truncate",
                          "POST", {"term": term}, 1.0, self.config.token)
            except Exception:  # noqa: BLE001
                pass

    def handle_op_truncate(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        term = int(body.get("term") or 0)
        with self.lock:
            if term and term < self.term:
                return 409, {"ok": False, "stale": True, "term": self.term}
            if self.store is None:
                return 503, {"ok": False, "error": "store 未启用"}
            seq = self.store.truncate_uncommitted()
            self._persist_locked()
        return 200, {"ok": True, "commit_seq": seq, "term": self.term}

    def handle_seed(self, body: dict) -> tuple[int, dict]:
        raw = str(body.get("url") or body.get("seed") or "").strip()
        if not raw:
            return 400, {"ok": False, "error": "seed 需要 url"}
        url = normalize_url(raw, self.config.port)
        with self.lock:
            if url and url not in self.config.seeds:
                self.config.seeds.append(url)
                self.config.save()
        ok = self._register_to(url)
        if not ok:
            with self.lock:
                self._emit_locked("seed_unreachable", message=f"seed {url} 暂不可达")
        return 200, {"ok": True, "joined": bool(ok), "seed": url}

    def handle_discover_trigger(self, body: dict) -> tuple[int, dict]:
        ok = self._discover_and_join(timeout=0.9)
        return 200, {"ok": True, "joined": bool(ok), "state": self.snapshot()
                     if ok else {"leader": self._leader_public_locked(),
                                 "term": self.term}}

    def handle_wait(self, query: dict) -> dict:
        try:
            since = int((query.get("since") or ["0"])[0])
        except (TypeError, ValueError):
            since = 0
        try:
            timeout = min(30.0, max(0.1, float((query.get("timeout") or ["20"])[0])))
        except (TypeError, ValueError):
            timeout = 20.0
        deadline = _now() + timeout
        with self.cond:
            while self.generation <= since and not self._stop.is_set():
                left = deadline - _now()
                if left <= 0:
                    break
                self.cond.wait(left)
            return self._snapshot_locked()

    # -- 供命令行/测试使用 -------------------------------------------------- #
    def publish_progress(self, payload: dict) -> dict:
        code, body = self.handle_push(dict(payload))
        if code != 200 or not isinstance(body, dict):
            raise ClusterError(str((body or {}).get("error") or f"HTTP {code}"))
        return body

    def publish_lease(self, payload: dict) -> dict:
        code, body = self.handle_lease(dict(payload))
        if code != 200 or not isinstance(body, dict):
            raise ClusterError(str((body or {}).get("error") or f"HTTP {code}"))
        return body

    def release_lease(self, tid: str) -> dict:
        code, body = self.handle_release({"task": tid})
        if code != 200 or not isinstance(body, dict):
            raise ClusterError(str((body or {}).get("error") or f"HTTP {code}"))
        return body

    def publish_event(self, etype: str, task: str = "", data: dict | None = None) -> dict:
        code, body = self.handle_event({"type": etype, "task": task,
                                        "data": data or {}})
        if code != 200 or not isinstance(body, dict):
            raise ClusterError(str((body or {}).get("error") or f"HTTP {code}"))
        return body


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #

def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def build_handler(cluster: Cluster):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"hive-cluster/{CLUSTER_VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            code = getattr(self, "_last_code", 200)
            if code >= 400:
                sys.stderr.write("[hive-cluster] %s\n" % (fmt % args))

        def handle(self):
            # 客户端（探活、被 stop 掉的旧连接、浏览器刷新）提前断开时，
            # readline/recv_into 会抛 ConnectionResetError(WinError 10054)。
            # BaseHTTPRequestHandler 默认让 socketserver 把整段 traceback 打进
            # stderr —— 服务完全没事，但日志尾部会堆满这种噪音，一旦别处报
            # 「启动未就绪」就会被误读成崩溃原因。这里就地吞掉连接重置。
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                self.close_connection = True

        def _send(self, code, payload):
            self._last_code = code
            body = _json_bytes(payload)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Hive-Cluster-Term", str(cluster.term))
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _authorized(self) -> bool:
            token = cluster.config.token
            if not token:
                return True
            supplied = (self.headers.get("X-Hive-Cluster-Token") or "").strip()
            if not supplied:
                auth = (self.headers.get("Authorization") or "").strip()
                if auth.lower().startswith("bearer "):
                    supplied = auth[7:].strip()
            return bool(supplied) and hmac.compare_digest(
                supplied.encode("utf-8"), token.encode("utf-8"))

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("顶层必须是对象")
            return data

        def _route_get(self, path, query):
            if path == "/api/cluster/health":
                snap = cluster.snapshot()
                me = snap.get("self") or {}
                leader = snap.get("leader") or {}
                # health 是免 token 的：只给最小身份信息，不泄露 agents/本地路径。
                self._send(200, {
                    "ok": True,
                    "node": {"id": me.get("id"), "name": me.get("name"),
                             "url": me.get("url")},
                    "role": snap["role"], "term": snap["term"],
                    "leader": ({"id": leader.get("id"), "name": leader.get("name"),
                                "url": leader.get("url")} if leader else None),
                    "cluster_id": snap["cluster_id"], "protocol": PROTOCOL,
                    "uptime_s": round(_now() - cluster.started_epoch, 1),
                    "peers": snap["alive"], "quorum": snap["quorum"],
                    "mcp": (cluster.mcp_service.status()
                            if cluster.mcp_service is not None else {"enabled": False}),
                    "store": (cluster.store.summary()
                              if cluster.store is not None else {"enabled": False}),
                    "archive": (cluster.archive_agent.status()
                                if cluster.archive_agent is not None else {"running": False}),
                    "coordinator": (cluster.coordinator_agent.status()
                                    if cluster.coordinator_agent is not None
                                    else {"enabled": False}),
                })
                return
            if not self._authorized():
                self._send(401, {"ok": False, "error": "token 不匹配"})
                return
            if path == "/api/cluster/state":
                self._send(200, {"ok": True, "state": cluster.snapshot()})
                return
            if path == "/api/cluster/task-status":
                snap = cluster.snapshot()
                self._send(200, {"ok": True, "leader": snap["leader"],
                                 "term": snap["term"],
                                 "progress": snap["progress"],
                                 "leases": snap["leases"]})
                return
            if path == "/api/cluster/events":
                try:
                    since = int((query.get("since") or ["0"])[0])
                except (TypeError, ValueError):
                    since = 0
                snap = cluster.snapshot()
                events = snap["events"]
                if since:
                    events = [e for e in events if int(e.get("seq") or 0) > since]
                self._send(200, {"ok": True, "events": events, "generation": snap["generation"]})
                return
            if path == "/api/cluster/chat":
                q = query
                try:
                    since = float((q.get("since") or ["0"])[0] or 0)
                except (TypeError, ValueError):
                    since = 0.0
                try:
                    limit = int((q.get("limit") or ["200"])[0] or 200)
                except (TypeError, ValueError):
                    limit = 200
                scope = (q.get("scope") or [""])[0]
                msgs = cluster.chat_list(since=since, scope=scope, limit=limit)
                meta = cluster.chat_meta()
                self._send(200, {"ok": True, "messages": msgs,
                                 "last_epoch": meta["last_epoch"],
                                 "total": meta["count"], "count": len(msgs)})
                return
            if path == "/api/cluster/wait":
                self._send(200, {"ok": True, "state": cluster.handle_wait(query)})
                return
            if path == "/api/cluster/sync":
                agent = cluster.sync_agent
                self._send(200, {"ok": True,
                                 "sync": agent.status() if agent else {"enabled": False}})
                return
            if path == "/api/cluster/store/state":
                code, payload = cluster.handle_store_state()
                self._send(code, payload)
                return
            if path == "/api/cluster/store/snapshot":
                if cluster.store is None:
                    self._send(503, {"ok": False, "error": "store 未启用"})
                    return
                self._send(200, {"ok": True, "snapshot": cluster.store.snapshot()})
                return
            if path == "/api/cluster/op/since":
                try:
                    since = int((query.get("since") or ["0"])[0])
                except (TypeError, ValueError):
                    since = 0
                if cluster.store is None:
                    self._send(503, {"ok": False, "error": "store 未启用"})
                    return
                if since < cluster.store.compacted_through():
                    self._send(409, {"ok": False, "snapshot_required": True,
                                     "compacted_through": cluster.store.compacted_through(),
                                     "commit_seq": cluster.store.commit_seq()})
                    return
                ops = [op for op in cluster.store.ops_since(since)
                       if op.get("committed")]
                self._send(200, {"ok": True, "ops": ops,
                                 "commit_seq": cluster.store.commit_seq()})
                return
            if path == "/api/cluster/config":
                cfg = cluster.config
                self._send(200, {"ok": True, "cluster_id": cfg.cluster_id,
                                 "node": cluster.node_id, "name": cfg.name,
                                 "port": cluster.port, "discovery": cfg.discover,
                                 "discovery_port": cfg.discovery_port,
                                 "seeds": list(cfg.seeds)})
                return
            self._send(404, {"ok": False, "error": f"未知资源 {path}"})

        def _route_post(self, path):
            if not self._authorized():
                self._send(401, {"ok": False, "error": "token 不匹配"})
                return
            try:
                body = self._read_body()
            except (ValueError, UnicodeDecodeError) as exc:
                self._send(400, {"ok": False, "error": f"请求体不合法：{exc}"})
                return
            try:
                if path == "/api/cluster/register":
                    code, payload = cluster.handle_register(body)
                elif path == "/api/cluster/heartbeat":
                    code, payload = cluster.handle_heartbeat(body)
                elif path == "/api/cluster/vote":
                    code, payload = cluster.handle_vote(body)
                elif path == "/api/cluster/resign":
                    code, payload = cluster.handle_resign(body)
                elif path == "/api/cluster/promote":
                    code, payload = cluster.handle_promote(body)
                elif path == "/api/cluster/leave":
                    code, payload = cluster.handle_leave(body)
                elif path == "/api/cluster/push":
                    code, payload = cluster.handle_push(body)
                elif path == "/api/cluster/lease":
                    code, payload = cluster.handle_lease(body)
                elif path == "/api/cluster/release":
                    code, payload = cluster.handle_release(body)
                elif path == "/api/cluster/event":
                    code, payload = cluster.handle_event(body)
                elif path == "/api/cluster/chat":
                    code, payload = cluster.handle_chat_post(body)
                elif path == "/api/cluster/op":
                    code, payload = cluster.handle_op(body)
                elif path == "/api/cluster/op/commit":
                    code, payload = cluster.handle_op_commit(body)
                elif path == "/api/cluster/op/truncate":
                    code, payload = cluster.handle_op_truncate(body)
                elif path.startswith("/api/cluster/store/"):
                    code, payload = cluster.handle_store(
                        path.rsplit("/", 1)[-1], body)
                elif path == "/api/cluster/seed":
                    code, payload = cluster.handle_seed(body)
                elif path == "/api/cluster/discover":
                    code, payload = cluster.handle_discover_trigger(body)
                else:
                    code, payload = 404, {"ok": False, "error": f"未知资源 {path}"}
            except Exception as exc:  # noqa: BLE001  服务端不能因一个请求崩掉
                code, payload = 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send(code, payload)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            self._route_get(parsed.path, parse_qs(parsed.query))

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            self._route_post(parsed.path)

    return Handler


# --------------------------------------------------------------------------- #
# 命令行：serve / start / stop / status / peers / events / join / leave / watch
# --------------------------------------------------------------------------- #

def _config_from_args(args) -> Config:
    prio = getattr(args, "priority", None)
    if prio is not None:
        try:
            n = int(prio)
        except (TypeError, ValueError):
            n = -1
        if not (MIN_LEADER_PRIORITY <= n <= MAX_LEADER_PRIORITY):
            raise ClusterError(
                f"--priority 必须在 {MIN_LEADER_PRIORITY}..{MAX_LEADER_PRIORITY} 之间"
                f"（OSPF DR priority 是 8bit：0=放弃资格，1=默认，值大者优先）：{prio}")
    return Config(
        state_dir=getattr(args, "state_dir", None),
        name=getattr(args, "name", None),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        advertise=getattr(args, "advertise", None),
        discovery_port=getattr(args, "discovery_port", None),
        discover=(False if getattr(args, "no_discover", False) else None),
        seeds=getattr(args, "seed", None),
        token=getattr(args, "token", None),
        lease_grace=getattr(args, "lease_grace", None),
        electable=(False if getattr(args, "no_elect", False) else None),
        sync_enabled=(False if getattr(args, "no_sync", False) else None),
        mcp_enabled=(
            True if getattr(args, "mcp", False)
            else (False if getattr(args, "no_mcp", False)
                  else (True if getattr(args, "command", "") == "daemon" else None))),
        mcp_port=getattr(args, "mcp_port", None),
        mcp_bind=getattr(args, "mcp_bind", None),
        store_enabled=(True if getattr(args, "store", False)
                       else (False if getattr(args, "no_store", False) else None)),
        claim_backend=getattr(args, "claim_backend", None),
        coordinator_enabled=(True if getattr(args, "coordinator", False)
                             else None),
        leader_priority=prio,
        hive_dir=getattr(args, "dir", None),
    )


def _health_timeout() -> float:
    """健康探测的超时。

    原值 0.45s 太紧：节点一旦在忙（store 追平上百张卡、git 同步、GC），
    一次 health round-trip 很容易超过它，调用方就误判成「集群没起来」。
    ensure_background 拿到这个误判后会在端口已被占用的情况下再 spawn 一个，
    新进程抢不到端口、又去抢同一个 sqlite —— 于是「database is locked」
    + 端口冲突 + 更慢 + 再 spawn，雪崩。
    连接被拒（真没起）在本地是立即返回的，所以放宽超时不会拖慢「确实没起」的判定。
    """
    return _env_float("HIVE_CLUSTER_HEALTH_TIMEOUT", 3.0)


def _startup_timeout() -> float:
    """`_spawn_background` 等待新进程 health 就绪的窗口（秒）。

    Windows 上默认给 30s：Microsoft Store 版 Python 冷启动 + 打开 store
    （上百张卡）+ 起 MCP / UDP 发现线程，实测会超过原先写死的 8s。
    `HIVE_CLUSTER_STARTUP_TIMEOUT` 可覆盖。
    """
    return _env_float("HIVE_CLUSTER_STARTUP_TIMEOUT",
                      30.0 if os.name == "nt" else 15.0)


def _local_health(config: Config, timeout: float | None = None):
    code, body = http_json(f"http://127.0.0.1:{config.port}/api/cluster/health",
                           timeout=_health_timeout() if timeout is None else timeout)
    if code == 200 and isinstance(body, dict):
        return body
    return None


def _local_state(config: Config):
    health = _local_health(config)
    if health:
        code, body = http_json(f"http://127.0.0.1:{config.port}/api/cluster/state",
                               timeout=1.0, token=config.token)
        if code == 200 and isinstance(body, dict) and isinstance(body.get("state"), dict):
            return body["state"], True
        # 进程在跑但 token 对不上：至少用 health 给出一份可读的最小视图，
        # 不要误报成「进程未运行」。
        me = health.get("node") or {}
        leader = health.get("leader") or {}
        snap = {
            "cluster_id": health.get("cluster_id", DEFAULT_CLUSTER_ID),
            "term": health.get("term", 0), "role": health.get("role", "?"),
            "leader": leader or None,
            "self": me, "nodes": ([dict(me, status="alive", role=health.get("role"))]
                                  if me else []),
            "alive": health.get("peers", 0), "known": health.get("peers", 0),
            "quorum": health.get("quorum", 1), "progress": {}, "leases": {},
            "events": [], "generation": 0,
        }
        return snap, True
    raw = _read_json(config.state_path, None)
    if not isinstance(raw, dict):
        return None, False
    snap = raw.get("snapshot")
    return (snap if isinstance(snap, dict) else raw), False


def _dashboard_line() -> str:
    """集群状态里补一行看板地址：8787 是看板、8788 才是集群 HTTP。

    这两个端口长得几乎一样，光看「主节点地址 http://x:8788」很容易把看板也
    敲成 8788（或反过来）。实现直接复用 hive.py 的 `dashboard_status_line()`
    （端口唯一来源仍是 hive_ports），这里只做「单独跑 hive_cluster.py 时也要
    能打印」的兜底，绝不让它把状态输出带崩。
    """
    try:
        import hive
        return hive.dashboard_status_line()
    except Exception:      # noqa: BLE001 —— 兜底文案，宁可少一行也不抛
        try:
            from hive_ports import DASHBOARD_PORT
            return f"看板 http://127.0.0.1:{DASHBOARD_PORT}/  （状态未知）"
        except Exception:  # noqa: BLE001
            return "看板 -"


def _status_line(snap: dict, running: bool, stopped: bool = False) -> str:
    if not snap:
        return ("集群未运行（已手动停止）" if stopped
                else "集群未运行（没有状态文件）")
    leader = snap.get("leader") or {}
    self_node = snap.get("self") or {}
    lines = [
        f"集群 {snap.get('cluster_id', DEFAULT_CLUSTER_ID)}  本机 {self_node.get('name', '?')}"
        f"（{self_node.get('id', '?')}）  {'运行中' if running else '状态文件（进程未运行）'}"
        + ("" if running else ("  已手动停止（不再自动拉起）" if stopped else "")),
        f"角色 {snap.get('role', '?')}  任期 {snap.get('term', 0)}  "
        f"主节点 {leader.get('name') or '-'}  "
        f"在线 {snap.get('alive', 0)}/{snap.get('known', 0)}  多数门槛 {snap.get('quorum', 1)}"
        + ("" if running else "  （快照，非实时）"),
    ]
    if leader:
        lease_until = leader.get("lease_until_epoch") or 0
        left = max(0, int(float(lease_until) - _now())) if lease_until else 0
        lines.append(f"主节点地址 {leader.get('url') or '-'}  租约剩余 {left}s")
    if snap.get("lease_grace_until_epoch"):
        left = max(0, int(float(snap["lease_grace_until_epoch"]) - _now()))
        lines.append(f"租约恢复宽限期剩余 {left}s")
    lines.append("节点：")
    for n in snap.get("nodes") or []:
        lines.append(
            f"  {str(n.get('status', '?')):<7} {str(n.get('name') or n.get('id'))[:24]:<24}"
            f" {str(n.get('role') or ''):<8} term={n.get('term', 0):<3}"
            f" {n.get('url') or ''}"
            f"{'  observer' if n.get('electable') is False else ''}")
    return "\n".join(lines)


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=3,
                                 **no_window_kwargs())
            return str(pid) in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(config: Config) -> int:
    try:
        return int(config.pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _spawn_background(config: Config, args) -> int:
    """脱离当前会话拉起 `hive.py cluster serve`。"""
    health = _local_health(config)
    if health is None:
        # 进程还在、只是这一拍没响应：**绝不能**再拉一个。
        # 再拉的那个抢不到端口（Address already in use），却会打开同一个 sqlite，
        # 把正在为公司服务的节点拖进 database is locked，然后循环恶化。
        pid = _read_pid(config)
        if _pid_alive(pid):
            print(f"集群进程在跑（PID {pid}）但暂未响应，不重复拉起。"
                  f"\n若确实卡死：hive cluster restart", file=sys.stderr)
            return 0
    if health:
        want_store = config.store_enabled or config.claim_backend == "store"
        store = health.get("store") if isinstance(health.get("store"), dict) else {}
        mcp = health.get("mcp") if isinstance(health.get("mcp"), dict) else {}
        if want_store and "tasks" not in store:
            print(f"集群已在运行，但不是 store 模式：{config.name} "
                  f"http://127.0.0.1:{config.port}\n"
                  "请用 `hive daemon restart --store --claim-backend store` 重启，"
                  "或先 `daemon stop` 再 start。", file=sys.stderr)
            return 1
        if config.mcp_enabled and not mcp.get("enabled"):
            print(f"集群已在运行，但 MCP HTTP 未启用/未就绪：{config.name}\n"
                  "请用 `hive daemon restart --mcp` 重启。", file=sys.stderr)
            return 1
        print(f"集群已在运行：{config.name} term={health.get('term')} "
              f"role={health.get('role')} http://127.0.0.1:{config.port}")
        return 0
    config.state_dir.mkdir(parents=True, exist_ok=True)
    _clear_stopped(config)   # 显式 start/restart/ensure=恢复自动拉起
    # 先把端口/名字/seed 落盘，后续 `hive cluster status` 不用重复传参
    config.save()
    try:
        if config.log_path.exists() and config.log_path.stat().st_size > 5 * 1024 * 1024:
            os.replace(config.log_path, config.log_path.with_suffix(".log.1"))
    except OSError:
        pass
    hive_py = Path(__file__).resolve().with_name("hive.py")
    argv = [sys.executable, str(hive_py), "--dir", str(config.hive_dir),
            "cluster", "serve",
            "--port", str(config.port),
            "--host", str(config.bind_host),
            "--discovery-port", str(config.discovery_port),
            "--state-dir", str(config.state_dir),
            "--name", str(config.name)]
    if config.advertise:
        argv += ["--advertise", str(config.advertise)]
    if not config.electable:
        argv += ["--no-elect"]
    if not config.sync_enabled:
        argv += ["--no-sync"]
    if config.mcp_enabled:
        argv += ["--mcp", "--mcp-port", str(config.mcp_port),
                 "--mcp-bind", str(config.mcp_bind)]
    if config.store_enabled:
        argv += ["--store"]
    if config.claim_backend == "store":
        argv += ["--claim-backend", "store"]
    if config.coordinator_enabled:
        argv += ["--coordinator"]
    if not config.discover:
        argv += ["--no-discover"]
    for seed in config.seeds:
        argv += ["--seed", seed]
    env = dict(os.environ)
    if config.token:
        env["HIVE_CLUSTER_TOKEN"] = config.token
    env["HIVE_CLUSTER_LEASE_GRACE"] = str(config.lease_grace)
    env["HIVE_CLUSTER_ID"] = config.cluster_id
    log = open(config.log_path, "ab", buffering=0)
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
              "env": env, "cwd": str(config.hive_dir)}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"启动集群失败：{exc}", file=sys.stderr)
        return 1
    finally:
        try:
            log.close()
        except OSError:
            pass
    # PID 由子进程自己在上台时写（见 _load_state 之后的自述），这里**不再代写**。
    # 代写过的后果链：父进程 spawn 失败（端口已被占）→ 仍把 pid 写成自己 → 立刻退出
    # → pid 文件指向一个死进程 → 下一轮 ensure 读不到活 PID 又去 spawn → 循环，
    # 且 `hive cluster stop` 按这个 PID 停不掉真正在服务的那个节点。
    #
    # 自检窗口必须比「进程真正就绪」长。原先写死 8s，在 Windows 上会误判：
    # Microsoft Store 版 Python 冷启动 + 打开 store（上百张卡）+ 起 MCP/发现线程，
    # 很容易超过 8s，此时父进程返回 1，但子进程没被杀、随后照样启动完成 ——
    # 用户看到「未能就绪」，实际服务好好的。更糟的是调用方据此再 restart 一次，
    # 就会去 kill 一个刚要起来的进程。
    deadline = _now() + _startup_timeout()
    while _now() < deadline:
        health = _local_health(config)
        if health:
            # 起来了再补写一次 PID：子进程自己写的是权威值，这里兜住
            # 「子进程还没来得及写、但端口已在服务」的窄窗口。
            if proc.poll() is None:
                try:
                    config.pid_path.write_text(str(proc.pid), encoding="utf-8")
                except OSError:
                    pass
            print(f"集群已启动：{config.name} http://127.0.0.1:{config.port} "
                  f"（日志 {config.log_path}）")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.15)

    # 窗口用尽但子进程还活着 = 只是启动慢，**不是失败**。返回 0 让调用方继续
    # 探活，绝不报错（也绝不重复 spawn）。只有进程真的退了才判定启动失败。
    if proc.poll() is None:
        print(f"集群进程已拉起（pid={proc.pid}），但 {_startup_timeout():.0f}s 内"
              f"尚未响应 health —— 常见于 Windows 冷启动，进程仍在初始化。\n"
              f"请用 `hive cluster status` 稍后确认；日志 {config.log_path}",
              file=sys.stderr)
        return 0
    tail = ""
    try:
        tail = _tail_text(config.log_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    print(f"集群启动失败：进程已退出（pid={proc.pid}）。日志：{config.log_path}\n{tail}",
          file=sys.stderr)
    return 1


STOPPED_MARKER_NAME = "stopped"


def _stopped_marker(config: Config) -> Path:
    return config.state_dir / STOPPED_MARKER_NAME


def _mark_stopped(config: Config) -> None:
    """记下「用户主动 stop」：ensure_background 看到标记就不再自动拉起。

    没有这个标记时，orchestrate 每轮 tick 都会 cluster_ensure，`cluster stop`
    刚杀掉的进程 180 秒内就会复活（2026-10-31 实测）。标记只在显式
    start/restart/serve 时清除。
    """
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        _stopped_marker(config).write_text(_iso() + "\n", encoding="utf-8")
    except OSError:
        pass


def _clear_stopped(config: Config) -> None:
    try:
        _stopped_marker(config).unlink()
    except OSError:
        pass


def _stop_background(config: Config) -> int:
    health = _local_health(config)
    if health:
        try:
            http_json(f"http://127.0.0.1:{config.port}/api/cluster/resign", "POST",
                      {"node_id": health.get("node", {}).get("id", ""),
                       "reason": "cli-stop"}, 0.8, config.token)
        except Exception:  # noqa: BLE001
            pass
    pid = _read_pid(config)
    if not pid:
        if health:
            print("集群在运行，但找不到 pid 文件；请手动结束进程", file=sys.stderr)
            return 1
        _mark_stopped(config)
        print("集群未运行")
        return 0
    if not _pid_alive(pid):
        print("集群未运行（pid 文件已过期）")
        try:
            config.pid_path.unlink()
        except OSError:
            pass
        _mark_stopped(config)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"结束进程 {pid} 失败：{exc}", file=sys.stderr)
        return 1
    deadline = _now() + 5.0
    while _now() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.15)
    if _pid_alive(pid):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    try:
        config.pid_path.unlink()
    except OSError:
        pass
    print(f"集群已停止（pid={pid}）")
    _mark_stopped(config)
    return 0


def _watch(config: Config, since: int = 0) -> int:
    state, _ = _local_state(config)
    if not state:
        print("集群未运行", file=sys.stderr)
        return 1
    cursor = int(since or state.get("generation") or 0)
    seen: set[str] = set()
    first = True
    print(f"监听集群事件（since={cursor}，Ctrl-C 退出）", flush=True)
    try:
        while True:
            code, body = http_json(
                f"http://127.0.0.1:{config.port}/api/cluster/wait"
                f"?since={cursor}&timeout=20", timeout=25, token=config.token)
            if code != 200 or not isinstance(body, dict):
                print("集群暂不可达，2 秒后重试", file=sys.stderr, flush=True)
                time.sleep(2)
                continue
            snap = body.get("state") or {}
            cursor = int(snap.get("generation") or cursor)
            events = snap.get("events") or []
            if first:
                # 首次只打印最近 10 条历史，避免把 500 条旧事件全刷出来
                for ev in events[-10:]:
                    seen.add(str(ev.get("id")))
                    print(f"{ev.get('ts')} [{ev.get('type')}] "
                          f"{ev.get('task') or ev.get('node') or ''} {ev.get('message') or ''}",
                          flush=True)
                first = False
                continue
            for ev in events:
                eid = str(ev.get("id") or "")
                if eid and eid in seen:
                    continue
                if eid:
                    seen.add(eid)
                print(f"{ev.get('ts')} [{ev.get('type')}] "
                      f"{ev.get('task') or ev.get('node') or ''} {ev.get('message') or ''}",
                      flush=True)
    except KeyboardInterrupt:
        return 0


def _find_node(snap: dict, want: str) -> tuple[str, str, str]:
    """按名字或 id 在节点表里找人，返回 (id, name, url)。

    名字匹配是主流用法（人记得住 fb98，记不住随机 id），
    但 id 也要支持 —— 脚本里往往直接用 id 更稳。
    """
    want = (want or "").strip()
    if not want:
        return "", "", ""
    nodes = (snap or {}).get("nodes") or []
    for n in nodes:
        if str(n.get("id") or "") == want or str(n.get("name") or "") == want:
            return str(n.get("id") or ""), str(n.get("name") or ""), str(n.get("url") or "")
    return "", "", ""


def _cmd_leader(config: Config, node: str | None = None) -> int:
    health = _local_health(config)
    if not health:
        print("集群未运行（先 hive cluster start）", file=sys.stderr)
        return 1
    snap, _running = _local_state(config)
    snap = snap or {}
    leader = snap.get("leader") or {}
    me = (health.get("node") or {})
    peer_count = snap.get("alive")
    quorum = snap.get("quorum")

    print(f"本机 {me.get('name') or config.name}  角色 {snap.get('role') or health.get('role')}"
          f"  任期 {snap.get('term')}")
    if leader:
        lease = leader.get("lease_until_epoch") or 0
        left = max(0, int(lease - _now())) if lease else 0
        print(f"主节点 {leader.get('name') or leader.get('id')}  {leader.get('url') or ''}"
              f"  租约剩余 {left}s")
    else:
        print("主节点 （无）—— 正在选举，或多数派不可用")
    if peer_count is not None:
        print(f"在线 {peer_count}  多数门槛 {quorum}")
    if config.preferred_leader:
        print(f"首选主控（本机配置） {config.preferred_leader}")
    else:
        print("首选主控（本机配置） （未设置；集群按默认规则选主）")
    if node:
        nid, name, url = _find_node(snap, node)
        if nid:
            print(f"\n指定节点 {name or nid}  {url}")
            print(f"  {'就是当前主控' if nid == (leader.get('id') or '') else '不是主控'}")
        else:
            print(f"\n找不到节点 {node}", file=sys.stderr)
            return 1
    return 0


def _cmd_promote(config: Config, node: str | None = None) -> int:
    """让人立刻把某个节点推上主控位置。"""
    snap, _running = _local_state(config)
    snap = snap or {}
    me_health = _local_health(config) or {}
    me = me_health.get("node") or {}

    target = (node or "").strip()
    if not target or target in (str(me.get("id") or ""), str(me.get("name") or ""), config.name):
        url = f"http://127.0.0.1:{config.port}"
        label = me.get("name") or config.name
    else:
        nid, name, node_url = _find_node(snap, target)
        if not nid:
            print(f"找不到节点 {target}（先 hive cluster peers 看一眼）", file=sys.stderr)
            return 1
        url, label = node_url, (name or nid)

    if not me_health and not target:
        print("集群未运行（先 hive cluster start）", file=sys.stderr)
        return 1

    code, body = http_json(url.rstrip("/") + "/api/cluster/promote", "POST",
                           {"force": True}, timeout=10.0, token=config.token)
    if not isinstance(body, dict):
        print(f"提升失败：HTTP {code}", file=sys.stderr)
        return 1
    if body.get("ok"):
        if body.get("already"):
            print(f"{label} 已经是主控（任期 {body.get('term')}），无需切换")
        else:
            prev = body.get("previous_leader") or ""
            print(f"已切换主控 → {label}（任期 {body.get('term')}）"
                  + (f"，原主控 {prev} 已下台" if body.get("resigned_previous") else ""))
        return 0
    print(f"提升失败：{body.get('error') or f'HTTP {code}'}", file=sys.stderr)
    if body.get("resigned_previous"):
        print("（原主控已下台，但现在没人当选 —— 集群暂时没有主控，"
              "通常是 pex 不在线或多数派不足）", file=sys.stderr)
    return 1


def _cmd_prefer(config: Config, node: str | None = None, clear: bool = False) -> int:
    """设置/清除「首选主控」。

    偏好存在**本机** config.json 里，作用于本机的选举延迟：被偏好的节点在主控
    失联后立刻发起选举（delay=0），其它节点让出一个窗口。所以要让某台机器稳定
    当主控，就在**那台机器**上配（或启动时用 `hive cluster start --leader`）。
    """
    if clear:
        if not config.preferred_leader:
            print("当前没有设置首选主控")
            return 0
        old = config.preferred_leader
        config.preferred_leader = ""
        config.save()
        print(f"已清除首选主控（原 {old}）")
        return 0

    # 不带参数 = 把自己设为首选。这是最常用的意图：
    # 「这台就是主控机，重启以后也还是我」。
    target = (node or "").strip() or config.name
    config.preferred_leader = target
    config.save()

    if target == config.name:
        print(f"已把本机（{config.name}）设为首选主控")
        print("  主控失联重新选主时，本机会最先发起选举，把主控权拿回来。")
        print("  想立刻生效而不等下次选举：hive cluster promote")
        return 0

    # 指定了别人：偏好仍只写在本地，要如实说明它不会把对方推上去。
    print(f"已记录首选主控：{target}（本机 {config.name} 的配置）", file=sys.stderr)
    print(f"  这条偏好只影响本机的选举时机，不会把 {target} 推上主控。",
          file=sys.stderr)
    print(f"  要让它真的当家：\n"
          f"    在 {target} 上执行  hive cluster prefer      # 长期\n"
          f"    或在这里执行        hive cluster promote {target}   # 立刻", file=sys.stderr)
    return 0


def _claim_leadership_after_start(config: Config) -> None:
    """`start --leader` 的后半段：起来之后如果主控在别人手上，直接拿回来。

    只写 preferred_leader 是不够的 —— 偏好只在「当前没有主控」时生效。
    集群里已经有主控时，新启动的节点只是注册成 follower，那台笔记本照样当家。
    用户的预期是「我说了算，启动就该是我」，所以这里补一次夺权。
    """
    deadline = _now() + 12.0
    health = None
    while _now() < deadline:
        health = _local_health(config)
        if health:
            break
        time.sleep(0.3)
    if not health:
        print("警告：集群 12s 内没起来，--leader 没能落实（看 cluster.log）",
              file=sys.stderr)
        return

    me = (health.get("node") or {}).get("id")
    leader = health.get("leader") or {}
    if leader.get("id") == me:
        print(f"本机 {config.name} 已是主控")
        return

    code, body = http_json(f"http://127.0.0.1:{config.port}/api/cluster/promote",
                           "POST", {"force": True}, timeout=12.0,
                           token=config.token)
    if isinstance(body, dict) and body.get("ok"):
        print(f"已从 {leader.get('name') or '（无主控）'} 拿回主控"
              f"（任期 {body.get('term')}）")
    else:
        reason = (body or {}).get("error") if isinstance(body, dict) else f"HTTP {code}"
        print(f"拿回主控失败：{reason}", file=sys.stderr)


def _cmd_priority(config: Config, value: int | None = None,
                  restart: bool = True) -> int:
    """查看/设置本机的主控优先级（OSPF 式，值大者优先，默认 1）。

    写 config.json 后**重启本机节点**让它生效 —— 优先级是启动时读进内存的，
    不重启的话命令行改完看起来毫无反应，这种「改了没生效」最难查。
    """
    if value is None:
        snap, running = _local_state(config)
        nodes = (snap or {}).get("nodes") or []
        tip = "" if config.leader_priority > 0 else "（0 = 不参选主控）"
        print(f"本机 {config.name} 优先级：{config.leader_priority}{tip}")
        if not running or not nodes:
            return 0
        ranked = sorted(nodes,
                        key=lambda n: (-normalize_leader_priority(
                            n.get("priority")), str(n.get("name") or "")))
        print("\n按优先级排序（当前无主控时，最上面那台先发起选举）：")
        print(f"  {'NAME':<24} {'PRIO':<5} {'STATUS':<8} ROLE")
        for n in ranked:
            prio = normalize_leader_priority(n.get("priority"))
            print(f"  {str(n.get('name') or n.get('id'))[:24]:<24} "
                  f"{str(prio):<5} {str(n.get('status') or ''):<8} "
                  f"{n.get('role') or ''}")
        leader = snap.get("leader") or {}
        if leader.get("name"):
            print(f"\n当前主控：{leader.get('name')}"
                  f"（优先级不抢占在位主控，只在没有主控时才按它选）")
        return 0

    # 设置：只改本机自己的值。优先级随自述广播，别的节点自己会看到，
    # 不需要（也不该）在别人机器上代设。
    try:
        prio = int(value)
    except (TypeError, ValueError):
        print(f"优先级必须是整数：{value!r}", file=sys.stderr)
        return 2
    if not (MIN_LEADER_PRIORITY <= prio <= MAX_LEADER_PRIORITY):
        print(f"优先级必须在 {MIN_LEADER_PRIORITY}..{MAX_LEADER_PRIORITY} 之间"
              f"（OSPF DR priority 是 8bit：0=放弃竞选资格，1=默认，"
              f"值大者优先）：{prio}", file=sys.stderr)
        return 2

    old = _read_json(config.state_dir / "config.json", {}) or {}
    old_prio = old.get("leader_priority") if isinstance(old, dict) else None
    try:
        old_prio = int(1 if old_prio is None else old_prio)
    except (TypeError, ValueError):
        old_prio = 1
    config.leader_priority = prio
    config.save()
    note = "（0 = 不主动参选主控）" if prio == 0 else ""
    print(f"本机 {config.name} 优先级：{old_prio} -> {prio}{note}")

    health = _local_health(config)
    if not health:
        print("  本机节点未运行，下次启动时生效。")
        return 0
    if not restart:
        print("  需要重启本机节点才生效：hive cluster restart")
        return 0
    print("  重启本机节点让它生效…")
    _stop_background(config)
    if _spawn_background(config, None) == 0:
        h = _local_health(config)
        if h:
            print(f"  已生效：role={h.get('role')} term={h.get('term')} "
                  f"priority={prio}")
            print("  想立刻当主控：hive cluster promote")
            return 0
        print("  重启后未就绪，请 `hive cluster status` 确认。")
        return 0
    print("  重启失败，请手动 `hive cluster restart` 并看日志。", file=sys.stderr)
    return 1


def run_cli(args) -> int:
    action = getattr(args, "action", "status") or "status"
    config = _config_from_args(args)
    config.state_dir.mkdir(parents=True, exist_ok=True)

    # `--leader` 是「启动时的手动开关」：把本机记为首选主控。
    # 写进 config.json 而不是只影响这一次进程 —— 机器重启、集群重新选主之后
    # 它仍然会最先发起选举，把主控权拿回来。这才是「一直是 0.88 当主控」的
    # 那个「一直」该由谁来保证。
    if getattr(args, "leader", False) and action in ("serve", "start", "restart", "ensure"):
        if config.preferred_leader != config.name:
            config.preferred_leader = config.name
            config.save()
        print(f"本机 {config.name} 记为首选主控（preferred_leader）")

    if action == "serve":
        cluster = Cluster(config)
        try:
            _clear_stopped(config)
            cluster.start()
        except ClusterError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        def _handle_signal(signum, frame):  # noqa: ARG001
            cluster.stop()
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, _handle_signal)
                except (OSError, ValueError):
                    pass
        try:
            while not cluster.wait_stopped(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            cluster.stop()
        return 0

    if action == "start":
        rc = _spawn_background(config, args)
        if rc == 0 and getattr(args, "leader", False):
            _claim_leadership_after_start(config)
        return rc

    if action == "stop":
        rc = _stop_background(config)
        if getattr(args, "restart", False):
            return _spawn_background(config, args)
        return rc

    if action == "restart":
        _stop_background(config)
        rc = _spawn_background(config, args)
        if rc == 0 and getattr(args, "leader", False):
            _claim_leadership_after_start(config)
        return rc

    if action == "ensure":
        if _local_health(config):
            return 0
        if _stopped_marker(config).exists():
            print("集群已手动停止（stopped 标记），跳过拉起；要恢复请 `hive cluster start`")
            return 0
        return _spawn_background(config, args)

    if action == "join":
        raw = getattr(args, "seed_url", None) or getattr(args, "seed", None)
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not raw:
            print("join 需要 HOST:PORT 或 http://HOST:PORT", file=sys.stderr)
            return 2
        url = normalize_url(raw, config.port)
        if url not in config.seeds:
            config.seeds.append(url)
            config.save()
        health = _local_health(config)
        if health:
            code, body = http_json(
                f"http://127.0.0.1:{config.port}/api/cluster/seed", "POST",
                {"url": url}, timeout=3.0, token=config.token)
            if code == 200 and isinstance(body, dict):
                print(f"已加入 {url}" if body.get("joined")
                      else f"种子 {url} 已记录，但当前不可达（会持续重试）")
                return 0 if body.get("joined") else 1
        print(f"种子已记录：{url}（集群启动后会尝试注册）")
        return 0

    if action == "leave":
        health = _local_health(config)
        if not health:
            print("集群未运行")
            return 0
        node_id = (health.get("node") or {}).get("id", "")
        http_json(f"http://127.0.0.1:{config.port}/api/cluster/leave", "POST",
                  {"node_id": node_id}, timeout=1.5, token=config.token)
        print(f"已从集群退出：{node_id}")
        return 0

    if action == "leader":
        return _cmd_leader(config, getattr(args, "node", None))

    if action == "promote":
        return _cmd_promote(config, getattr(args, "node", None))

    if action == "prefer":
        return _cmd_prefer(config, getattr(args, "node", None),
                           bool(getattr(args, "clear", False)))

    if action == "priority":
        return _cmd_priority(config, getattr(args, "priority", None),
                             restart=not getattr(args, "no_restart", False))

    if action == "discover":
        health = _local_health(config)
        if not health:
            print("集群未运行", file=sys.stderr)
            return 1
        code, body = http_json(f"http://127.0.0.1:{config.port}/api/cluster/discover",
                               "POST", {}, timeout=3.0, token=config.token)
        if code == 200 and isinstance(body, dict):
            print("已发现并注册" if body.get("joined") else "本轮没有发现可注册的主节点")
            return 0
        print("发现失败", file=sys.stderr)
        return 1

    if action == "watch":
        return _watch(config, int(getattr(args, "since", 0) or 0))

    snap, running = _local_state(config)
    if action == "status":
        if getattr(args, "json", False):
            health = _local_health(config) or {}
            _print_json({"running": running, "state": snap,
                         "mcp": health.get("mcp") or {}})
        else:
            print(_status_line(snap, running, _stopped_marker(config).exists()))
            health = _local_health(config) or {}
            mcp = health.get("mcp") or {}
            if mcp:
                print(f"MCP HTTP {mcp.get('url') or '-'}  "
                      f"{'运行中' if mcp.get('enabled') else '未启用'}"
                      + (f"  ({mcp.get('last_error')})" if mcp.get("last_error") else ""))
            store = health.get("store") or {}
            if store and "tasks" in store:
                print(f"store {store.get('path') or '-'}  tasks={store.get('tasks')} "
                      f"claims={store.get('claims')} results={store.get('results')} "
                      f"last_seq={store.get('last_seq')}")
            print(_dashboard_line())
        return 0 if snap is not None else 1

    if action == "peers":
        if not snap:
            print("集群未运行", file=sys.stderr)
            return 1
        nodes = snap.get("nodes") or []
        if getattr(args, "json", False):
            _print_json(nodes)
        else:
            print(f"{'STATUS':<8} {'NAME':<24} {'ROLE':<9} {'TERM':<5} {'PRIO':<5} URL")
            for n in nodes:
                # 优先级是「主控归属」的第一顺位依据，放在表格里才能一眼看出
                # 下次选主该轮到谁，不然只能靠翻配置。
                print(f"{str(n.get('status', '?')):<8} "
                      f"{str(n.get('name') or n.get('id'))[:24]:<24} "
                      f"{str(n.get('role') or ''):<9} {n.get('term', 0):<5} "
                      f"{str(n.get('priority') if n.get('priority') is not None else 1):<5} "
                      f"{n.get('url') or ''}")
        return 0

    if action == "sync":
        health = _local_health(config)
        if not health:
            print("集群未运行", file=sys.stderr)
            return 1
        code, body = http_json(
            f"http://127.0.0.1:{config.port}/api/cluster/sync",
            timeout=1.5, token=config.token)
        sync = (body or {}).get("sync") if isinstance(body, dict) else None
        if code != 200 or not isinstance(sync, dict):
            print(f"读取同步状态失败：HTTP {code}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            _print_json(sync)
        else:
            print(f"代码同步：{'开启' if sync.get('enabled') else '关闭'}"
                  f"（{'运行中' if sync.get('running') else '未运行'}）")
            print(f"最近一次：{sync.get('last_run') or '-'}")
            if sync.get("last_error"):
                print(f"最近错误：{sync['last_error']}")
            for name, rec in sorted((sync.get("repos") or {}).items()):
                rec = rec if isinstance(rec, dict) else {}
                print(f"  {name:<12} {str(rec.get('status') or '-'):<14} "
                      f"{str(rec.get('sha') or '')[:12]:<12} {rec.get('path') or ''}")
        return 0

    if action == "events":
        if not snap:
            print("集群未运行", file=sys.stderr)
            return 1
        events = snap.get("events") or []
        since = int(getattr(args, "since", 0) or 0)
        if since:
            events = [e for e in events if int(e.get("seq") or 0) > since]
        if getattr(args, "json", False):
            _print_json(events)
        else:
            for e in events:
                print(f"{e.get('ts')} [{e.get('type')}] "
                      f"{e.get('task') or e.get('node') or ''} {e.get('message') or ''}")
        return 0

    print(f"未知动作 {action}", file=sys.stderr)
    return 2


def ensure_background(hive_dir=None, state_dir=None) -> bool:
    """给 `hive work` / `hive orchestrate` 用：本机没有集群就静默拉起。"""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("HIVE_TESTING"):
        return False
    if not _env_bool("HIVE_CLUSTER_AUTOSTART", True):
        return False
    config = Config(hive_dir=hive_dir, state_dir=state_dir)
    if _local_health(config):
        return True
    if _stopped_marker(config).exists():
        # 用户显式 stop 之后，orchestrate/worker 的「按需自愈」不能再把它拉起来，
        # 否则 stop 看起来「没生效」（实测：180s 内复活）。
        return False
    if _spawn_background(config, None) == 0:
        return True
    return False


# 供 hive.py 的 argparse 直接复用
def add_parser(sub) -> None:
    p = sub.add_parser(
        "cluster", aliases=["daemon"],
        help="常驻集群/hived：自动发现/注册/主节点选举；daemon 别名默认带 MCP HTTP")
    p.add_argument("action", nargs="?", default="status",
                   choices=["serve", "start", "stop", "restart", "ensure",
                            "status", "peers", "events", "sync", "join", "leave",
                            "discover", "watch", "leader", "promote", "prefer",
                            "priority"])
    p.add_argument("--node", default=None,
                   help="leader/promote/prefer 的目标节点（名字或 id，默认本机）")
    p.add_argument("--leader", action="store_true",
                   help="start/restart 时把本机设为首选主控（重启后自动拿回主控权）")
    p.add_argument("--clear", action="store_true",
                   help="prefer --clear：清除本机的首选主控设置")
    p.add_argument("--priority", type=int, default=None,
                   help="主控优先级（OSPF 式 0-255：默认 1，值大者优先，"
                        "0 = 放弃竞选资格）。"
                        "serve/start 时设定；cluster priority --priority N 改本机")
    p.add_argument("--no-restart", action="store_true",
                   help="cluster priority 改完不自动重启本机节点")
    p.add_argument("seed_url", nargs="?", help="join 的目标（HOST:PORT 或 URL）")
    p.add_argument("--port", type=int, default=None,
                   help=f"HTTP 端口（默认 {DEFAULT_PORT}）")
    p.add_argument("--host", default=None, help="HTTP 绑定地址（默认 0.0.0.0）")
    p.add_argument("--advertise", default=None, help="对外公布地址（默认自动猜局域网 IP）")
    p.add_argument("--name", default=None, help="节点名（默认 hostname）")
    p.add_argument("--seed", action="append", default=None,
                   help="静态种子节点，可重复；HOST:PORT 或 URL")
    p.add_argument("--discovery-port", type=int, default=None,
                   help=f"UDP 发现端口（默认 {DEFAULT_DISCOVERY_PORT}）")
    p.add_argument("--no-discover", action="store_true", help="关闭 UDP 广播发现")
    p.add_argument("--state-dir", default=None,
                   help="集群状态目录（默认 ~/.hive/cluster 或 $HIVE_CLUSTER_STATE）")
    p.add_argument("--token", default=None,
                   help="共享 token（也可用 $HIVE_CLUSTER_TOKEN；跨机必配）")
    p.add_argument("--lease-grace", type=float, default=None,
                   help="新主上任后租约恢复宽限秒数（默认 120）")
    p.add_argument("--no-elect", action="store_true",
                   help="observer：不参选、不投票（verify-only 机器用）")
    p.add_argument("--no-sync", action="store_true",
                   help="关闭本机 repo.updated 代码同步")
    p.add_argument("--mcp", action="store_true",
                   help="启动本机 MCP HTTP 入口（127.0.0.1:8790）")
    p.add_argument("--no-mcp", action="store_true", help="不启动 MCP HTTP 入口")
    p.add_argument("--mcp-port", type=int, default=None,
                   help="MCP HTTP 端口（默认 8790）")
    p.add_argument("--mcp-bind", default=None,
                   help="MCP 绑定地址（默认 127.0.0.1）")
    p.add_argument("--store", action="store_true",
                   help="启用本地 SQLite store（P2）")
    p.add_argument("--no-store", action="store_true", help="关闭 store")
    p.add_argument("--claim-backend", choices=["git", "store"], default=None,
                   help="claim 走 Git（默认）或 store（P2）")
    p.add_argument("--coordinator", action="store_true",
                   help="leader 在 hived 内跑单轮 orchestrate（P2b；默认关）")
    p.add_argument("--since", type=int, default=0, help="events/watch 的起始序号")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: run_cli(a))
