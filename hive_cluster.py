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
import contextlib
import hashlib
import hmac
import io
import json
import os
import random
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
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client as httpclient
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# 常量与配置
# --------------------------------------------------------------------------- #

PROTOCOL = 1


def _detect_hive_version() -> str:
    """取「本进程正在跑的 hive 版本」，给节点自述和 User-Agent 用。

    以前这里是写死的 ``"1.0"``，看板「集群」页每个节点都显示 1.0，跟
    `hive --version` 完全脱节，升级后也看不出谁跑旧代码。现在改成进程启动时
    读同目录 hive.py 的 ``VERSION`` 常量：

    * 读的是**启动那一刻**的文件内容，所以升级但没重启的进程仍报旧版本——
      这正好是事实（它跑的还是旧代码），看板据此能发现「该重启了」。
    * 只读文件不做 import，避免 hive.py ↔ hive_cluster.py 循环导入（hive.py
      是懒加载本模块的，反向在顶部 import 会炸）。
    * 读不到就退回 ``"0.0.00"``，宁可显示未知，也不要谎报版本。
    """
    try:
        path = Path(__file__).resolve().with_name("hive.py")
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1).strip() if m else ""


# 节点自述里的 version（看板「集群」页那一列）。取不到时用 0.0.00。
CLUSTER_VERSION = _detect_hive_version() or "0.0.00"

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
# 空闲期（没有在跑的任务、没有有效租约）的心跳间隔。1s 一拍会把全量
# progress+leases 每秒在节点间来回搬：现场实测空转就有 2-3 Mbps 收发，
# state.json 也每秒重写一次（565KB）。空闲降频，任务一动立刻回 1s。
DEFAULT_IDLE_HEARTBEAT_S = 5.0
# 空闲期仍然定期全量重传一次：某一跳丢包时，对端不会永远停在旧视图上。
FULL_BROADCAST_S = 30.0
# state.json 落盘节流（秒）。原先 1s 一次全量序列化 + os.replace，空闲期
# 也能把磁盘写到 1.8MB/s；5s 足够让「重启后视图」保持新鲜。
PERSIST_INTERVAL_S = 5.0
# 心跳响应最多回多少条增量事件（events 本身是 deque(EVENT_LIMIT)）。
EVENT_PUSH_LIMIT = 120
# 0.16.05：心跳是「轻量 ping」，只在内容真的变了时才搬大块状态。
#
# 之前用 generation 当同步水位，但 generation 是**每个节点本地自增**的计数器
# （现场四台：607万 / 300万 / 1056万 / 42万），跨节点永远不相等，于是
# `changed = req_gen != self.generation` 恒为 True —— 每次心跳请求和响应都带上
# 全量 progress+leases（约 50KB），而实测四台机器的内容哈希完全一致。再加上
# 每拍都回整份 nodes（每条含 1.6KB agents）和最多 120 条 events（~32KB），
# 空闲期实测稳定在 ~600KB/s。
#
# 现在改用「内容摘要」：请求方带上自己 progress+leases 的摘要，应答方只在
# 摘要不同时才回全量；旧节点不带摘要 → 仍走全量，滚动升级期间保持兼容。
HEARTBEAT_EVENT_LIMIT = 32
# 心跳事件只回「最近这么久」的（事件自带 _now() 墙钟时间戳，各机时钟实测
# 误差 <1s）。请求方每拍发 `events_since = now - EVENT_WINDOW_S`，应答方只回
# epoch 比它新的事件，重复按 id 去重。
#
# 关键是**窗口跟着墙钟走**：空闲期没有新事件，窗口内就是空的，响应里
# `events` 恒为 [] —— 不会像旧的 seq 机制那样常年重发 120 条（~32KB），
# 也不会像「水位只跟收到的事件走」那样把已有事件永远重发一遍。
# 10s 大于空闲心跳间隔（5s）的 2 倍：每个事件至少能被下一拍、下下拍看到，
# 降频后不漏事件；也盖住常见的时钟偏斜。事件只是通知（node_up /
# task_progress / leader_elected），权威状态是 progress/leases/nodes，靠摘要
# 同步；`repo.updated` 另有 RepoSyncAgent 每 5 分钟直接比远端 HEAD 的补偿
# 通道（_reconcile），不依赖通知长期留存。长时间离线漏几条通知可以接受。
EVENT_WINDOW_S = 10.0
# 心跳响应里的节点记录不下发这些「展示用/大块静态」字段。agents 单节点就有
# ~1.6KB（agent 列表），7 个节点 ~11KB，而它只在看板配置里用，心跳路径没人读。
HEARTBEAT_NODE_SKIP_KEYS = ("agents", "capabilities", "hive_dir",
                            "hostname", "pid", "started_at", "joined_at",
                            "cluster_id", "protocol")
# 「在跑的活儿」的最长有效期：租约/进度超过这么久没更新就不算活跃，避免一条
# 远期租约（现场 T-0332 的 lease_until 比当前时间晚 6 小时、两小时没续过）
# 把心跳永久钉在 1s，空闲降频永远失效。
ACTIVE_WORK_WINDOW_S = 600.0
# 留言增量拉取：活跃期 2s，空闲退避到 5s；收到/发出新留言后回到活跃期。
CHAT_SYNC_FAST_S = 2.0
CHAT_SYNC_IDLE_S = 5.0
CHAT_SYNC_ACTIVE_WINDOW_S = 30.0
# 节点记录里「只表示它还活着/租约刚续」的字段：只有这些变化时不算状态
# 变更，不能推进 generation。lease_until* 每跳都会被主控刷新，若算变化，
# follower 每次心跳都会把 generation 推一格，全量广播永远停不下来。
_NODE_VOLATILE_KEYS = ("last_seen", "last_seen_epoch", "_status",
                       "lease_until", "lease_until_epoch",
                       "grace_until", "grace_until_epoch")
DEFAULT_PEER_TIMEOUT_S = 4.0
DEFAULT_DEAD_TIMEOUT_S = 15.0
DEFAULT_ELECTION_MIN_S = 3.0
DEFAULT_ELECTION_JITTER_S = 0.8
DEFAULT_LEADER_LEASE_S = 5.0
DEFAULT_DISCOVERY_INTERVAL_S = 5.0
# 成员表保留多久（仅用于展示与 GC）。
#
# 历史：T-0299 曾用它做「dead 成员仍算票」的兜底，防「网络抖一下 quorum 塌成 1、
# 单票自封」。那个兜底后来被证明是灾难：真正下线/从没见过的机器会一直占票位，
# 把 quorum 抬高到实际凑不够（现场 3 台真机 + 2 条僵尸 → quorum 4 → 连续 382 次
# 「未获多数」）。T-0328 起票池只收 alive/suspect，dead 一律出池，与 OSPF 的
# RouterDeadInterval 一致；选主本身也不再要求多数票。这个 TTL 现在只影响
# 节点表的展示时长与墓碑回收节奏。
DEFAULT_MEMBERSHIP_TTL_S = 300.0
# 墓碑保留期（秒）：被显式删除/清理的 node_id 在这么长时间内不许被转述复活。
# 取 1 小时 —— 远长于一次故障排查，又不会让「机器改名后重新入网」永久被挡
# （真重入网会带新 incarnation，且 1 小时后自动放行）。
TOMBSTONE_TTL_S = 3600.0
# 对外地址自校准周期（秒）。DHCP 租期远短于进程寿命：Wi-Fi 换网、笔记本
# 睡眠唤醒都可能让本机 IP 变掉，而启动时算出的 advertise 不会自己跟进。
DEFAULT_ADVERTISE_REFRESH_S = 30.0

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


def _state_digest(progress: dict, leases: dict) -> str:
    """progress+leases 的内容摘要：跨节点可比、内容不变则不变。

    generation 是节点本地计数器，不能跨机比较（0.16.05 修的正是这个坑）。
    摘要用 sha256 取前 16 位：心跳每拍都要算，18 张进度 + 95 条租约约 50KB，
    C 实现的 json.dumps + hashlib 一次不到 1ms，比把 50KB 发上网络便宜几个
    数量级。排序 + 紧凑分隔符保证「字典顺序不同但内容相同」也一致。
    """
    payload = {"progress": progress, "leases": leases}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


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


def _node_url_host_matches(url: str, ip: str) -> bool:
    """节点自述的 URL 主机，是否就是这次 HTTP 请求的来源 IP。

    挡的是「拿别人的身份自称主控」：一台机器若沿用旧 node.json 里的 id/url
    （现场是旧 Mac 身份 + 早已下线的 192.168.0.103）发 leader 心跳，照单全收
    就会把主控指向一个连不上的地址，随后同伴转述再把租约永久续命。
    自述是域名/主机名时不做比对，避免误伤配了 advertise 域名的部署。
    """
    if not url or not ip:
        return True
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").strip("[]").lower()
    except ValueError:
        return False
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    peer = str(ip).strip("[]").lower()
    if peer.startswith("::ffff:"):
        peer = peer[7:]
    if host == peer:
        return True
    # 回环的几种写法等价（单机/测试常用）
    return (host in ("127.0.0.1", "::1", "localhost")
            and peer in ("127.0.0.1", "::1", "localhost"))


def _reply_is_from_leader(resp) -> bool:
    """一次心跳/注册回复是否来自**主控本人**。

    跟随者的回复里也会带 ``leader`` 字段，但那是它「听说」的主控视图，不是
    主控本人。只有主控自己发的回复才有权给租约续命。``is_leader`` 是本次
    新增的显式标记；老版本节点没有这个字段时用 ``role`` 兜底（role 一直是
    真实角色，不像节点表里那条被标注出来的 role）。
    """
    if not isinstance(resp, dict):
        return False
    flag = resp.get("is_leader")
    if flag is not None:
        return flag is True
    return str(resp.get("role") or "") == "leader"


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
        # 显式 advertise（参数/环境变量/磁盘）是人指定的地址，自校准必须让路；
        # 只有「自动猜」出来的地址才允许随网卡变化跟进。
        self.advertise = str(
            kw.get("advertise") or os.environ.get("HIVE_CLUSTER_ADVERTISE")
            or saved.get("advertise") or "")
        self.advertise_explicit = bool(self.advertise)
        refresh = kw.get("advertise_refresh")
        if refresh is None:
            refresh = os.environ.get("HIVE_CLUSTER_ADVERTISE_REFRESH")
        if refresh is None:
            refresh = saved.get("advertise_refresh")
        self.advertise_refresh = float(
            DEFAULT_ADVERTISE_REFRESH_S if refresh is None else refresh)
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
        # 链路速率（Mbit/s），主控排序的第二权重（T-0328，用户指定）。
        # priority 相等时，速率高的那台更该当主：它带得动更多同步/心跳负载。
        # 0 = 未声明，排序时按「未知」处理（排在同 priority 的已知速率之后，
        # 但不影响通过 priority 决胜）。可用 HIVE_CLUSTER_LINK_SPEED 覆盖。
        raw_speed = kw.get("link_speed")
        if raw_speed is None:
            raw_speed = os.environ.get("HIVE_CLUSTER_LINK_SPEED")
        if raw_speed is None:
            raw_speed = saved.get("link_speed")
        try:
            self.link_speed = max(0, int(raw_speed or 0))
        except (TypeError, ValueError):
            self.link_speed = 0
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
        # 空闲期心跳间隔：必须 >= heartbeat_interval，否则「空闲更勤」。
        # 显式传入 heartbeat_interval 的调用方（测试、私有部署）默认不额外
        # 降频，保持「你设多少就多少」；只有走默认值/环境变量时才用 5s。
        _idle_default = (kw["heartbeat_interval"] if kw.get("heartbeat_interval")
                         else _env_float("HIVE_CLUSTER_IDLE_HEARTBEAT",
                                         DEFAULT_IDLE_HEARTBEAT_S))
        self.idle_heartbeat_interval = max(
            self.heartbeat_interval,
            float(kw.get("idle_heartbeat_interval") or _idle_default))
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
        # 成员表 TTL：见 DEFAULT_MEMBERSHIP_TTL_S 的注释。仅用于展示/GC；
        # 票池口径自 T-0328 起只认 alive/suspect，dead 不再抬高 quorum。
        self.membership_ttl = float(
            kw.get("membership_ttl")
            or _env_float("HIVE_CLUSTER_MEMBERSHIP_TTL", DEFAULT_MEMBERSHIP_TTL_S))
        # 显式允许「1 票当选」（默认关）。T-0328 起选主不再要求多数票，
        # 这个开关仅保留给 op 复制的多数判定与历史兼容；已知成员 >1 时
        # 打开它等于放弃多数保护，运维必须自己知道这一点。
        if kw.get("allow_single_vote") is None:
            self.allow_single_vote = _env_bool(
                "HIVE_CLUSTER_ALLOW_SINGLE_VOTE",
                bool(saved.get("allow_single_vote", False)))
        else:
            self.allow_single_vote = bool(kw.get("allow_single_vote"))
        self.discovery_interval = float(
            kw.get("discovery_interval") or _env_float("HIVE_CLUSTER_DISCOVERY_INTERVAL",
                                                       DEFAULT_DISCOVERY_INTERVAL_S))
        self.hive_dir = Path(
            kw.get("hive_dir") or os.environ.get("HIVE_DIR") or Path.cwd()
        ).expanduser().resolve()
        # D14（实现约定 §4）：控制面只在 `hive cluster serve` 入口开启。
        # 程序化构造的 Cluster 一律默认关闭 —— 既有 50+ 测试实例会同时
        # 实例化 Cluster，默认开启会一起抢 47899 组播端口互相干扰。
        # 进程内内部属性：不读环境变量、不写进 config.json。
        self.control_plane = bool(kw.get("control_plane", False))
        self.control_limits = kw.get("control_limits") or None

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
            "link_speed": int(self.link_speed),
            "preferred_leader": self.preferred_leader,
            "membership_ttl": float(self.membership_ttl),
            "allow_single_vote": bool(self.allow_single_vote),
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


def effective_config_path(config: Config) -> Path:
    """进程真正读取的配置文件：永远是 ``state_dir/config.json``。"""
    return Path(config.state_dir) / "config.json"


def ignored_config_path(config: Config) -> Path | None:
    """``hive_dir`` 下那份「看起来像配置、其实没人读」的 config.json。

    state_dir 默认是 ``~/.hive/cluster``，而 ``--dir`` 常指向一个也叫 hive 的
    仓库根。用户把 config.json 放在仓库里改半天、进程读的却是另一份，
    是最容易让人认定「改了没生效」的坑。返回影子路径用于显式提示；
    None 表示不存在这种文件，或它恰好就是生效的那一份。
    """
    shadow = Path(config.hive_dir) / "config.json"
    try:
        if shadow.resolve() == effective_config_path(config).resolve():
            return None
    except OSError:
        return None
    return shadow if shadow.is_file() else None


def config_source_lines(config: Config) -> list[str]:
    """给人看的配置来源：生效路径 + 关键字段 + 影子文件告警。"""
    lines = [
        f"配置 {effective_config_path(config)}  name={config.name}"
        f"  priority={config.leader_priority}"
        f"  preferred={config.preferred_leader or '-'}",
    ]
    shadow = ignored_config_path(config)
    if shadow:
        lines.append(f"注意 {shadow} 存在但不会被读取（实际只认上面的 state_dir 配置）")
    return lines


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
# 注意：这里直接走 http.client，不读 HTTP(S)_PROXY —— 集群节点在同一局域网，
# 走代理只会超时（旧实现靠 ProxyHandler({}) 达到同样效果）。


# --------------------------------------------------------------------------- #
# HTTP（keep-alive 连接池）
# --------------------------------------------------------------------------- #
# 心跳一秒一拍，原来每次 urlopen 都新建 TCP 连接：对端刚处理完这条连接就被
# 丢弃，两台机器各留一份 TIME_WAIT（现场 429 条）。
#
# 池必须是**进程级共享**的：每次心跳都新建一个 ThreadPoolExecutor，池里的
# 线程用完即销毁，threading.local 缓存等于每拍都从空池开始（实测 TIME_WAIT
# 仍然每拍 +1）。共享池用「取出即独占、用完放回」的语义，不需要担心同一条
# socket 被两个线程同时写。
_CONN_POOL: dict = {}
_CONN_POOL_LOCK = threading.Lock()
_CONN_POOL_MAX = 32


def _pool_take(key):
    with _CONN_POOL_LOCK:
        return _CONN_POOL.pop(key, None)


def _pool_put(key, conn) -> None:
    with _CONN_POOL_LOCK:
        old = _CONN_POOL.pop(key, None)
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        if len(_CONN_POOL) >= _CONN_POOL_MAX:
            # 池里 key 本来就只有「几个同伴 × 几个超时档」，正常不会到上限；
            # 真到了就放弃缓存，宁可多一条短连接也不无限占 fd。
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return
        _CONN_POOL[key] = conn


def _new_conn(scheme: str, host: str, port: int, timeout: float):
    if scheme == "https":
        return httpclient.HTTPSConnection(host, port, timeout=timeout)
    return httpclient.HTTPConnection(host, port, timeout=timeout)


def _http_round_trip(url: str, method: str, data, headers: dict,
                     timeout: float) -> tuple[int, bytes]:
    """发一次请求，复用本线程池里的连接。返回 (status, raw)。"""
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"URL 缺少主机：{url}")
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    key = (scheme, host, port, round(float(timeout), 2))
    for attempt in (0, 1):
        conn = _pool_take(key)
        reused = conn is not None
        if conn is None:
            conn = _new_conn(scheme, host, port, timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_BODY)
            code = int(resp.status)
            # body 没读完/对端要关：这条连接不能再复用，否则下一次 request
            # 会读到上一条的残留响应。
            leftover = (getattr(resp, "length", None) or 0) - len(raw)
            if resp.will_close or leftover > 0:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            else:
                _pool_put(key, conn)
            return code, raw
        except Exception:  # noqa: BLE001
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            # 复用的长连接可能已被对端静默关闭（重启/空闲超时）：换一条新
            # 连接重试一次。新连接也失败，就是真不可达，交给调用方处理。
            if reused and attempt == 0:
                continue
            raise
    raise RuntimeError("unreachable")


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
    try:
        code, raw = _http_round_trip(url, method, data, headers,
                                     max(0.1, float(timeout)))
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
        self.control_adapter = None
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
        # 墓碑（T-0327）: node_id -> 删除时刻 epoch。被显式 leave 掉的节点，
        # 或长期 dead 后被本机清理的节点，转述/心跳再把它带回来时一律拒收。
        # OSPF 里没有这个需求 —— 邻居表删了就删了，只有本机 Hello 能重建；
        # 我们是 gossip 拓扑，必须自己记住「谁已经被删过」，否则删一个被转述
        # 灌回一个，永远是打地鼠。
        self._tombstones: dict[str, float] = {}
        self.events: deque = deque(maxlen=EVENT_LIMIT)
        self._event_ids: set[str] = set()
        self.event_seq = 0
        self.progress: dict[str, dict] = {}
        self.leases: dict[str, dict] = {}
        self.chat: deque = deque(maxlen=CHAT_LIMIT)
        self._chat_ids: set[str] = set()
        self._chat_last_epoch = 0.0
        # 留言同步的活跃期：有新留言（本地发出 / 从主节点拉到）就刷新，
        # 空闲时把 2s 一拍降频到 5s。聊天是低频人类文本，没必要常年 2s 轮询。
        self._chat_active_until = 0.0
        self.generation = 0
        self.updated_at = _iso()

        self._dirty = False
        self._last_persist = 0.0
        # 上次兜底全量重传 progress/leases 的时刻（见 _heartbeat_once）。
        # 0 表示「还没重传过」，第一次心跳必定带全量，新节点能立刻收敛。
        self._broadcast_at = 0.0
        self._last_election = 0.0
        # 只有「主控本人发来的 leader 心跳」才刷新这个时间戳。跟随者之间转述
        # leader 记录不算数 —— 否则每台都以为别人见过主控，幽灵主控的租约
        # 被互相喂成永不过期（2026-11-09 的 .103 死循环）。
        self._last_leader_heartbeat_at = 0.0
        self._last_forged_log = 0.0
        self._leader_lost_at: float | None = None
        self._leader_fail_at: float | None = None
        self._election_inflight = False
        # 选举失败后的随机退避（CSMA/CD）：连续失败越多，等得越久；成功当选
        # 或收到合法主控心跳后清零。没有它就会 3 秒一轮连打、term 膨胀。
        self._election_failures = 0
        self._next_election_at = 0.0
        self._last_election_lock_wait = 0.0
        # 启动宽限：配了 seed / 广播的节点刚起来时，先给发现+注册留一个窗口，
        # 别让高优先级节点在「还没认识在位主控」时立刻 0 延迟抢主（会打断在位主控）。
        self._startup_grace_until = 0.0
        # health 快照缓存：HTTP 线程只读它，绝不等集群主锁（长 git/store 操作
        # 持锁时也能秒回）。由维护循环/独立线程低频刷新。
        self._health_cache: dict = {}
        self._health_cache_at = 0.0
        self._store_health_cache: dict = {"enabled": False}
        # store 位点缓存（T-0327）：commit_seq() 要拿 store._lock，而
        # _ingest_store_cards() 会长时间持有它。持主锁的代码只读这个缓存值，
        # 绝不跨到 store 锁上去排队 —— 两把锁互相等就是「整集群 20s 无响应」。
        self._commit_seq_cache = 0
        self._store_reconcile_busy = False
        self._peer_errors: dict[str, float] = {}
        self._expired_reported: set[str] = set()
        self._last_store_sync = 0.0
        self._last_store_reconcile = 0.0
        # 对外地址自校准：候选地址要连续两次探测一致才切换（多网卡/唤醒抖动）。
        self._last_advertise_check = _now()
        self._advertise_candidate = ""
        self._advertise_candidate_hits = 0
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

    def _store_seq(self) -> int:
        """持锁路径专用：读缓存的本机 store 位点，绝不碰 store._lock（T-0327）。"""
        return int(self._commit_seq_cache or 0)

    def _refresh_store_seq(self) -> int:
        """**锁外**刷新 _commit_seq_cache；会短暂等 store 锁，别在持主锁时调。"""
        if self.store is None:
            self._commit_seq_cache = 0
            return 0
        try:
            seq = int(self.store.commit_seq() or 0)
        except Exception:  # noqa: BLE001  测试桩/库损坏都不能拖垮主循环
            seq = int(self._commit_seq_cache or 0)
        self._commit_seq_cache = seq
        return seq

    # -- 生命周期 ---------------------------------------------------------- #
    @contextmanager
    def _timed_lock(self):
        """带等待耗时的集群主锁，用于把「锁被长操作占住」写进日志。"""
        started = _now()
        self.lock.acquire()
        try:
            yield _now() - started
        finally:
            self.lock.release()

    @property
    def url(self) -> str:
        return _base_url(self.advertise_host or _guess_lan_ip(), self.port)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return self._stop.wait(timeout)

    # -- 控制面（D14：只有 serve 入口开启） -------------------------------- #
    def _start_control_plane(self) -> None:
        """按配置装配控制面适配器；失败只记日志，不拖垮集群启动。"""
        if not self.config.control_plane or self.control_adapter is not None:
            return
        try:
            from hive_control.adapter import ClusterControlAdapter
            self.control_adapter = ClusterControlAdapter(
                self, limits=self.config.control_limits)
            self.control_adapter.start()
            _log("control: 控制面适配器已启动")
        except Exception as exc:  # noqa: BLE001  控制面是可选件
            self.control_adapter = None
            _log(f"control: 启动失败（忽略）: {exc}")

    def _stop_control_plane(self) -> None:
        """停止控制面；Cluster.stop() 最先调用它（约定 §3.10）。"""
        adapter, self.control_adapter = self.control_adapter, None
        if adapter is None:
            return
        try:
            adapter.stop()
        except Exception:  # noqa: BLE001  停止路径尽力而为
            pass

    def control_status(self) -> dict:
        """GET /api/cluster/status 的 control 段。"""
        if self.control_adapter is None:
            return {"enabled": False, "running": False, "plane_available": False}
        try:
            return self.control_adapter.status()
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "running": False,
                    "error": f"{type(exc).__name__}: {exc}"}

    def control_request_pull(self, body: dict) -> tuple[int, dict]:
        """POST /api/cluster/pull。"""
        if self.control_adapter is None:
            return 503, {"ok": False,
                         "error": "控制面未启用（仅 hive cluster serve 开启）"}
        return self.control_adapter.request_pull(body)

    def control_pull_status(self, request_id: str) -> tuple[int, dict]:
        """GET /api/cluster/pull/status?id=...。"""
        if self.control_adapter is None:
            return 503, {"ok": False,
                         "error": "控制面未启用（仅 hive cluster serve 开启）"}
        return self.control_adapter.pull_status(request_id)

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
        self._start_control_plane()
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
                             (self._loop_health_cache, "hive-cluster-health"),
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

    def _schedule_store_reconcile(self) -> None:
        """把 store 补卡挪到后台线程，别让 git 卡面读取拖住维护循环。

        维护循环同时负责心跳/选举；以前 _maybe_reconcile_store_cards() 直接
        在这里跑 `git`，一次慢 fetch 就能让选举停摆几十秒，且期间无人续租。
        """
        if self.store is None or self._store_reconcile_busy:
            return
        if self.role != "leader":
            return
        if _now() - self._last_store_reconcile < STORE_RECONCILE_S:
            return
        self._store_reconcile_busy = True

        def _worker():
            try:
                self._maybe_reconcile_store_cards()
            except Exception as exc:  # noqa: BLE001
                _log(f"store: 后台补卡失败（忽略）: {exc}")
            finally:
                self._store_reconcile_busy = False

        threading.Thread(target=_worker, name="hive-store-reconcile-cards",
                         daemon=True).start()

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
        self._stop_control_plane()   # 约定 §3.10：先停控制面
        try:
            if self.role == "leader":
                self._resign_peers("shutdown")
            # 优雅退出要显式 leave：把本机从同伴的成员表里删掉，quorum 立刻
            # 回落到剩余节点。否则「停止一台机器」要等 membership_ttl（默认
            # 5 分钟）才允许剩下那台当选，现场表现就是「停了 Mac，fb98 叫不到主」。
            self._leave_peers()
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
        # 停机前强制把最新状态同步写下去（T-0327）：异步写盘可能还在队列里，
        # 不在这里等一次，进程退出后 state.json 会停在旧任期上，`cluster
        # status` 离线视图就少一次选举记录。
        try:
            self._persist(force=True)
        except Exception:  # noqa: BLE001
            pass
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

        # 墓碑：被删过的身份在保留期内不许复活（重启后也要记住）。
        tombs = raw.get("tombstones")
        if isinstance(tombs, dict):
            horizon = _now() - TOMBSTONE_TTL_S
            for nid, at in tombs.items():
                try:
                    when = float(at)
                except (TypeError, ValueError):
                    continue
                if when >= horizon:
                    self._tombstones[str(nid)] = when
        # 与墓碑冲突的记录一律不信
        for nid in list(self.nodes):
            if self._tombstoned_locked(nid):
                self.nodes.pop(nid, None)

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

        # 重启后**一律不继承**磁盘里的主控身份。真主控每秒发心跳，1 秒内就会
        # 重新确立；而磁盘里若残留「查无此人」的幽灵（旧机器名、改过名的实例、
        # 或 follower 互相喂出来的租约），原样读回就会让心跳继续给它续命，
        # 谁都不发起选举 —— 这正是「改了盘、重启又复发」的根因（见
        # 2026-11-09 幽灵 leader 死循环）。宁可短暂无主，也不要继承可疑身份。
        if self.leader_id or self.leader_lease_until:
            _log(f"集群状态加载：丢弃旧主控指向 {self.leader_id or '-'}，"
                 f"等待主控心跳/重新选举确认")
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

    def _mark_touch_locked(self) -> None:
        """只「碰一下」：唤醒长轮询，但不推进 generation、不置脏。

        心跳和同伴快照里的 last_seen 刷新只说明「它还活着」，不是「状态
        变了」。原先这条路径也走 _mark_dirty_locked，后果是每拍 generation
        +1：对端永远拿不到相等的 gen，心跳响应次次退化成全量；同时每秒把
        565KB 的 state.json 重写一遍（实测空闲期磁盘写 1.8MB/s）。
        """
        try:
            self.cond.notify_all()
        except RuntimeError:
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
            "tombstones": dict(self._tombstones),
            # 磁盘只留最近一批事件：events 本身是 deque(EVENT_LIMIT)，
            # 再整份塞进 snapshot 会把 state.json 直接抬高一倍。
            "events": list(self.events)[-EVENT_PUSH_LIMIT:],
            "event_seq": self.event_seq,
            "progress": copy.deepcopy(self.progress),
            "leases": copy.deepcopy(self.leases),
            "generation": self.generation,
            "updated_at": self.updated_at,
            # 进程没跑时 `hive cluster status` 读 state.json 也能拿到和 HTTP 一样的视图
            # snapshot 不带 events：上面已经有一份了，重复序列化 135KB 纯浪费。
            "snapshot": self._snapshot_locked(include_events=False),
            "saved_at": _iso(),
        }

    def _persist_locked(self) -> None:
        """持主锁路径：**只打脏、绝不写盘**（T-0327）。

        原先这里在 self.lock 里直接 _write_json：state.json 实测 521KB，
        一次写入（json.dumps + 临时文件 + os.replace）40ms 起步，ZFS 上还会
        撞 zil_commit 的 fsync。选举路径（_start_election 持锁调本函数）
        一次就占住主锁 40ms+，心跳与 HTTP 全排队。

        真正的落盘由 _persist() 完成：锁内只取快照，锁外写文件。
        调用方语义不变 —— 状态此刻起已标记为待落盘。
        """
        self._dirty = True

    def _persist(self, force: bool = False) -> None:
        """落盘：锁内只取快照，锁外写文件。

        force=True 用于停机等「必须写下去」的时刻，同步写完。
        默认路径按 PERSIST_INTERVAL_S 节流，且只在维护循环里调用 —— 不额外开
        线程，避免写盘线程和时序敏感的选举逻辑抢 GIL。

        节流从 1s 调到 5s：state.json 实测 565KB，原先 1s 一次的全量
        json.dumps + 临时文件 + os.replace 让空闲期磁盘稳定写到 1.8MB/s。
        """
        with self.lock:
            if not force and not self._dirty:
                return
            if not force and _now() - self._last_persist < PERSIST_INTERVAL_S:
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
        """本机自述记录：心跳 body 与节点表自身条目的模板。

        `last_seen` 必须带上。leader 收不到自己的心跳，`_update_node_locked
        (touch=True)` 永远轮不到本机，若这里也不带，`state.json` 里本机记录
        就永远停在启动时刻，看板/体检读盘会把活着的主节点判成失联（T-0297）。
        """
        now_epoch = _now()
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
            # 链路速率（Mbit/s），priority 相等时的第二权重（T-0328）。
            "link_speed": int(self.config.link_speed),
            "last_store_seq": self._store_seq(),
            "hive_dir": str(self.config.hive_dir),
            # 本机此刻必然活着：这是「自身记录」而不是从别处收来的观测。
            "last_seen": _iso(now_epoch),
            "last_seen_epoch": now_epoch,
        }

    def _put_self_node(self) -> None:
        with self.lock:
            self._put_self_node_locked()

    def _put_self_node_locked(self) -> None:
        # `_self_node()` 已带 last_seen/last_seen_epoch（now），不要再覆盖成
        # 第二份时间戳 —— 单一时钟源，磁盘与 API 才不会分叉。
        self.nodes[self.node_id] = self._self_node()
        self._mark_dirty_locked()

    def _touch_self_last_seen_locked(self, now_epoch: float | None = None) -> None:
        """刷新节点表里本机记录的 `last_seen`（维护循环每 tick 调用）。

        为什么必须落盘刷新：`_update_node_locked(..., touch=True)` 只在对端给
        本机发心跳/快照时执行，leader 收不到自己的心跳，于是本机记录的时间戳
        停在启动时刻；`/api/cluster/state` 靠 `_public_node_locked` 对 self 回填
        `now` 看起来正常，但 `state.json` 上那个旧值会被看板/体检当成「主节点
        长期失联」（T-0287/T-0288 报出的 leader_record_stale）。

        只动时间戳，**不**改 `_node_status_locked()` 的判定，也**不**推进
        generation：长轮询等的是真实状态变化，不是「本机还活着」这件事
        （同 `_loop_heartbeat` 里 leader 租约的取舍）。维护循环 5Hz，写盘由
        `_persist()` 的 1s 节流兜住，不会把磁盘打满。
        """
        now_epoch = _now() if now_epoch is None else now_epoch
        rec = self.nodes.get(self.node_id)
        if rec is None:
            self._put_self_node_locked()
            return
        rec["last_seen"] = _iso(now_epoch)
        rec["last_seen_epoch"] = now_epoch
        # 直接置脏（而非 _mark_dirty_locked）：只让下一次 _persist 落盘，
        # 不推进 generation、不唤醒长轮询。
        self._dirty = True

    def _refresh_advertise(self, guess: str | None = None) -> bool:
        """校准对外地址；返回是否真的发生了切换。

        探测放在锁外：`_guess_lan_ip()` 在无路由时会走 DNS 兜底，不该占着
        集群锁。显式指定 advertise 的节点直接跳过（人定的地址不自动改）。
        """
        if self.config.advertise_explicit:
            return False
        if guess is None:
            guess = _guess_lan_ip()
        with self.lock:
            return self._refresh_advertise_locked(guess, _now())

    def _refresh_advertise_locked(self, guess: str, now_epoch: float) -> bool:
        """把新地址探测收成一条判定：连续两次一致才切换。

        只切一次、并留 `advertise_changed` 事件：多网卡机器上地址可能来回跳，
        没有去抖就会把节点表刷成「一秒一个新地址」，同伴反而更找不到本机。
        """
        if self.config.advertise_explicit or not guess:
            return False
        # 断网时 _guess_lan_ip() 会退到 127.0.0.1；把可达地址换成回环只会让
        # 同伴更连不上本机，宁可保留旧值等网络恢复。
        if (guess.startswith("127.") and self.advertise_host
                and not self.advertise_host.startswith("127.")):
            return False
        if guess == self.advertise_host:
            # 回到当前地址：清掉候选，避免「A→B→A」被误判成连续命中。
            self._advertise_candidate = ""
            self._advertise_candidate_hits = 0
            return False
        if guess != self._advertise_candidate:
            self._advertise_candidate = guess
            self._advertise_candidate_hits = 1
            return False
        self._advertise_candidate_hits += 1
        if self._advertise_candidate_hits < 2:
            return False
        old = self.advertise_host
        self.advertise_host = guess
        self._advertise_candidate = ""
        self._advertise_candidate_hits = 0
        rec = self.nodes.get(self.node_id)
        if rec is None:
            self._put_self_node_locked()
        else:
            rec["host"] = guess
            rec["url"] = self.url
            rec["last_seen"] = _iso(now_epoch)
            rec["last_seen_epoch"] = now_epoch
            self._mark_dirty_locked()
        self._emit_locked(
            "advertise_changed", node_id=self.node_id,
            message=f"对外地址 {old or '-'} → {guess}")
        return True

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
        # alive 窗口不能比「最慢一次心跳」还窄：空闲期 5s 一拍、peer_timeout
        # 4s 时，节点会在 alive/suspect 之间每拍抖一次，维护循环就会不停
        # 发 node_up 事件、推进 generation（实测每 2-3 秒一次）。
        dead_timeout = max(0.05, float(self.config.dead_timeout))
        alive_window = max(float(self.config.peer_timeout),
                           2.0 * float(self.config.idle_heartbeat_interval))
        # 但 alive 窗口必须给 dead 留缓冲，否则一次丢包就直接判死。
        alive_window = min(alive_window, dead_timeout * 0.75)
        alive_window = max(alive_window, float(self.config.peer_timeout))
        age = (now_epoch or _now()) - float(rec.get("last_seen_epoch") or 0.0)
        if age <= alive_window:
            return "alive"
        if age <= dead_timeout:
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
                            joined: bool = False,
                            trust_remote_seen: bool = True) -> bool:
        """合入一个节点记录。低化身号直接丢弃；返回是否写入。"""
        if not isinstance(rec, dict) or not rec.get("id"):
            return False
        nid = str(rec["id"])
        if nid == self.node_id:
            return False
        # 墓碑优先于一切：被显式删掉的身份，心跳/转述都不能把它带回来。
        # 只有在保留期过后（_tombstoned_locked 会自动清过期条目）才允许重入。
        if self._tombstoned_locked(nid):
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
            # 通常取较新的那个，否则一次状态合并会把刚确认的 alive 又打回 dead。
            old_seen = float((known or {}).get("last_seen_epoch") or 0.0)
            if trust_remote_seen:
                new_seen = max(old_seen, float(incoming.get("last_seen_epoch") or 0.0))
            else:
                # 跟随者转述的存活时间**不能当证据**：两台 follower 互相把
                # 同一台幽灵的 last_seen 刷成「刚刚」，它就永远 alive、永远占
                # 一票，quorum 被抬高，选举反复凑不够多数。存活只认本机直接
                # 观测（_peer_heartbeat 收到回复时会刷新）。
                #
                # T-0327 追加修复：**本机从没见过的新节点**，绝不能拿「此刻」
                # 给它垫时间。原先 `old_seen or _now()` 在 known is None 时
                # 等于「凭空当我刚见过它」—— 现场就是删掉 probe / DESKTOP-2NRDKF0
                # 之后，被同伴转述又灌回来，last_seen 被写成删除那一刻，于是
                # 它们稳稳占住票池 300s、把 quorum 抬到 4，而真机只有 3 台，
                # 连续 382 次「未获选举多数」，直到 TTL 过期才突然选出主。
                # OSPF 的邻居表根本不接受第三方转述，只由本机 Hello 维护；
                # 这里同理：没被本机亲手确认过，就以「从没见过」入表（时间 0），
                # 它既不进票池也不影响 quorum，等它自己发心跳或本机探测成功
                # 再成为真邻居。
                #
                # 注意 **不能** 采信转述方自带的 last_seen —— 那仍然是二手证据：
                # 同伴把「它上次见到 ghost 的时间」抄给我们，ghost 照样能凭这个
                # 时间戳赖在票池里。时间戳只由本机直接观测写入（touch=True 或
                # _peer_heartbeat 收到回复），这里一律保持原值 / 归零。
                new_seen = old_seen
            merged["last_seen_epoch"] = new_seen
            merged["last_seen"] = _iso(new_seen) if new_seen else ""
        merged["id"] = nid
        if joined or known is None:
            merged["joined_at"] = merged.get("joined_at") or _iso()
        prev_sig = {k: v for k, v in (known or {}).items()
                    if k not in _NODE_VOLATILE_KEYS}
        cur_sig = {k: v for k, v in merged.items()
                   if k not in _NODE_VOLATILE_KEYS}
        self.nodes[nid] = merged
        if known is None:
            self._emit_locked("node_join", node_id=nid,
                              message=f"{merged.get('name') or nid} 加入")
        if known is not None and cur_sig == prev_sig:
            # 只有 last_seen 被刷新：这不是状态变更，绝不能推进 generation。
            self._mark_touch_locked()
        else:
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

    def _heartbeat_nodes_locked(self) -> list[dict]:
        """心跳响应的节点表：去掉展示用的大块静态字段。

        现场单节点 `agents` 就有 ~1.6KB（agent 列表），7 个节点 ~11KB；连
        `hive_dir`/`hostname`/`pid`/时间戳一起，整份 nodes 常年 12KB+，而
        心跳路径没有任何消费者读这些字段（看板走 `/api/cluster/state` 的
        `_snapshot_locked`，仍是完整记录）。合并方 `_update_node_locked`
        用 `dict.update` 语义，缺字段会保留本地已有值，不会抹掉数据。
        """
        out = []
        for n in self.nodes.values():
            rec = self._public_node_locked(n)
            for key in HEARTBEAT_NODE_SKIP_KEYS:
                rec.pop(key, None)
            out.append(rec)
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
        # 只有主控**本人**才有权对外声明「我的租约到 X」。
        #
        # follower 照着 `self.leader_lease_until` 转述，会让两个 follower
        # 互相把对方的本地续期时间当成主控租约回灌：A 说「主控到 t+5」，
        # B 收到后 `min(远端, now+lease)` 取到 t+5，下一拍 B 又把自己的
        # t+5 说给 A…… 主控真的死掉时，这个值不再前进却也不后退，
        # 于是 `now > leader_lease_until` 永远不成立，**集群永久无主**。
        # 现场（2026-11-11，0.16.03 心跳降频后）：杀掉 3 节点里的主控，
        # 剩下两台 10 秒后仍认为它活着，从不发起选举。
        #
        # 修法：follower 只转述「当初听主控说的那个绝对时间」，
        # 不掺自己的续期；主控本人则照常下发自己的租约。
        if str(self.leader_id) == str(self.node_id):
            out["lease_until"] = (_iso(self.leader_lease_until)
                                  if self.leader_lease_until else "")
            out["lease_until_epoch"] = self.leader_lease_until
        else:
            # 保持节点记录里的原值（由 _adopt_leader_locked 写入的绝对时间）。
            out["lease_until"] = str(rec.get("lease_until") or "")
            out["lease_until_epoch"] = float(rec.get("lease_until_epoch") or 0.0)
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
            self._chat_active_until = _now() + CHAT_SYNC_ACTIVE_WINDOW_S
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
                # 刚拉到新留言：进入活跃期，接下来 30s 维持 2s 一拍，
                # 让「有人回话」时秒级可见；之后自动退回 5s 空闲档。
                self._chat_active_until = _now() + CHAT_SYNC_ACTIVE_WINDOW_S
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

    def _chat_sync_interval(self) -> float:
        with self.lock:
            return (CHAT_SYNC_FAST_S if _now() < self._chat_active_until
                    else CHAT_SYNC_IDLE_S)

    def _loop_chat_sync(self) -> None:
        """留言不要等 git fetch：活跃期 2s 增量拉一次，空闲退避到 5s。

        留言是低频人类文本，常驻集群却 7×24 小时 2s 一拍（`since` 请求 +
        空 `messages` 响应 ≈ 几百字节/次 × 3 台 follower）也是一笔常年的
        固定开销。有新留言时自动回到 2s，不牺牲「秒级可见」的体验。
        测试用 `heartbeat_interval=0.08` 不影响这里，如需覆盖请直接改常量。
        """
        while not self._stop.wait(self._chat_sync_interval()):
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
            "voters": len(self._voter_ids_locked()),
            "membership_ttl_s": round(float(self.config.membership_ttl), 1),
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()

    def _build_health_locked(self) -> dict:
        """在持锁状态下拼 health。只做内存计算，绝不碰 sqlite/git 等慢资源。"""
        now_epoch = _now()
        alive = sum(1 for rec in self.nodes.values()
                    if self._node_status_locked(rec, now_epoch) == "alive")
        me = self.nodes.get(self.node_id) or {}
        # 不用 _leader_record_locked：它对本机 leader 会走 _self_node() 读 agents 目录。
        leader_rec = self.nodes.get(self.leader_id) if self.leader_id else None
        leader = None
        if leader_rec:
            leader = {"id": leader_rec.get("id"),
                      "name": leader_rec.get("name"),
                      "url": leader_rec.get("url")}
        return {
            "ok": True,
            "node": {"id": self.node_id,
                     "name": str(me.get("name") or self.config.name),
                     "url": self.url},
            "role": self.role, "term": self.term,
            "leader": ({"id": leader.get("id"), "name": leader.get("name"),
                        "url": leader.get("url")} if leader else None),
            "leader_lease_until_epoch": float(self.leader_lease_until or 0.0),
            "cluster_id": self.config.cluster_id, "protocol": PROTOCOL,
            "uptime_s": round(now_epoch - self.started_epoch, 1),
            "peers": alive, "quorum": self._quorum_locked(),
            "voters": len(self._voter_ids_locked()),
            "membership_ttl_s": round(float(self.config.membership_ttl), 1),
            "mcp": (self.mcp_service.status()
                    if self.mcp_service is not None else {"enabled": False}),
            # store.summary() 会碰 sqlite；用后台线程刷出来的缓存，避免 health
            # 与 store 导入/压缩抢锁（现场 5s 超时就是这么来的）。
            "store": copy.deepcopy(self._store_health_cache),
            # archive.status() 会查 pending_results（sqlite），同样只给运行态。
            "archive": ({"running": bool(self.archive_agent._thread
                                         and self.archive_agent._thread.is_alive())}
                        if self.archive_agent is not None else {"running": False}),
            "coordinator": (self.coordinator_agent.status()
                            if self.coordinator_agent is not None
                            else {"enabled": False}),
        }

    def health_payload(self) -> dict:
        """无阻塞 health 快照：拿不到集群主锁就返回上一份缓存。

        长操作（git sync / store 导入）持锁时，HTTP 线程也必须能秒回，
        否则主控本人看起来像「死」的，同伴会提前发起选举。
        """
        if self.lock.acquire(timeout=0.15):
            try:
                data = self._build_health_locked()
                self._health_cache = data
                self._health_cache_at = _now()
                return copy.deepcopy(data)
            finally:
                self.lock.release()
        cached = self._health_cache
        if cached:
            out = dict(cached)
            out["stale"] = True
            return out
        return {"ok": True,
                "node": {"id": self.node_id, "name": self.config.name,
                         "url": self.url},
                "role": self.role, "term": self.term, "leader": None,
                "leader_lease_until_epoch": float(self.leader_lease_until or 0.0),
                "cluster_id": self.config.cluster_id, "protocol": PROTOCOL,
                "uptime_s": round(_now() - self.started_epoch, 1),
                "peers": 0, "quorum": 1, "voters": 1,
                "mcp": {"enabled": self.mcp_service is not None},
                "store": {"enabled": self.store is not None},
                "archive": {"running": self.archive_agent is not None},
                "coordinator": {"enabled": self.coordinator_agent is not None},
                "stale": True}

    def _loop_health_cache(self) -> None:
        """低频刷新 store 摘要与位点缓存。sqlite 再慢也只拖住这个线程。

        顺带刷新 _commit_seq_cache（T-0327）：持主锁的路径（_self_node /
        心跳响应 / 选票闸门）都从这里读位点，绝不自己去抢 store._lock ——
        两把锁互相等正是「整集群 20s 无响应」的根因。
        """
        while not self._stop.wait(1.0):
            try:
                self._store_health_cache = (
                    self.store.summary() if self.store is not None
                    else {"enabled": False})
            except Exception as exc:  # noqa: BLE001
                self._store_health_cache = {
                    "enabled": self.store is not None, "error": str(exc)}
            if self.store is not None:
                self._refresh_store_seq()

    def _quorum_locked(self) -> int:
        """多数门槛按**已知可参选成员**算，而不是「此刻还活着」的节点。

        T-0299 的核心：peer_timeout（默认 4s）一到就把同伴踢出票池，任何一次
        git/store 长操作都能让本机同时满足「无主」+「同伴全死」，于是 quorum
        退化成 1、单票自封。成员表用 membership_ttl（默认 5 分钟）兜底，
        短暂失联不再改变多数门槛；只有真正长期缺席的机器才会退出票池。

        T-0327 教训：这个「兜底」必须**只对真认识过的节点**生效。转述进来的
        陌生 id 若也进票池，门槛会被凭空抬高，而现场只有 3 台真机 → 永远
        凑不够多数。见 `_update_node_locked` 里 known is None 的处理。
        """
        voters = len(self._voter_ids_locked())
        if voters <= 1 or self.config.allow_single_vote:
            return 1
        return voters // 2 + 1

    def _tombstoned_locked(self, nid: str) -> bool:
        """该身份是否在墓碑保留期内（被删过，不许转述复活）。"""
        when = self._tombstones.get(str(nid))
        if when is None:
            return False
        if _now() - float(when) > TOMBSTONE_TTL_S:
            self._tombstones.pop(str(nid), None)
            return False
        return True

    def _tombstone_locked(self, nid: str, reason: str = "") -> None:
        """给一个身份立墓碑：从成员表移除，并禁止转述把它带回来。"""
        nid = str(nid or "")
        if not nid or nid == self.node_id:
            return
        self._tombstones[nid] = _now()
        removed = self.nodes.pop(nid, None)
        if self.leader_id == nid:
            self._clear_leader_locked(reason or "主节点被移除")
        if removed is not None:
            self._emit_locked(
                "node_tombstoned", node_id=nid,
                message=(f"{removed.get('name') or nid} 已移除"
                         + (f"：{reason}" if reason else "")))
        self._mark_dirty_locked()

    def _gc_tombstones_locked(self) -> None:
        """过期墓碑自动清理（否则 state.json 会无限长大）。"""
        horizon = _now() - TOMBSTONE_TTL_S
        for nid, when in list(self._tombstones.items()):
            if float(when) < horizon:
                self._tombstones.pop(nid, None)

    def _voter_ids_locked(self) -> list[str]:
        """票池：用于 quorum 与拉票。

        OSPF 语义（T-0327 的重要修正）：邻居「超时即出票池」。OSPF 里
        RouterDeadInterval 一到就把邻居从邻居表删掉，从此不参与任何计算；
        我们这里的对等物是 `dead_timeout`。

        T-0299 当初引入 membership_ttl 是为了防「一次 git/store 长操作让
        同伴全判死、quorum 塌成 1、单票自封」。那个诉求仍然成立，但实现方式
        错了：拿一个 5 分钟的窗口去兜，等于让**真正下线/从没见过的机器**也
        一直占着票位 —— 现场 3 台真机 + 2 条僵尸记录 → quorum 4 → 永远
        选不出主（连续 382 次未获多数），直到 TTL 到期才突然自愈。

        正确做法（照 OSPF）：票池只收「本机确实亲眼见过它活着」的节点，
        宽限期只覆盖 `suspect`（短暂失联），不覆盖长期 `dead`：
          * alive / suspect —— 算票（suspect 覆盖长操作导致的瞬时失联）；
          * dead 且仍在 membership_ttl 内 —— **不算票**；
          * 从没被本机确认过（last_seen 为 0）—— 不算票。
        """
        now_epoch = _now()
        ids = [self.node_id] if self.config.electable else []
        for nid, rec in self.nodes.items():
            if nid == self.node_id:
                continue
            if not rec.get("electable", True):
                continue
            if self._tombstoned_locked(nid):
                continue
            last_seen = float(rec.get("last_seen_epoch") or 0.0)
            if last_seen <= 0.0:
                # 从没被本机直接观测过：可能是转述来的陌生 id，不给票位。
                continue
            if self._node_status_locked(rec, now_epoch) in ("alive", "suspect"):
                ids.append(nid)
            # dead（超过 dead_timeout 没心跳）一律不给票位 —— 这就是 OSPF
            # 的 RouterDeadInterval 语义：邻居死了就不再参与 DR 计算。
        return ids

    def _reachable_voter_ids_locked(self) -> list[str]:
        """成员表里最近还见过的那些：优先级排序只认它们。

        dead 成员仍算 quorum（防止 1 票自封），但不该让一个永远连不上的高优先级
        节点把所有人无限期按住 —— 高优先级只在「它还活着/可疑」时享受 0 延迟。
        """
        ids = []
        for nid in self._voter_ids_locked():
            if nid == self.node_id:
                ids.append(nid)
                continue
            rec = self.nodes.get(nid) or {}
            if self._node_status_locked(rec) in ("alive", "suspect"):
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

    def _node_speed_locked(self, nid: str) -> int:
        """某个可参选节点的链路速率（Mbit/s）；0 = 未知。"""
        if nid == self.node_id:
            return max(0, int(self.config.link_speed or 0))
        rec = self.nodes.get(nid) or {}
        try:
            return max(0, int(rec.get("link_speed") or rec.get("speed") or 0))
        except (TypeError, ValueError):
            return 0

    def _rank_key_locked(self, nid: str) -> tuple:
        """主控竞争力排序键（越大越该当主）。T-0328，用户指定的四级链：

          1. preferred_leader（人工硬指定）—— 单独在 _election_delay_locked 里处理
          2. leader_priority：值大者胜（OSPF DR priority 语义）
          3. link_speed：优先级相等时，链路快的胜（带得动更多同步/心跳）
          4. node_id 字典序：全等时用它做**确定性**兜底

        第 4 条是关键：没有它，两台同优先级的机器会算出「一样强」，
        只能靠随机退避抢，撞车后又要重选一轮。有了 node_id 兜底，
        全集群对「谁更强」得出**同一个答案**，可以直接让位、无需竞争。

        注意第 4 项必须是**与 self 无关的纯函数**（这里用字符串本身），
        否则每台机器按自己视角算出不同键，"谁更强"就没有全局一致性了。
        """
        nid = str(nid)
        # node_id 越小越优先 —— 用取反的 ord 序列无法比较，改用「反转字符串」
        # 这种简单可靠的技巧不对；直接把「字典序小」编码成一个可比较的整数：
        # 用 hash 会引入碰撞与不可复现，所以最终改用元组末端放原串，
        # 比较时方向由调用方决定（见 _stronger_than_locked）。
        return (
            self._node_priority_locked(nid),
            self._node_speed_locked(nid),
        )

    def _stronger_than_locked(self, a: str, b: str) -> bool:
        """a 是否比 b 更有资格当主（全集群口径一致）。

        排序：priority（大胜） → link_speed（大胜） → node_id（**字典序小**胜）。
        node_id 比较放在最后且方向固定，保证所有节点对同一对候选得出同一答案。
        """
        ka = self._rank_key_locked(a)
        kb = self._rank_key_locked(b)
        if ka != kb:
            return ka > kb
        return str(a) < str(b)

    def _has_stronger_peer_locked(self) -> str:
        """返回比本机更强的†可达节点 id（没有则空串）。

        †「更强」= 按 _rank_key_locked 排序严格高于本机。只认可达（alive/suspect）
        的节点：对一台早已关机的机器让位，等于永久放弃主控。

        **T-0328 关键约束（non-preemptive，照 OSPF 的 DR 语义）**：
        只要已经有**在位的活主控**，本函数一律返回空 —— 更高优先级节点后加入
        不抢现任的位。OSPF 里 DR 一旦选出，即使来了优先级更高的路由器，在
        DR 失效（Hello 超时）之前也不会被抢占；否则每次开机/重启都抖一次主，
        集群根本没法用。用户原话：「竞争得主后，不手工关闭，几乎不会丢失主」。

        所以「择优」只作用于**无主时的竞争**，不作用于「推翻在任者」。
        """
        if (self.role != "leader"
                and self.leader_id
                and self.leader_id != self.node_id
                and self.leader_lease_until > _now()):
            # 有活主控在任：不预占。
            return ""
        best = ""
        for nid in self._reachable_voter_ids_locked():
            if nid == self.node_id:
                continue
            try:
                if not self._stronger_than_locked(nid, self.node_id):
                    continue
            except Exception:  # noqa: BLE001
                continue
            if not best or self._stronger_than_locked(nid, best):
                best = nid
        return best

    def _priority_zero_excludes_locked(self) -> bool:
        """priority 0 此刻是否意味着「禁赛」。

        OSPF 里 0 是**硬资格**（放弃 DR/BDR 竞选），不只等于「懒得先开口」：
        除了自己不发起选举，别的节点也不该把票投给它。唯一例外是全员都填 0，
        那时退化成人人 1（见 _election_delay_locked），0 就不再是禁赛标志，
        否则一个节点都不肯发起选举、集群永远没有主控。
        """
        voters = self._reachable_voter_ids_locked()
        return any(self._node_priority_locked(n) > 0 for n in voters)

    def _election_backoff_locked(self) -> float:
        """选举失败后的随机退避（CSMA/CD 的 collision backoff）。

        失败次数越多等得越久，上限约 4 个 election_min；乘 ±20% 抖动，
        避免所有失败节点在同一刻再次撞车。成功当选/收到主控心跳后清零。
        """
        failures = max(0, int(self._election_failures) - 1)
        base = max(self.config.election_min, MIN_ELECTION_STEP_S)
        cap = max(base * 4.0, 1.5)
        delay = min(base * (2 ** min(failures, 5)), cap)
        return delay * random.uniform(0.8, 1.2)

    def _election_delay_locked(self) -> float:
        """本机在「当前无主控」时该等多久才发起选举（CLAIM）。

        规则（T-0299，用户明确指定）：
          1. priority 不同：**值最高者 0 延迟直接 CLAIM**，其它节点让出窗口、
             不跟它竞争（只投票/承认）；
          2. priority 相同：随机退避（以太网 CSMA/CD），谁先发出 CLAIM 谁为主；
             撞车后由 _election_backoff_locked 再随机退一轮；
          3. 只有两人且僵持：同上，随机重试直到一方先 CLAIM；
          4. 0 仍是硬资格（弃权），全员 0 时退化成人人 1；
          5. preferred 语义不变，仍压过 priority。
        """
        voters = sorted(self._voter_ids_locked())
        if self.node_id not in voters:
            return float("inf")

        # 0 = 放弃竞选资格：只要集群里还有正常优先级的节点，就永不发起选举。
        if (self._node_priority_locked(self.node_id) <= 0
                and self._priority_zero_excludes_locked()):
            return float("inf")

    def _election_delay_locked(self) -> float:
        """本机在「当前无主控」时该等多久才发起选举（CLAIM）。

        T-0328 重写为**确定性择优**，取代原先「同优先级就随机抢」的 CSMA/CD。
        四级链（用户明确指定）：
          1. preferred_leader：人工硬指定，立刻可竞选；
          2. leader_priority：值大者优先（OSPF DR priority）；
          3. link_speed：优先级相等时链路快的优先；
          4. node_id：全等时字典序兜底 —— **保证全集群对「谁更强」算出同一
             答案**，于是不需要随机竞争、也就没有撞车与重选风暴。

        比本机更强的节点可达时，本机让位（给一个「它可能已经死了」的兜底
        窗口）；本机就是最强时才立刻 CLAIM。这样「依次启动」时先起来的
        弱节点会主动让位给后起来的强节点，而不是各抢各的。
        """
        voters = sorted(self._voter_ids_locked())
        if self.node_id not in voters:
            return float("inf")

        # 0 = 放弃竞选资格：只要集群里还有正常优先级的节点，就永不发起选举。
        if (self._node_priority_locked(self.node_id) <= 0
                and self._priority_zero_excludes_locked()):
            return float("inf")

        step = max(self.config.election_jitter, MIN_ELECTION_STEP_S)
        base = self.config.election_min
        reachable = self._reachable_voter_ids_locked()

        pref = self._preferred_leader_id_locked()
        if pref and self._node_priority_locked(pref) <= 0:
            # 被 preferred 指定的那台自己弃了权，还让全集群给它让路会导致
            # 没人肯发起选举 —— 视同没配这个偏好，退回按优先级来。
            pref = ""
        if pref == self.node_id:
            # 人工硬指定：立刻可竞选，压过一切优先级。
            return 0.0
        if pref and pref in voters and pref in reachable:
            # 其它节点让出一整个选举窗口，别跟被指定的那台同轮打平。
            return base + base

        stronger = self._has_stronger_peer_locked()
        if stronger:
            # 有更强的可达节点：让它先上。留一个兜底窗口 —— 万一它其实发不出
            # CLAIM（进程半死、网络单向），次强者仍能接手，不会永久无主。
            return base * 3 + random.uniform(0, step)

        # 本机就是最强的那个：立刻 CLAIM。
        # 单机也一样（本来它就该直接当主，不该等选举超时）。
        return 0.0

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
    def _merge_snapshot_locked(self, snap: dict, from_leader: bool = False,
                               sender_id: str = "") -> None:
        """合并远端快照。

        ``sender_id`` 是这份快照的签发者。主控记录**必须由那个主控本人签发**：
        一台机器可以自称 role=leader，却因为旧代码的转述把 ``leader_id`` 指向
        一台早已下线的机器（现场就是 .103）。这种「我是主，但我认为主是别人」
        的不一致状态曾让注册/双主路径把幽灵重新灌回刚清干净的磁盘。
        """
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
        # 主控身份**只能由主控本人确立**。跟随者的快照里也有 leader 字段，但那是
        # 它听说的视图（幽灵常借这条路径在 follower 之间扩散）：只拿来展示/发现，
        # 绝不写进 leader_id。真主控每秒给所有 peer 发心跳，不需要转述。
        leader = snap.get("leader")
        if from_leader and isinstance(leader, dict) and leader.get("id"):
            sid = str(sender_id or "")
            if sid and str(leader.get("id")) == sid:
                self._adopt_leader_locked(leader, term, refresh_lease=True)
        for rec in snap.get("nodes") or []:
            if isinstance(rec, dict):
                self._update_node_locked(rec, touch=False,
                                         trust_remote_seen=from_leader)
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
        # 不在这收尾处无条件 _mark_dirty_locked：合并里的每一条写入
        # （_merge_progress/_merge_lease/_update_node/_merge_event）已经各自
        # 按「是否真的变了」置脏。原先这里再补一刀，等于每次心跳都把
        # generation 推一格，全量响应与全量落盘永远停不下来。

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
        """采纳一条主控记录。

        ``refresh_lease=True`` 是**签发租约**的权力，只允许用在「主控本人
        发来的、已核验过身份的心跳」上（handle_heartbeat 的 leader 心跳通道）。
        从同伴快照/心跳回复里「听说」的主控一律走 False：只转述对方给出的
        绝对到期时间，绝不重新签发。否则两台跟随者互相转述就能把同一条
        now+lease 一跳一跳往后推，旧主死了租约也永不过期（幽灵 leader）。
        """
        if not isinstance(rec, dict) or not rec.get("id"):
            return False
        lid = str(rec["id"])
        if lid == self.node_id:
            return True
        # 续租（refresh_lease）每拍都会刷新 leader_lease_until，这属于「主控
        # 还活着」，不是「集群视图变了」：按关键字段判断，未变就只 touch。
        before = (self.leader_id, self.leader_url, self.term, self.role)
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
        # 只有真主控心跳才给这条记录 touch；转述不能把幽灵记录刷成 alive。
        self._update_node_locked(rec, touch=refresh_lease)
        changed = self.leader_id != lid
        self.leader_id = lid
        self.leader_url = str(rec.get("url") or self.leader_url or "")
        if refresh_lease:
            now = _now()
            self.leader_lease_until = now + self.config.leader_lease
            self._last_leader_heartbeat_at = now
        else:
            # 从同伴快照里「听说」的主节点不能当直接心跳：只转述对方记录的
            # 绝对到期时间。min(..., now+lease) 只做上限裁剪（防异常大值），
            # 不会把租约往后推 —— 每跳 re-sign 成 now+lease 正是死循环的根因。
            remote_lease = float(rec.get("lease_until_epoch") or 0.0)
            if remote_lease > 0:
                self.leader_lease_until = min(
                    remote_lease, _now() + self.config.leader_lease)
            elif not self.leader_lease_until:
                self.leader_lease_until = 0.0
        self._leader_lost_at = None
        self._leader_fail_at = None
        # 已经有合法主控了：清掉选举失败计数与退避，下一次丢主从正常窗口开始。
        self._election_failures = 0
        self._next_election_at = 0.0
        if changed:
            import traceback
            caller = traceback.extract_stack()[-2].name
            self._emit_locked("leader_seen", message=f"主节点 -> {rec.get('name') or lid}")
            # 排障用：主控指向变化时留下「谁把它写进来的」。幽灵 leader 反复
            # 复活时，一眼就能看出是心跳、注册还是同伴转述带回来的。
            _log(f"采纳主控 {rec.get('name') or lid}（来源 {caller}，"
                 f"term={term}，签租={refresh_lease}）")
        if (self.leader_id, self.leader_url, self.term, self.role) != before:
            self._mark_dirty_locked()
        else:
            # 单纯的续租：别把 generation 推一格，否则每次心跳都会触发一次
            # 全量状态广播 + 全量落盘。
            self._mark_touch_locked()
        return True

    def _clear_leader_locked(self, reason: str) -> None:
        if not self.leader_id:
            return
        old = self.leader_id
        self.leader_id = None
        self.leader_url = None
        self.leader_lease_until = 0.0
        # 主控指向变了，旧地址的「连续不可达」计时作废，别把新主也误清掉。
        self._leader_fail_at = None
        self._leader_lost_at = _now()
        self._emit_locked("leader_lost", message=f"{old} 失联：{reason}")
        self._mark_dirty_locked()

    def _become_leader_locked(self) -> None:
        old_role = self.role
        self.role = "leader"
        self.leader_id = self.node_id
        self.leader_url = self.url
        self.leader_lease_until = _now() + self.config.leader_lease
        self._last_leader_heartbeat_at = _now()
        self.voted_for = self.node_id
        self._leader_lost_at = None
        self._leader_fail_at = None
        self.event_seq = 0
        self._recover_leases_locked()
        if self.store is not None:
            # 本地 truncate + 通知同伴 truncate 都是 sqlite/HTTP 级别的慢操作，
            # 绝不能占着集群主锁做（T-0299：长操作持锁会让心跳/健康检查一起卡死）。
            threading.Thread(target=self._truncate_stores_after_win,
                             name="hive-store-truncate", daemon=True).start()
        self._emit_locked(
            "leader_elected",
            message=f"{self.config.name} 成为主节点，term={self.term}"
                    + ("" if old_role == "candidate" else f"（原 {old_role}）"))
        self._mark_dirty_locked()
        # 落后当选：先对齐 store 再服务写请求（与投票闸门配套，缺一半就是
        # 拿旧 commit_seq 起新 oplog 的分叉风险，见 T-0295）。
        self._arm_store_reconcile_locked()

    def _truncate_stores_after_win(self) -> None:
        if self.store is not None:
            try:
                self.store.truncate_uncommitted()
            except Exception:  # noqa: BLE001
                pass
        self._broadcast_truncate()

    def _arm_store_reconcile_locked(self) -> bool:
        """落后当选时把「写请求」关到 store 追平为止；返回是否进入对齐态。

        调用方持有 self.lock。没有 store、或本机不比投票人落后时立刻放行
        （纯 git 模式完全不受影响）。
        """
        self._store_serving_ready = True
        if self.store is None:
            return False
        want = int(self._store_lag_target or 0)
        have = self._store_seq()
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
                    if self._refresh_store_seq() >= want:
                        break
                if self.store is not None and self._refresh_store_seq() >= want:
                    break
                if self._stop.wait(0.5):
                    break
        finally:
            with self.lock:
                self._store_serving_ready = True
                have = self._store_seq()
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

    def _carrier_sense(self) -> bool:
        """发 CLAIM 前先「听」一下：有没有人在当主（以太网 carrier sense）。

        同优先级随机退避后，两个节点仍有概率几乎同时开口。先并发问一圈
        `/api/cluster/health`（0.5s 上限）：只要有人报出仍然有效的 leader，
        本轮就不发 CLAIM，让主控心跳把状态带回来。顺带把本机任期追到集群
        最高值，避免拿一个偏小的 term 去撞车。

        返回 True = 检测到在位主控，应放弃本轮选举。
        """
        with self.lock:
            voter_ids = set(self._voter_ids_locked())
            peers = [dict(rec) for nid, rec in self.nodes.items()
                     if nid != self.node_id and nid in voter_ids and rec.get("url")]
            if not peers:
                return False
        best_term = 0
        leader_seen = False
        with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
            futs = [pool.submit(http_json,
                                str(p["url"]).rstrip("/") + "/api/cluster/health",
                                "GET", None, 0.5, self.config.token)
                    for p in peers]
            for fut in as_completed(futs):
                try:
                    code, resp = fut.result()
                except Exception:  # noqa: BLE001  单个节点不通不能阻止选举
                    continue
                if code != 200 or not isinstance(resp, dict):
                    continue
                try:
                    best_term = max(best_term, int(resp.get("term") or 0))
                except (TypeError, ValueError):
                    pass
                leader = resp.get("leader")
                lease = float(resp.get("leader_lease_until_epoch") or 0.0)
                if (str(resp.get("role") or "") == "leader"
                        and isinstance(leader, dict) and leader.get("id")
                        and lease > _now()):
                    leader_seen = True
        if best_term > self.term:
            with self.lock:
                self._adopt_higher_term_locked(best_term)
        if leader_seen:
            # 已经有人当家：把本机的「主控丢失」计时重置，下一轮重新听。
            with self.lock:
                self._leader_lost_at = _now()
                self._next_election_at = _now() + self.config.election_min
            _log("选举前侦听到在位主控，本轮不发 CLAIM")
            return True
        return False

    def _start_election(self, force: bool = False) -> bool:
        """发起一轮选举（CLAIM）。force=True 是人工 promote 通道。

        返回是否真的发起了 —— 已经是主控 / 不可参选 / 被节流都返回 False，
        调用方可以据此给出准确提示，而不是假装成功。
        """
        if force:
            self._sync_term_from_peers()
        else:
            # 先做廉价闸门，别让失败退避期间每 0.2s 都去发一圈 health 探测。
            with self.lock:
                now_epoch = _now()
                if (self._last_election
                        and now_epoch - self._last_election
                        < self.config.election_min):
                    return False
                if now_epoch < self._next_election_at:
                    return False
                if now_epoch < self._startup_grace_until:
                    return False
            if self._carrier_sense():
                return False
        lock_wait_started = _now()
        with self.lock:
            lock_wait = _now() - lock_wait_started
            self._last_election_lock_wait = lock_wait
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
            # 已经认了别的主控且租约还有效：不要因为自己没收到心跳就抢跑。
            # 随机退避的节点可能比心跳晚一步醒来，这一句能挡住大量「刚选完又重选」。
            if (not force and self.leader_id
                    and self.leader_id != self.node_id
                    and self.leader_lease_until > _now()):
                return False
            # 失败后不要每 0.2s 就 term+1：至少隔一个选举超时再试。
            # 人工 promote 是明确意图，不受这个节流限制 —— 否则用户点了之后
            # 还要等一个 window 才生效，会以为「没反应」。
            if (not force and self._last_election
                    and _now() - self._last_election < self.config.election_min):
                return False
            if not force and _now() < self._next_election_at:
                return False
            if not force and _now() < self._startup_grace_until:
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
            term = self.term
            # 票池是「已知可参选成员」，不是「这 4 秒里还活着的节点」。
            voters = self._voter_ids_locked()
            quorum = self._quorum_locked()
            now_epoch = _now()
            peer_ages = {}
            for nid in voters:
                if nid == self.node_id:
                    continue
                rec = self.nodes.get(nid) or {}
                peer_ages[str(rec.get("name") or nid)] = round(
                    max(0.0, now_epoch - float(rec.get("last_seen_epoch") or 0.0)), 1)
            peers = [dict(rec) for nid, rec in self.nodes.items()
                     if nid != self.node_id and nid in voters and rec.get("url")]
            self._emit_locked(
                "election_started",
                message=(f"{self.config.name} 竞选 term={self.term}"
                         f"（voters={len(voters)} quorum={quorum} "
                         f"lock_wait={lock_wait * 1000:.0f}ms）"),
                data={"voters": len(voters), "quorum": quorum,
                      "peer_ages": peer_ages,
                      "lock_wait_ms": round(lock_wait * 1000, 1),
                      "force": bool(force)})
            self._persist_locked()
            body = {"term": term, "candidate": self._self_node(),
                    "last_event": self.events[-1]["id"] if self.events else "",
                    "last_store_seq": self._store_seq()}
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
                        peer = futs[fut]
                        if not isinstance(resp, dict):
                            continue
                        if resp.get("term") and int(resp["term"]) > self.term:
                            with self.lock:
                                self._adopt_higher_term_locked(int(resp["term"]),
                                                               resp.get("leader"))
                            continue
                        if code == 200 and resp.get("granted"):
                            votes.add(str(peer.get("id")))
                            try:
                                store_ahead = max(store_ahead,
                                                  int(resp.get("store_seq") or 0))
                            except (TypeError, ValueError):
                                pass
        finally:
            with self.lock:
                self._election_inflight = False
        backoff = 0.0
        with self.lock:
            # T-0328 的核心：**主必须选出来**。
            # 原先 `len(votes) >= quorum` 是 Raft 的一致性关卡；hive 的 store
            # 只是缓存、git 才是事实账，卡在这里只会让集群长期无主（现场连续
            # 382 次「未获多数」）。改为「择优当选」：
            #   * 没有更强的可达节点在等位时，本机直接上任（不必凑够多数）；
            #   * 有更强节点则让位 —— 但它只在**无主**时才让（见
            #     _has_stronger_peer_locked 的 non-preemptive 约束），
            #     在任主控不会被后来者顶掉；
            #   * 票数只用于记录与告警，不再是否决条件。
            #
            # 但有一个必须尊重的例外：**比我强的节点明确投了我**，说明它
            # 自己承认这轮该我当主（可能它正忙、priority 刚改、或它愿意让）。
            # 这时不能反过来以「它更强」为由让位 —— 否则会出现「2/2 全票
            # 当选却让位、然后谁都不当主」的怪圈（现场 T-0328 回归）。
            stronger_reachable = self._has_stronger_peer_locked()
            if stronger_reachable and stronger_reachable in votes:
                stronger_reachable = ""
            can_lead = not stronger_reachable
            is_single = len(voters) <= 1
            if (self.role == "candidate" and self.term == term
                    and (can_lead or is_single or force)
                    and not (self.leader_id
                             and self.leader_id != self.node_id
                             and self.leader_lease_until > _now())):
                # 落后当选也要赢，但赢下来先对齐 store 再服务写：不让新主控拿
                # 旧 commit_seq 起一段新 oplog（那才是「孤身称主」的真危险）。
                self._store_lag_target = max(
                    store_ahead,
                    self._store_seq())
                self._become_leader_locked()
                self._election_failures = 0
                self._next_election_at = 0.0
                self._persist_locked()
                became = True
            else:
                became = False
                self._election_failures += 1
                # 有更强节点时用短窗口：它才是正主，长退避只会让集群在
                # 无主状态多停一会儿。本机仍是兜底（它若发不出 CLAIM，
                # 我们会在短窗口后接手，绝不永久无主）。
                backoff = (self.config.election_min
                           if stronger_reachable
                           else self._election_backoff_locked())
                self._next_election_at = _now() + backoff
                self._emit_locked(
                    "election_yielded" if stronger_reachable else "election_failed",
                    message=(f"term={term} 让位给更强的 {stronger_reachable}，"
                             f"由它接手（本机 {len(votes)} 票）"
                             if stronger_reachable else
                             f"term={term} 未当选（{len(votes)}/{quorum} 票，"
                             f"voters={len(voters)}），{backoff:.1f}s 后重试"),
                    data={"votes": len(votes), "quorum": quorum,
                          "voters": len(voters), "backoff_s": round(backoff, 2),
                          "yielded_to": stronger_reachable})
                self._persist_locked()
        if became:
            _log(f"当选主节点：term={term}（{len(votes)}/{quorum} 票，"
                 f"voters={len(voters)}，lock_wait={lock_wait * 1000:.0f}ms）")
            self._heartbeat_once()
        else:
            if stronger_reachable:
                _log(f"让位给更强的 {stronger_reachable}：term={term}"
                     f"（本机 {len(votes)}/{quorum} 票，voters={len(voters)}）")
            else:
                _log(f"本轮未当选：term={term}（{len(votes)}/{quorum} 票，"
                     f"voters={len(voters)}，退避 {backoff:.1f}s）")
        return became

    def _adopt_higher_term_locked(self, term: int, leader=None) -> None:
        """只同步任期，不借机改主控指向。

        ``leader`` 参数是历史遗留：更高任期消息（vote 回复、peer 心跳回复）里
        带的 leader 只是对方「听说」的视图，采纳它正是幽灵扩散的一条路径。
        主控归属交给主控本人的心跳重新确立即可。
        """
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

    # -- 心跳 / 维护循环 ---------------------------------------------------- #
    def _has_active_work_locked(self) -> bool:
        """有在跑的活儿吗？判定只看内存里已有的租约/进度，不打任何 IO。

        0.16.05 修复：租约不能只看「到期时间在未来」。现场一条 T-0332 租约
        `lease_until_epoch` 比当前时间晚 6 小时（`claim` 一次给了 8 小时），
        `updated_epoch` 却是两小时前 —— 它让本函数恒为 True，0.16.03 的
        「空闲 5s 降频」永远不生效，心跳整年按 1s 跑。

        判据改成「还在续租/推进」：租约与进度都必须满足
        `updated_epoch >= now - ACTIVE_WORK_WINDOW_S` 才算活跃。真在干活时
        agent 会持续 `publish_progress` / `renew`，时间戳一直新鲜；没人续的
        远期租约不该把全网心跳钉死。
        """
        now_epoch = _now()
        horizon = now_epoch - ACTIVE_WORK_WINDOW_S
        for lease in self.leases.values():
            if lease.get("released"):
                continue
            if float(lease.get("lease_until_epoch") or 0.0) <= now_epoch:
                continue
            if float(lease.get("updated_epoch") or 0.0) >= horizon:
                return True
        settled = ("done", "completed", "finished", "failed", "cancelled",
                   "canceled", "blocked", "released")
        for item in self.progress.values():
            state = str(item.get("state") or "").strip().lower()
            if state and state not in settled:
                # 十分钟没推进的老进度不算「在跑」，否则会永远把心跳钉在 1s。
                if float(item.get("updated_epoch") or 0.0) >= horizon:
                    return True
        return False

    def _heartbeat_interval_now(self) -> float:
        """活跃期 1s 一拍，空闲期降频。空闲期仍每秒全量同步就是这次故障。"""
        with self.lock:
            active = self._has_active_work_locked()
        return (self.config.heartbeat_interval if active
                else self.config.idle_heartbeat_interval)

    def _loop_heartbeat(self) -> None:
        while not self._stop.wait(max(0.05, self._heartbeat_interval_now())):
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
            # 轻量心跳主干：不带大块状态。请求方用 `sd`（state digest）告诉
            # 应答方「我手上这份 progress+leases 是什么内容」，应答方只在两边
            # 摘要不同时才回全量。generation 是各节点本地计数器（现场四台
            # 607万/300万/1056万/42万），绝不能当跨机水位用。
            #
            # 事件只问「最近 EVENT_WINDOW_S 秒」的：各机 event_seq 是本地
            # 计数器、切主还会重置，根本对不上，不能用它。窗口跟着墙钟走，
            # 空闲期没有新事件时响应里 events 恒为空。
            sd = _state_digest(self.progress, self.leases)
            events_since = max(0.0, _now() - EVENT_WINDOW_S)
            if role == "leader":
                self.leader_lease_until = _now() + self.config.leader_lease
                body = {"node": self._self_node(), "term": self.term,
                        "leader": True, "leader_url": self.url,
                        "lease_until": _iso(self.leader_lease_until),
                        "sd": sd, "events_since": events_since}
                # 心跳更新 leader_lease_until 但不推进 generation：长轮询等的是
                # 真实状态变化，不是「主节点还活着」这件事。
            else:
                body = {"node": self._self_node(), "term": self.term,
                        "leader": False, "sd": sd,
                        "events_since": events_since}
            # 兜底：某一拍的全量包丢了、对端又恰好不在线时，每
            # FULL_BROADCAST_S 主动重传一次，避免永远停在旧视图上。
            if _now() - self._broadcast_at >= FULL_BROADCAST_S:
                body["progress"] = copy.deepcopy(self.progress)
                body["leases"] = copy.deepcopy(self.leases)
                self._broadcast_at = _now()
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
                    # 只有对端也自称主控（同任期双主）才走让位规则。跟随者
                    # 回复里的 leader 只是它听说的视图，不能把在位主控带偏。
                    if (_reply_is_from_leader(resp)
                            and int(resp.get("term") or 0) >= self.term):
                        leader = resp.get("leader")
                        # 双主让位也只认「对方本人签发的」主控记录；对方若自称
                        # 主控却把 leader_id 指向第三台（幽灵），一律不理。
                        peer_id = str(peer.get("id") or "")
                        if (isinstance(leader, dict) and peer_id
                                and str(leader.get("id") or "") == peer_id
                                and peer_id != self.node_id):
                            self._adopt_leader_locked(
                                leader, int(resp.get("term") or 0),
                                refresh_lease=True)
                    if int(resp.get("term") or 0) > self.term:
                        self._adopt_higher_term_locked(int(resp["term"]), resp.get("leader"))
                    self._mark_touch_locked()

    def _follower_heartbeat(self, leader_url: str, body: dict) -> None:
        code, resp = http_json(str(leader_url).rstrip("/") + "/api/cluster/heartbeat",
                               "POST", body, 1.0, self.config.token)
        with self._timed_lock() as lock_wait:
            if isinstance(resp, dict):
                self._leader_fail_at = None
                leader = resp.get("leader")
                # 关键防线：leader_url 指向的那台未必真是主控（历史上它可能是
                # 旧机器、或自己也是 follower）。只有它明确自报 role=leader、
                # 且它认为的主控就是它自己，才把这份回复当主控心跳续租。
                # 否则一律只转述租约，绝不能 now+lease 重新签发 —— 这曾经让
                # 几台 follower 互相把 .103 的幽灵租约喂成永不过期。
                # 老版本不回 node_id：退而用「我们请求的那个 leader_id」当
                # 期望签发者（leader_url 本来就是按它拼出来的）。
                sender_id = str(resp.get("node_id") or self.leader_id or "")
                verified = (_reply_is_from_leader(resp)
                            and isinstance(leader, dict)
                            and bool(sender_id)
                            and str(leader.get("id") or "") == sender_id)
                # verified 时由 _merge_snapshot_locked 统一采纳；verified=False
                # 时连 leader_id 都不改，只同步节点表/进度/事件。
                self._merge_snapshot_locked(resp, from_leader=verified,
                                            sender_id=sender_id)
                self._mark_touch_locked()
            else:
                self._leader_fail_at = _now()
                if (self.leader_lease_until and _now() > self.leader_lease_until
                        and self.role != "leader"):
                    voters = self._voter_ids_locked()
                    ages = [round(max(0.0, _now() - float(
                        (self.nodes.get(nid) or {}).get("last_seen_epoch") or 0.0)), 1)
                        for nid in voters if nid != self.node_id]
                    _log(f"主控心跳超时：term={self.term} voters={len(voters)} "
                         f"quorum={self._quorum_locked()} "
                         f"peer_age={max(ages) if ages else 0:.1f}s "
                         f"lock_wait={lock_wait * 1000:.0f}ms")
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
                    # 跟随者互探不能当作主控心跳：即使它回复里的 leader 指向
                    # 对方自己（幽灵常长这样），也只有当它真是主控时才续租。
                    sender_id = str(peer.get("id") or "")
                    direct = (_reply_is_from_leader(resp)
                              and isinstance(leader, dict)
                              and bool(sender_id)
                              and str(leader.get("id") or "") == sender_id)
                    self._merge_snapshot_locked(resp, from_leader=direct,
                                                sender_id=sender_id)
                    self._mark_touch_locked()

    def _loop_maintenance(self) -> None:
        while not self._stop.wait(0.2):
            try:
                self._maintenance_once()
            except Exception as exc:  # noqa: BLE001
                _log(f"维护循环异常（继续）：{exc!r}")

    def _maintenance_once(self) -> None:
        need_election = False
        refresh_advertise = False
        with self.lock:
            now_epoch = _now()
            # 本机记录也走「活着的节点每秒刷新 last_seen」这一条路径：否则
            # state.json 里 leader 的 last_seen 永远停在启动时刻（T-0297）。
            self._touch_self_last_seen_locked(now_epoch)
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
                # 防御性收敛：本机记的租约绝不能晚于「节点表里主控记录」的
                # 到期时间。本进程可能刚从 leader 退回 follower（3782 行那段
                # 曾在还是 leader 时把租约续到 now+lease），也可能被转述路径
                # 写大；一旦本机租约比记录更远，`now > leader_lease_until`
                # 就永远不成立，主控死了也**永远不会触发选举**（现场：
                # 3 节点杀掉主控，剩下两台 10 秒后仍认为它活着）。
                rec_lease = float(
                    (self.nodes.get(str(self.leader_id)) or {}
                     ).get("lease_until_epoch") or 0.0)
                if rec_lease and self.leader_lease_until > rec_lease:
                    self.leader_lease_until = rec_lease
                if self.leader_lease_until and now_epoch > self.leader_lease_until:
                    self._clear_leader_locked("主节点租约过期")
                else:
                    # 连不上自己认的主控，就不该继续认 —— 幽灵常声明一个
                    # 早已下线的地址，靠同伴转述把租约撑着，本地却永远拨不通。
                    unreachable_for = max(self.config.leader_lease * 2, 2.0)
                    if (self._leader_fail_at
                            and now_epoch - self._leader_fail_at > unreachable_for):
                        self._clear_leader_locked("主控地址连续不可达")
                    else:
                        # 终极防线：租约时间可以被跟随者转述，但「主控本人
                        # 心跳」不能转述。超过几个租约窗口没收到本人心跳，
                        # 就认定是幽灵主控并清掉，让选举重新跑起来。
                        since = self._last_leader_heartbeat_at or self.started_epoch
                        ghost_after = max(self.config.leader_lease * 3, 2.0)
                        if now_epoch - since > ghost_after:
                            self._clear_leader_locked(
                                "未收到主控本人心跳（疑似幽灵主控）")
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
            # 对外地址自校准：未显式指定 advertise 时按周期重新探测（见
            # _refresh_advertise_locked）。探测本身放在锁外。
            if (not self.config.advertise_explicit
                    and now_epoch - self._last_advertise_check
                    >= self.config.advertise_refresh):
                self._last_advertise_check = now_epoch
                refresh_advertise = True
            # 墓碑过期回收（T-0327）：删过的身份过保留期自动放行，避免
            # state.json 被历史墓碑无限撑大，也避免机器改名后永久进不来。
            if self._tombstones:
                self._gc_tombstones_locked()
        # 落盘不在锁内做（_persist 自己加锁），并且按 1s 节流；维护循环 5Hz，
        # 若每轮都写 state.json 会把磁盘打满。
        self._persist()
        # 长期不变式：启动后新增的卡也要进 store 并让所有副本追平。
        # 读卡面要跑 git，必须放后台；维护循环只负责发起，不陪它等。
        self._schedule_store_reconcile()
        if refresh_advertise and self._refresh_advertise():
            # 地址变了就立刻广播一次：否则同伴还会按旧地址发包，直到租期超时
            # 把本机判死——这正是「全群认为主节点已死」的起点。
            if self.role == "leader":
                self._heartbeat_once()
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
                        # 高优先级节点的 delay 是 0，光设置 lost_at 挡不住它。
                        # 至少留 1s / 两个选举窗口，够一轮发现+注册+心跳。
                        self._startup_grace_until = _now() + max(
                            self.config.election_min * 2, 1.0)
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
            # `accepted=False`：主控**收到了但不接纳**（最常见：本机 id 还在
            # 墓碑保留期内）。旧代码无论收没收都回 200，注册方照样打印
            # 「已注册到主节点」，人却在面板上找不到它。新版主控会明说，
            # 这里就不要谎报成功，让上层继续重试并把原因带出去。
            if resp.get("accepted") is False:
                reason = str(resp.get("reason") or "rejected")
                if _now() - self._last_forged_log > 30.0:
                    self._last_forged_log = _now()
                    _log(f"主节点 {base} 拒绝接纳本机入网（{reason}）；"
                         f"若本机确实该在集群里，请确认它没被 `cluster leave` "
                         f"剔除过（墓碑 1 小时）")
                with self.lock:
                    state = resp.get("state")
                    if isinstance(state, dict):
                        self._merge_snapshot_locked(state, from_leader=False)
                    self._mark_dirty_locked()
                return False
            with self.lock:
                state = resp.get("state")
                # 只有 role=leader 的实例才会给 register 返回 200；但一台机器
                # 可能「自己 role=leader，leader_id 却指向幽灵」（旧代码转述），
                # 所以还要核对 leader 记录就是注册目标本人签发的。
                self_id = (str((state.get("self") or {}).get("id") or "")
                           if isinstance(state, dict) else "")
                leader = ((state or {}).get("leader")
                          if isinstance(state, dict) else None)
                verified = (isinstance(state, dict)
                            and str(state.get("role") or "") == "leader"
                            and bool(self_id) and isinstance(leader, dict)
                            and str(leader.get("id") or "") == self_id)
                if isinstance(state, dict):
                    self._merge_snapshot_locked(state, from_leader=verified,
                                                sender_id=self_id)
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
                            state2 = resp2.get("state")
                            self_id2 = (str((state2.get("self") or {}).get("id") or "")
                                        if isinstance(state2, dict) else "")
                            leader2 = ((state2 or {}).get("leader")
                                       if isinstance(state2, dict) else None)
                            verified2 = (isinstance(state2, dict)
                                         and str(state2.get("role") or "") == "leader"
                                         and bool(self_id2)
                                         and isinstance(leader2, dict)
                                         and str(leader2.get("id") or "") == self_id2)
                            self._merge_snapshot_locked(
                                state2 or {}, from_leader=verified2,
                                sender_id=self_id2)
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

    def _leave_peers(self) -> None:
        """优雅退出时请同伴把自己从成员表删掉（缩短 quorum 回落时间）。"""
        with self.lock:
            peers = [dict(rec) for nid, rec in self.nodes.items()
                     if nid != self.node_id and rec.get("url")
                     and self._node_status_locked(rec) in ("alive", "suspect")]
            body = {"node": self._self_node(), "node_id": self.node_id}
        for peer in peers:
            try:
                http_json(str(peer["url"]).rstrip("/") + "/api/cluster/leave",
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
            accepted = self._update_node_locked(node, touch=True,
                                                joined=known is None)
            if known is None and accepted:
                self._emit_locked("node_registered",
                                  message=f"{node.get('name') or node['id']} 注册")
            self._persist_locked()
            # 「回 200 但没进成员表」是最难查的一类故障：对端看到 ok 就停止
            # 重试，人却在面板上找不到它（现场 2026-11-11：Mac 被墓碑挡住，
            # 注册连续返回 200 ok，整整一小时不可见）。这里如实回执，
            # 让对端知道「你还没进网」并继续重试/上报。
            if not accepted:
                reason = ("tombstoned" if self._tombstoned_locked(str(node["id"]))
                          else "rejected")
                return 200, {"ok": True, "accepted": False, "reason": reason,
                             "state": self._snapshot_locked()}
            return 200, {"ok": True, "accepted": True,
                         "state": self._snapshot_locked()}

    def handle_heartbeat(self, body: dict,
                         peer_ip: str = "") -> tuple[int, dict]:
        node = body.get("node") if isinstance(body, dict) else None
        term = int(body.get("term") or 0) if isinstance(body, dict) else 0
        # 身份校验：自述 url 的主机必须就是请求来源 IP。旧 node.json 里残留的
        # 身份（现场：一台 192.168.0.110 的机器声明自己是已下线的 .103）会
        # 以幽灵身份持续注入高 term 和 leader 心跳，把本机选举一遍遍打断。
        # 这类请求**只读不写**：不合并节点、不推进 term、不采纳主控、不签租约。
        forged = (bool(peer_ip) and isinstance(node, dict)
                  and not _node_url_host_matches(
                      str(node.get("url") or ""), peer_ip))
        # IP 迁移（不是伪造）：同一台机器换了网卡地址，进程却没重启 ——
        # 自述 url 还停在上一个 IP，于是每一跳都被身份校验拒掉，永远回不了
        # 成员表，还每 30s 往日志里丢一条噪音（现场：DESKTOP-IC6RTQ4 从
        # .109 迁到 .100，incarnation 全程未变）。
        #
        # 判据刻意从严，只认「同一个进程实例」：node_id 已登记在册，且
        # 对端自述的 incarnation 与册上那条**完全相等**。incarnation 是启动
        # 时刻的毫秒时间戳，换实例必变 —— 冒充者拿不到旧实例的原值，除非
        # 它读得到 state.json。地址对不上但 incarnation 对得上，就是本机搬家。
        # 这时不改身份、只把登记地址改写成请求来源 IP，后续心跳即可正常。
        relocated = False
        if forged and isinstance(node, dict) and peer_ip:
            rec = self.nodes.get(str(node.get("id") or ""))
            if (rec is not None
                    and int(rec.get("incarnation") or 0) > 0
                    and int(node.get("incarnation") or 0) == int(rec.get("incarnation") or 0)):
                relocated = True
        with self.lock:
            if relocated:
                old_rec = self.nodes.get(str(node.get("id"))) or {}
                node = dict(node)
                node["host"] = str(peer_ip)
                # 端口优先取对端自述，其次沿用册上旧值，最后才落到本机端口。
                # 直接取本机端口在「各机端口不同」的部署里会拼出错误地址。
                port = int(node.get("port") or old_rec.get("port") or self.config.port)
                node["url"] = f"http://{peer_ip}:{port}"
                # 旧代码只在广播前刷新 advertise，对端进程没重启就一直是旧地址；
                # 于是它每一跳都带着旧 url 过来。册上地址已经是新值时**静默放行**
                # （等价于一次普通心跳），不重复记日志/事件 —— 否则「迁移」本身
                # 会变成新的噪音源（现场：每 30s 一条 .100 → .100）。
                if old_rec.get("url") != node["url"]:
                    if _now() - self._last_forged_log > 30.0:
                        self._last_forged_log = _now()
                        _log(f"节点 {node.get('name') or node.get('id')} 地址迁移："
                             f"{old_rec.get('url') or '-'} → {node['url']}"
                             f"（incarnation 未变，按同实例处理）")
                    self._emit_locked(
                        "node_relocated", node_id=str(node.get("id") or ""),
                        message=(f"{node.get('name') or node.get('id')} 对外地址变更 → "
                                 f"{node['url']}"))
                forged = False
            if forged:
                # 幽灵每秒都来一次，日志节流到 30s 一条，别把 cluster.log 刷爆。
                if _now() - self._last_forged_log > 30.0:
                    self._last_forged_log = _now()
                    _log(f"忽略伪造身份心跳：{node.get('name') or node.get('id')} "
                         f"声明 {node.get('url')}，来自 {peer_ip}，term={term}")
                return 200, {
                    "ok": True, "term": self.term, "role": self.role,
                    "is_leader": self.role == "leader",
                    "node_id": self.node_id,
                    "leader": self._leader_public_locked(),
                    "leader_lease_until": _iso(self.leader_lease_until)
                    if self.leader_lease_until else "",
                    "nodes": [], "events": [], "progress": {}, "leases": {},
                    "generation": self.generation,
                    "store_commit_seq": self._store_seq(),
                }
            if isinstance(node, dict) and node.get("id"):
                self._update_node_locked(node, touch=True)
            if term > self.term:
                self._adopt_higher_term_locked(term, body.get("leader"))
            # 只有主控本人发来的心跳（_heartbeat_once 里 role=leader 才置 True）
            # 有资格签发租约。跟随者的互探心跳带 leader=False，绝不能续租。
            if body.get("leader") is True and isinstance(node, dict):
                self._adopt_leader_locked(node, max(term, self.term),
                                          refresh_lease=True)
            self._merge_body_state_locked(body)
            # 0.16.05：心跳响应不再无条件搬 40-50KB 全量状态。
            #
            # 旧实现用 `changed = req_gen != self.generation` 判断要不要回全量。
            # 但 generation 是**每个节点本地自增**的计数器（现场四台分别
            # 607万/300万/1056万/42万），跨节点永远不相等 —— 于是每次心跳
            # 都回全量 progress+leases，而实测四台内容哈希完全一致。改用内容
            # 摘要：请求方带 `sd`，两边摘要相等就只回主干。
            req_sd = str(body.get("sd") or "") if isinstance(body, dict) else ""
            my_sd = _state_digest(self.progress, self.leases)
            # 老版本不带 sd：仍按旧语义回全量，滚动升级期间不丢状态。
            changed = (not req_sd) or (req_sd != my_sd)
            # 事件：老实现用 `event_seq` 增量，但 seq 是各节点各自的计数器、
            # 切主还会重置，合并进来的事件 seq 来自四面八方（现场本机
            # event_seq=664，deque 里却有 seq=17162 的事件），于是
            # `ev.seq > req_event_seq` 恒真，每拍重发 120 条 ~32KB。
            # 新节点改发 `events_since = now - EVENT_WINDOW_S`，只回这个时间
            # 窗口内的事件；窗口跟着墙钟走，空闲期没有新事件就恒为空。
            # 重复事件按 id 去重，重发几条无害。
            events_since = float(body.get("events_since") or 0) \
                if isinstance(body, dict) else 0.0
            if events_since > 0:
                events = [copy.deepcopy(ev) for ev in self.events
                          if float(ev.get("epoch") or 0) > events_since]
                # 按时间排序后取**最早**的一批：突发一堆事件时先把老的送出去，
                # 窗口滑过之后它们自然不再进入响应；取最新会永远丢掉最老那几条。
                events.sort(key=lambda e: float(e.get("epoch") or 0))
                events = events[:HEARTBEAT_EVENT_LIMIT]
            else:
                # 向后兼容：旧节点（不带 events_since）仍按 seq 增量。
                req_event_seq = (int(body.get("event_seq") or 0)
                                 if isinstance(body, dict) else 0)
                same_term = (int(body.get("term") or 0) == self.term
                             if isinstance(body, dict) else False)
                if same_term:
                    events = [copy.deepcopy(ev) for ev in self.events
                              if int(ev.get("seq") or 0) > req_event_seq]
                    events = events[-EVENT_PUSH_LIMIT:]
                else:
                    events = copy.deepcopy(list(self.events))[-EVENT_PUSH_LIMIT:]
            return 200, {
                "ok": True,
                "term": self.term,
                "role": self.role,
                # 让请求方能验证「这个回复是不是主控本人发的」。旧版本没有
                # is_leader 字段，用 role 兜底即可（role 一直是真实角色）。
                "is_leader": self.role == "leader",
                "node_id": self.node_id,
                "leader": self._leader_public_locked(),
                "leader_lease_until": _iso(self.leader_lease_until)
                if self.leader_lease_until else "",
                "nodes": self._heartbeat_nodes_locked(),
                "events": events,
                "progress": copy.deepcopy(self.progress) if changed else {},
                "leases": copy.deepcopy(self.leases) if changed else {},
                # 供新节点确认摘要是否收敛（老版本忽略）。
                "sd": my_sd,
                "generation": self.generation,
                "event_seq": self.event_seq,
                "store_commit_seq": self._store_seq(),
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
        # 这里**没有**持主锁，可以直接问 store 要实时位点（顺序：先取位点，
        # 再进锁；反过来就会在持主锁时等 store 锁）。严格模式靠它判「落后」。
        my_seq = self._refresh_store_seq() if self.store else 0
        cand_seq = int(cand.get("last_store_seq") or 0)
        # 人工夺权通道：body 里带 force 才放行（且本机没禁用）。
        # 这道闸原本是防止选出数据落后的主节点，但 hive 的 store 只是缓存、
        # git 才是事实账（见 StoreReconciler），落后能在当上主控后从卡面补回来。
        # 保留开关（HIVE_CLUSTER_ALLOW_FORCE_PROMOTE=0 可关掉）是为了让集群
        # 管理员能选择「宁可没主控，也不接受落后节点」这种更严的策略。
        forced = bool(body.get("force")) and self.config.allow_forced_promote
        behind = cand_seq < my_seq
        # 注意顺序：**任期/化身号这类「硬门槛」必须排在「择优」前面**。
        # 一个陈旧任期的候选应该拿到 stale_term，而不是 stronger_candidate ——
        # 后者语义是「你够格，但我更有资格」，会让候选人误以为只差让位。
        if term < self.term:
            return 200, {"ok": True, "granted": False, "term": self.term,
                         "leader": self._leader_public_locked(),
                         "reason": "stale_term"}
        with self.lock:
            stored = self.nodes.get(cid)
        if stored and int(cand.get("incarnation") or 0) < int(stored.get("incarnation") or 0):
            return 200, {"ok": True, "granted": False, "term": self.term,
                         "reason": "stale_incarnation"}
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
            if term > self.term:
                self._adopt_higher_term_locked(term)
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
        # nudge=True：这是**弱节点让位后请我接手**的礼貌请求，不是人工夺权。
        # 区别必须体现出来（T-0328）：
        #   * 不去踢现任主控下台 —— 现任若是活的好主，不该因为一次让位就震掉；
        #   * 只在「确实没有主控」时才真的发起竞选。
        nudge = bool(body.get("nudge"))
        if not self.config.electable:
            return 409, {"ok": False, "role": self.role, "node": self.node_id,
                         "error": "本机 electable=false（observer），不能当主控"}

        with self.lock:
            if self.role == "leader" and self.leader_lease_until > _now():
                return 200, {"ok": True, "already": True, "role": "leader",
                             "term": self.term, "node": self.node_id,
                             "previous_leader": self.node_id}
            if nudge and self.leader_id and self.leader_id != self.node_id \
                    and self.leader_lease_until > _now():
                # 已有在位主控：这个 nudge 是过期的，什么都不做。
                return 200, {"ok": True, "already": True, "role": self.role,
                             "term": self.term, "node": self.node_id,
                             "previous_leader": self.leader_id,
                             "ignored": "leader_alive"}
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
        if (not nudge) and leader_url and leader_id and leader_id != me:
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

    def handle_leave(self, body: dict,
                     peer_ip: str = "") -> tuple[int, dict]:
        """节点退出成员表。区分**优雅下线**与**运维剔除**，只对后者立墓碑。

        这两件事在协议里长得几乎一样（都是 `POST /api/cluster/leave` 带
        node_id），但语义完全相反：

        * 优雅下线（`_leave_peers`，进程正常退出/重启）：对方**还会回来**，
          它删记录只是为了别让大家等 membership_ttl。这里绝不能立墓碑 ——
          墓碑的 TTL 是 1 小时，而重启只要 3 秒，于是该节点会在主控面板上
          整整消失一小时。现场（2026-11-11 13:59:56）：Mac 优雅停机后立刻
          用新 incarnation 重新注册，`handle_register` 回了 200 ok，但
          `_update_node_locked` 撞上墓碑直接返回 False，Mac 直到 14:59:56
          墓碑过期才重新出现在 fb98 的集群面板里。
        * 运维剔除（人手工 `cluster leave --node X` / curl 调接口）：是真的
          要把它赶走，必须立墓碑，否则下一跳就被同伴转述灌回成员表、继续
          占票池抬 quorum（现场见 T-0327）。

        判据（照 handle_heartbeat 的身份校验口径）：请求体里带**完整自述
        记录**、且自述的 url 主机就是请求来源 IP —— 说明是那个进程自己在
        说话，属于优雅下线。运维剔除只会带一个 node_id，没有 node 自述；
        即使带了（脚本图省事抄了别人的记录），来源 IP 也对不上。
        """
        node = body.get("node") if isinstance(body, dict) else None
        node = node if isinstance(node, dict) else {}
        node_id = str(body.get("node_id") or node.get("id") or "")
        # 「自报家门 + 来源 IP 对得上」才算进程本人的优雅退出。
        graceful = bool(
            node.get("id") and peer_ip
            and str(node.get("id")) == node_id
            and _node_url_host_matches(str(node.get("url") or ""), peer_ip))
        with self.lock:
            if node_id and node_id != self.node_id and node_id in self.nodes:
                if not graceful and node.get("id") and node_id:
                    # 走到这里多半是「同一实例刚换过 IP，自述 url 还是旧地址」
                    # （与 handle_heartbeat 的 relocated 同因）。判据同款、
                    # 一样从严：册上那条的 incarnation 必须与自述**完全相等**。
                    # incarnation 是启动时刻的毫秒时间戳，换实例必变，
                    # 所以运维剔除（没有自述，或抄来的记录 incarnation 不符）
                    # 不会误判成优雅下线。
                    known = self.nodes.get(node_id) or {}
                    graceful = bool(
                        int(known.get("incarnation") or 0) > 0
                        and int(node.get("incarnation") or 0)
                        == int(known.get("incarnation") or 0))
                if graceful:
                    # 优雅下线：删记录让 quorum 立刻回落，但不立墓碑。
                    rec = self.nodes.pop(node_id, None) or {}
                    self._emit_locked(
                        "node_leave", node_id=node_id,
                        message=f"{rec.get('name') or node_id} 优雅下线"
                                f"（重启后可立即回网）")
                    if node_id == self.leader_id:
                        self._clear_leader_locked("主节点退出")
                    self._mark_dirty_locked()
                    self._persist_locked()
                    return 200, {"ok": True, "removed": True,
                                 "graceful": True, "tombstoned": False}
                self._tombstone_locked(node_id, "operator leave")
                self._persist_locked()
                return 200, {"ok": True, "removed": True,
                             "graceful": False, "tombstoned": True}
            rec = self.nodes.pop(node_id, None) if node_id else None
            if rec is not None:
                self._emit_locked("node_leave", node_id=node_id,
                                  message=f"{rec.get('name') or node_id} 退出")
                if node_id == self.leader_id:
                    self._clear_leader_locked("主节点退出")
                self._mark_dirty_locked()
                self._persist_locked()
            return 200, {"ok": True, "removed": bool(rec)}

    def handle_upgrade_trigger(self, body: dict) -> tuple[int, dict]:
        """让本进程执行一次集群升级（主控「一键节点升级」的落地端）。

        为什么走集群内 HTTP 而不是 SSH：集群本来就有互相可达的地址与共享
        token，再引一套免密登录只会多一个故障面。这条接口和 `hive upgrade
        apply` 等价，闸门也完全一样（脏工作区 / 非快进 / policy 都在 CLI 里）。

        **安全边界**：只有主控能触发（`leader_id == 请求来源节点`，
        或本机就是主控）。否则任意集群成员都能远程改写别人的代码仓。
        """
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "请求体必须是对象"}
        requester = str(body.get("requester") or "")
        if not requester:
            return 400, {"ok": False, "error": "缺少 requester（发起方 node_id）"}
        with self.lock:
            leader = str(self.leader_id or "")
            # 发起方只能是**主控本人**：
            #   * 本机是主控 → requester 必须就是本机 id（主控给自己下令）；
            #   * 本机是跟随者 → requester 必须等于本机认可的那个主控。
            # 不能写成「本机是主控就一律放行」：集群默认不带 token（局域网信任
            # 模型），那样任何能访问 8788 的人都能拿任意 requester 触发一次
            # 代码仓改写。这条接口会动 git HEAD，闸门必须比读接口严。
            if requester != self.node_id and not (leader and requester == leader):
                return 403, {"ok": False,
                             "error": f"只有主控能触发升级（发起方 {requester}，"
                                      f"主控 {leader or '无'}，本机 {self.node_id}）"}
            target = str(body.get("version") or "")
        # `skip_apply`：调用方自己已经完成快进，只想让本进程安排重启。
        # 主控那一行走「本地 apply + 请本机 hived 重启」，若不再跑一次 apply，
        # 第二次 apply 只会回「已是最新」。这条分支不做任何 git 动作。
        if body.get("skip_apply"):
            restarted = self._schedule_self_restart() if body.get("restart") else False
            with self.lock:
                self._emit_locked(
                    "upgrade.restart_scheduled",
                    message=f"按主控 {requester[:12]} 的指令安排重启以加载新代码")
                self._persist_locked()
            return 200, {"ok": True, "skipped_apply": True,
                         "node_id": self.node_id, "node": self.config.name,
                         "restart_scheduled": bool(restarted)}
        try:
            import hive as _hive
        except Exception as exc:  # noqa: BLE001
            return 503, {"ok": False, "error": f"无法加载 hive 模块：{exc}"}
        # CLI 的 apply 会往 stdout 打报告；服务进程里不能让它直接落到日志，
        # 收进缓冲区当 detail 回给主控（主控面板要能看到失败原因）。
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = _hive.Hive(self.config.hive_dir).cmd_upgrade(
                    "apply", yes=bool(body.get("yes")), as_json=True,
                    dry_run=bool(body.get("dry_run")))
        except Exception as exc:  # noqa: BLE001 —— 升级失败要如实回给主控
            return 500, {"ok": False, "error": str(exc),
                         "detail": buf.getvalue()[-2000:]}
        detail = buf.getvalue().strip()
        restarted = False
        if rc == 0 and body.get("restart"):
            # 升级只换代码，进程还跑着旧代码 —— 面板的版本列不会变，人会以为
            # 「一键升级没生效」。这里可选地安排一次自重启：先让响应发出去，
            # 再由一个**脱离本进程**的 helper 调 `cluster restart`（它会读
            # pidfile 停掉当前 hived 再拉起新的）。刻意用延迟 + 独立会话，
            # 否则我们会在自己还在写响应时把自己杀掉。
            restarted = self._schedule_self_restart()
        with self.lock:
            self._emit_locked(
                "upgrade.applied",
                message=(f"本机按主控 {requester[:12]} 的指令升级到 {target or '最新'}"
                         f"（rc={rc}" + ("，已安排重启" if restarted else "") + "）"))
            self._persist_locked()
        return 200, {"ok": rc == 0, "code": rc, "node_id": self.node_id,
                     "node": self.config.name, "target": target,
                     "restart_scheduled": restarted,
                     "detail": detail[-2000:]}

    def _schedule_self_restart(self, delay: float = 3.0) -> bool:
        """安排一次「退出自己、由托管方/helper 拉起」，让新代码生效。失败返回 False。

        用 `start_new_session=True`（POSIX）/ detached（Windows）把 helper
        彻底脱离本进程：本进程随后会被 SIGTERM 掉，helper 必须活着才能收尾。

        **必须兼顾 daemon 托管**：生产机（fb98）的 hived 挂在 daemon(8) 下
        （`-r` 持续重启），本进程一死它就自动拉一个新的。helper 若再无条件
        起一份，第二个进程抢不到端口（EADDRINUSE），还会打开同一个 sqlite
        拖进 database is locked。所以 helper 的顺序是：

          1. `cluster stop` —— 尽力优雅停机（落盘 state.json、通知同伴 leave）；
          2. 等端口释放，再看**是否已经有人把它拉起来**（托管方会抢先）；
          3. 只有确实没人拉起时，才自己 `cluster start`。
        """
        hive_py = Path(__file__).resolve().with_name("hive.py")
        base = [sys.executable, str(hive_py), "--dir", str(self.config.hive_dir)]
        start_argv = base + ["cluster", "start",
                             "--port", str(self.config.port),
                             "--host", str(self.config.bind_host),
                             "--discovery-port", str(self.config.discovery_port),
                             "--state-dir", str(self.config.state_dir),
                             "--name", str(self.config.name)]
        if self.config.mcp_enabled:
            start_argv += ["--mcp", "--mcp-port", str(self.config.mcp_port)]
        if self.config.store_enabled:
            start_argv += ["--store", "--claim-backend",
                           str(self.config.claim_backend)]
        my_pid = os.getpid()
        marker = self.config.state_dir / STOPPED_MARKER_NAME
        health_url = f"http://127.0.0.1:{self.config.port}/api/cluster/health"
        # 延迟交给 helper 自己 sleep：父进程随时可能被信号打断，把 sleep 放在
        # 这里反而会缩短「先把 HTTP 响应发完」的窗口。
        #
        # 用列表拼行而不是 `%` 格式化：这段脚本里既有 repr 出来的路径，也有
        # f-string 和字面量，混着写 `%` 很容易在「隐式字符串拼接 + 运算符优先级」
        # 上出错，而且错了只在运行时才炸。
        lines = [
            "import os, subprocess, sys, time, urllib.request",
            f"time.sleep({max(0.5, float(delay))!r})",
            "DN = subprocess.DEVNULL",
            # 先清 stopped 标记：托管方（daemon -r / systemd）拉起的新进程若看到
            # 这个标记，会把「自动重启」误当成「用户要停机」。
            f"try: os.unlink({str(marker)!r})",
            "except OSError: pass",
            # 只 SIGTERM 自己，**不要**调 `cluster stop`：那条命令会写 stopped
            # 标记并删 pidfile，正是上面要避免的副作用。
            f"os.kill({my_pid}, 15)",
            "deadline = time.time() + 25",
            "up = False",
            "while time.time() < deadline:",
            "    time.sleep(1)",
            "    try:",
            f"        urllib.request.urlopen({health_url!r}, timeout=1).read()",
            "        up = True",
            "        break",
            "    except Exception:",
            "        up = False",
            "# 托管方通常先一步把进程拉起来；只有确实没人管（裸启动的机器）才自己起。",
            "if not up:",
            f"    subprocess.call({start_argv!r}, stdin=DN, stdout=DN, stderr=DN)",
        ]
        script = "\n".join(lines) + "\n"
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                  "stderr": subprocess.DEVNULL, "close_fds": True}
        # 刻意不走 no_window_kwargs()：它的注释写明与 DETACHED_PROCESS 互斥，
        # 而后台常驻进程正是要用 detached。POSIX 用 start_new_session，
        # Windows 用 DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP。
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:  # pragma: no cover - Windows
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        try:
            subprocess.Popen([sys.executable, "-c", script], **kwargs)
            _log(f"已安排自重启（{delay:.0f}s 后 SIGTERM 自身，"
                 f"由托管方拉起；无人托管则自行 start）")
            return True
        except Exception as exc:  # noqa: BLE001
            _log(f"安排自重启失败（忽略，需手工 hive cluster restart）：{exc!r}")
            return False

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
                have = self._refresh_store_seq()
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
                # 走无锁缓存：长 git/store 操作持锁时也必须秒回，否则同伴会
                # 把「健康检查超时」误判成本机死亡，提前发起选举。
                self._send(200, cluster.health_payload())
                return
            if not self._authorized():
                self._send(401, {"ok": False, "error": "token 不匹配"})
                return
            if path == "/api/cluster/state":
                self._send(200, {"ok": True, "state": cluster.snapshot()})
                return
            if path == "/api/cluster/status":
                self._send(200, {"ok": True, "control": cluster.control_status()})
                return
            if path == "/api/cluster/pull/status":
                rid = (query.get("id") or [""])[0]
                code, payload = cluster.control_pull_status(rid)
                self._send(code, payload)
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
                    peer_ip = str((self.client_address or ("", 0))[0] or "")
                    code, payload = cluster.handle_heartbeat(body, peer_ip=peer_ip)
                elif path == "/api/cluster/vote":
                    code, payload = cluster.handle_vote(body)
                elif path == "/api/cluster/resign":
                    code, payload = cluster.handle_resign(body)
                elif path == "/api/cluster/promote":
                    code, payload = cluster.handle_promote(body)
                elif path == "/api/cluster/leave":
                    # 传来源 IP：handle_leave 靠它区分「节点自己优雅下线」
                    # 与「运维从别处剔除」—— 只有后者才立墓碑（见其 docstring）。
                    peer_ip = str((self.client_address or ("", 0))[0] or "")
                    code, payload = cluster.handle_leave(body, peer_ip=peer_ip)
                elif path == "/api/cluster/upgrade":
                    code, payload = cluster.handle_upgrade_trigger(body)
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
                elif path == "/api/cluster/pull":
                    code, payload = cluster.control_request_pull(body)
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

    # 配置来源说清楚：只有 state_dir/config.json 参与运行时。serve/start/restart
    # 是「人以为改了配置」的高发场景，启动时就把仓库里那份影子文件点出来，
    # 否则用户会在 --dir 目录的 config.json 上改半天，然后认为改动没生效。
    if action in ("serve", "start", "restart"):
        for line in config_source_lines(config)[1:]:
            print(f"[hive-cluster] {line}", file=sys.stderr)

    if action == "serve":
        # D14：只有 serve 入口开启控制面。后台 start/restart 最终也是
        # _spawn_background 拉起 `hive.py cluster serve`，同样走这里。
        config.control_plane = True
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
            shadow = ignored_config_path(config)
            _print_json({"running": running, "state": snap,
                         "config_file": str(effective_config_path(config)),
                         "ignored_config_file": str(shadow) if shadow else "",
                         "mcp": health.get("mcp") or {}})
        else:
            print(_status_line(snap, running, _stopped_marker(config).exists()))
            for line in config_source_lines(config):
                print(line)
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
