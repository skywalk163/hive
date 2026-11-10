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
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

BASE_DIR = Path(__file__).resolve().parent
HIVE_ROOT = BASE_DIR.parent  # dashboard 的上一级就是 hive 仓库根

# 端口默认值的唯一来源（与 hive.py / hive_cluster.py 共用同一份）。
if str(HIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(HIVE_ROOT))
try:
    from hive_ports import (
        DASHBOARD_PORT as DEFAULT_DASHBOARD_PORT,
        DASHBOARD_HOST as DEFAULT_DASHBOARD_HOST,
        DASHBOARD_HOST_ENV as DASHBOARD_HOST_ENV,
        CLUSTER_HTTP_PORT as CLUSTER_DEFAULT_PORT,
        CLUSTER_HTTP_ENV as CLUSTER_PORT_ENV,
    )
except ImportError:  # pragma: no cover - 单独拷走 dashboard 时的兜底
    DEFAULT_DASHBOARD_PORT = 8787
    DEFAULT_DASHBOARD_HOST = "0.0.0.0"
    DASHBOARD_HOST_ENV = "HIVE_DASHBOARD_HOST"
    CLUSTER_DEFAULT_PORT = 8788
    CLUSTER_PORT_ENV = "HIVE_CLUSTER_PORT"

# 子进程「无窗」（T-0206）：refresh_loop 每 --refresh-interval（默认 30s）跑一次
# `hive status --json`，不给 CREATE_NO_WINDOW 就会每 30 秒闪一次黑窗。
try:
    from hive_compat import no_window_kwargs
except ImportError:  # pragma: no cover - 单独拷走 dashboard 时的兜底
    def no_window_kwargs():
        return {}

# 系统级核心配置：Hub 地址 + 集中式仓库列表，与 agents/<name>.json 共同构成 hive 配置
HIVE_CONFIG_PATH = HIVE_ROOT / "hive-config.json"

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
AGENT_MODES = ("exec", "verify", "manual", "accept")


def _as_str_list(v):
    """abilities / capabilities：列表或逗号字符串 → 去空白字符串数组。"""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


def _iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

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
API_TASK = "/api/task"
API_PUSH = "/api/push"
API_CLUSTER = "/api/cluster"
API_DISPATCH_PROMPT = "/api/dispatch-prompt"
API_CHAT = "/api/chat"
API_PLANS = "/api/plans"

# ---- 看板聊天框（跨机留言）----
# 真身住在集群（每个节点 ~/.hive/cluster/chat.jsonl + 2 秒增量同步），
# 看板只是它的一个前端。集群没跑时降级到 hive/chat/chat.jsonl，
# 保证「本机单人也能留言」，等集群起来不影响已有留言。
CHAT_SCOPE_RE = re.compile(r"^(hall|task:T-\d{3,6}|agent:[A-Za-z0-9._-]{1,64})$")
CHAT_MAX_TEXT = 8000
CHAT_KINDS = ("note", "question", "answer")

# 任务卡目录：默认就是协调仓根的 tasks/；测试可以替换这个模块级变量指向临时目录。
TASKS_DIR = HIVE_ROOT / "tasks"
# 派发接口的任务号必须严格是 T-四位数字：这里比 /api/push 的 TID_RE 更严，
# 因为接口会把任务号直接拼进文件名（TID_RE 允许 `../`，拼路径就成了穿越口子）。
DISPATCH_TID_RE = re.compile(r"^T-\d{4}$")
DISPATCH_FORMATS = ("markdown", "json")
CONFIG_PATH = BASE_DIR / "settings.json"
NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"

# 任务操作代理：后端调用 hive CLI 真正执行（前端按钮 -> 这里 -> python3 hive.py）
HIVE_PY = HIVE_ROOT / "hive.py"
HIVE_OP_TIMEOUT = 120  # 单条操作超时（秒）；hive 命令涉及 git push，给足余量
# 子进程一律走 UTF-8：Windows 下子进程 stdout 默认 cp936，`hive status --json` 碰到
# ✓ / 全角符号会以 UnicodeEncodeError 退出（rc=1、stdout 空），状态文件于是一直写不出来，
# 看板就永远停在旧快照或 503。三个平台用同一套环境变量最省事。
PYTHON_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
# 用当前解释器，别硬编码 python3：Windows 上 python3 常常是 Store 的 App 别名存根
PYTHON = sys.executable or "python3"


def hive_argv(*args):
    """拼一条 hive CLI 的子进程命令行（统一解释器 + 协调仓根目录）。"""
    return [PYTHON, str(HIVE_PY), "--dir", str(HIVE_ROOT), *args]


# 看板按钮暴露的操作。shelve/unshelve/respawn 是「搁置」三件套：封存、放回、
# 另立新卡。它们会释放或重建派发资源，一律只许 controller 角色做。
TASK_ACTIONS = ("close", "retry", "unassign", "reopen",
                "shelve", "unshelve", "respawn")
TID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")  # 任务号允许字符
SHELF_TITLE_MAX = 200                                    # 另立新卡的标题长度上限
SHELF_ACTIONS = ("shelve", "unshelve", "respawn")

# ---- 写操作鉴权（复用 mcp_auth 的本机 token 表）----
#
# 看板默认只听 127.0.0.1：本机就是身份边界，写操作直接放行（与老行为一致，
# 不至于一升级就所有按钮 401）。一旦 --host 0.0.0.0（让别的机器/手机看），
# 写操作**必须**带 token，否则只能读。判定口径：
#   * HIVE_DASH_AUTH=off   显式关闭（自建内网也接受风险时用）
#   * HIVE_DASH_AUTH=on     任何来源都要 token
#   * HIVE_DASH_AUTH=auto   默认：非 loopback 绑定 → 要 token
# token 来源（依次尝试）：Authorization: Bearer / X-Hive-Token / hive_token cookie。
# cookie 由 POST /api/login 写入（HttpOnly；看板本身还要能读，所以不用 SameSite=None）。
AUTH_MODE_ENV = "HIVE_DASH_AUTH"
COOKIE_NAME = "hive_token"
LOGIN_PATH = "/api/login"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _auth_mode(bind_host: str) -> str:
    mode = (os.environ.get(AUTH_MODE_ENV) or "auto").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return "off"
    if mode in ("on", "1", "true", "yes"):
        return "on"
    host = (bind_host or "").strip()
    return "off" if host in LOOPBACK_HOSTS else "on"


def _load_mcp_auth():
    """延迟导入 mcp_auth；缺模块时返回 None（写操作会被拒绝，而不是静默放行）。"""
    try:
        import mcp_auth  # noqa: WPS433  同仓模块，按需导入
    except ImportError:
        return None
    return mcp_auth


def _resolve_dashboard_token(token: str):
    """校验 token，返回 {agent, role} 或 None。role 决定能做什么。"""
    if not token:
        return None
    mod = _load_mcp_auth()
    if mod is None:
        return None
    try:
        return mod.resolve(token)
    except Exception:  # noqa: BLE001  坏 token 文件不等于我有权限
        return None


def _audit_log_path() -> Path:
    return BASE_DIR / "audit.log"


def append_audit(record: dict) -> None:
    """把一次写操作记进 dashboard/audit.log（JSON Lines，append-only）。

    为什么单独一个文件而不是混进 status.json：audit 要长期留、要能 grep，
    而 status.json 是频繁覆盖的派生快照。
    """
    row = {"ts": _iso_now(), **record}
    try:
        path = _audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - 磁盘满等极端情况
        sys.stderr.write("[hive-dashboard] 审计日志写入失败: %s\n" % exc)

# agent 主动上报：HTTP 直推（加速通道）落到这个文件，/api/status 取快照时合并进去
PUSHED_STATUS_PATH = BASE_DIR / "pushed-status.json"


def load_pushed():
    try:
        return json.loads(PUSHED_STATUS_PATH.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, ValueError):
        return {}


def save_pushed(data):
    try:
        PUSHED_STATUS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _ts(s):
    """ISO 字符串 -> epoch 毫秒。跨时区比较必须转时间戳，否则字符串比较会错序。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)).timestamp() * 1000.0
    except ValueError:
        return None


# ---- 集群（hive_cluster.py）只读桥接 ----------------------------------- #
CLUSTER_STATE_ENV = "HIVE_CLUSTER_STATE"


def cluster_state_dir():
    return Path(os.environ.get(CLUSTER_STATE_ENV)
                or (Path.home() / ".hive" / "cluster")).expanduser()


def _cluster_config():
    try:
        data = json.loads((cluster_state_dir() / "config.json").read_text(
            encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cluster_port():
    raw = (os.environ.get(CLUSTER_PORT_ENV) or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    try:
        return int(_cluster_config().get("port") or CLUSTER_DEFAULT_PORT)
    except (TypeError, ValueError):
        return CLUSTER_DEFAULT_PORT


def cluster_token():
    token = (os.environ.get("HIVE_CLUSTER_TOKEN") or "").strip()
    if token:
        return token
    return str(_cluster_config().get("token") or "")


def load_cluster_state():
    """优先问本机集群要实时状态；进程没跑时退回 state.json 的 snapshot。

    返回 (state|None, running_bool)。任何失败都静默：集群是可选件，
    看板没有集群也必须能正常显示任务。
    """
    import urllib.error as _urlerror
    from urllib import request as _request
    url = "http://127.0.0.1:%d/api/cluster/state" % cluster_port()
    headers = {"Accept": "application/json"}
    token = cluster_token()
    if token:
        headers["X-Hive-Cluster-Token"] = token
    try:
        opener = _request.build_opener(_request.ProxyHandler({}))
        req = _request.Request(url, headers=headers, method="GET")
        with opener.open(req, timeout=0.6) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        state = body.get("state") if isinstance(body, dict) else None
        if isinstance(state, dict):
            return state, True
    except (_urlerror.URLError, OSError, ValueError):
        pass
    try:
        raw = json.loads((cluster_state_dir() / "state.json").read_text(
            encoding="utf-8-sig"))
        if isinstance(raw, dict):
            snap = raw.get("snapshot")
            return (snap if isinstance(snap, dict) else raw), False
    except (OSError, ValueError):
        pass
    return None, False


def cluster_progress_map(state):
    """集群 progress -> 与 pushed-status.json 同构的 {tid: item}。"""
    out = {}
    if not isinstance(state, dict):
        return out
    for tid, item in (state.get("progress") or {}).items():
        if not isinstance(item, dict):
            continue
        out[str(tid)] = {
            "task": str(tid),
            "agent": item.get("agent") or "",
            "state": item.get("state") or "",
            "progress": item.get("progress"),
            "step": item.get("step") or "",
            "message": item.get("message") or "",
            "updated_at": item.get("updated_at") or "",
        }
    return out


# ---- 聊天（跨机留言）桥接 ---------------------------------------------- #
def chat_local_path():
    """集群不在时的本地留言文件（每台机器各一份，不会被 git 同步覆盖）。"""
    raw = (os.environ.get("HIVE_CHAT_LOCAL") or "").strip()
    return Path(raw).expanduser() if raw else (
        Path.home() / ".hive" / "chat" / "chat.jsonl")


def _cluster_chat_http(method, query=None, payload=None, timeout=2.0):
    """问本机集群要留言。/ 发留言。集群不在返回 (False, None)，由调用方降级。"""
    import urllib.error as _urlerror
    from urllib import request as _request
    url = "http://127.0.0.1:%d/api/cluster/chat" % cluster_port()
    if query:
        url += "?" + urlencode(query)
    headers = {"Accept": "application/json"}
    token = cluster_token()
    if token:
        headers["X-Hive-Cluster-Token"] = token
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        opener = _request.build_opener(_request.ProxyHandler({}))
        req = _request.Request(url, data=data, headers=headers, method=method)
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return True, body if isinstance(body, dict) else None
    except (_urlerror.URLError, OSError, ValueError):
        return False, None


def chat_local_read(since=0.0, scope="", limit=200):
    path = chat_local_path()
    scopes = {s.strip() for s in (scope or "").split(",") if s.strip()}
    items = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or not msg.get("id"):
                    continue
                if float(msg.get("epoch") or 0) <= since:
                    continue
                if scopes and str(msg.get("scope")) not in scopes:
                    continue
                items.append(msg)
    except OSError:
        return []
    items.sort(key=lambda m: (float(m.get("epoch") or 0), str(m.get("id"))))
    return items[-limit:] if limit > 0 else items


def chat_local_append(msg):
    path = chat_local_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def norm_chat_scope(value):
    text = str(value or "").strip()
    return text if CHAT_SCOPE_RE.match(text) else "hall"


def write_status_atomic(status_path, text):
    """原子替换写状态文件（T-0191 §4）。

    Path.write_text 直写主文件时，另一个读者正好读到一半就拿到半截 JSON
    → /api/status 500。先写 tmp 再 os.replace（同目录 rename，POSIX 上原子），
    主文件任何时刻都是完整状态。refresh_loop 与 _refresh_status 共用。
    """
    tmp = Path(str(status_path) + ".tmp.py")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, status_path)


def refresh_loop(status_path, interval):
    """「辅」通道：定期跑 `hive status --json` 重生成状态文件。

    agent 主动推送是主通道，但只有 git 通道的 agent 推送要等 hive 重新拉一次
    才会出现在快照里；这个循环就是兜底——长时间没推送的卡也由它刷新出真实状态
    （租约过期 → stale，交给 watchdog 回收）。
    """
    argv = hive_argv("status", "--json")
    while True:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  encoding="utf-8", env=PYTHON_ENV,
                                  timeout=HIVE_OP_TIMEOUT, **no_window_kwargs())
            if proc.returncode == 0 and proc.stdout.strip():
                write_status_atomic(status_path, proc.stdout)
            else:
                # 不能全静默：Windows 上 cp936 曾让这里连续失败而没人发现，
                # 表现就是「看板一直不刷新」。留一行证据，保留上一份状态。
                sys.stderr.write("[hive-dashboard] 刷新失败 rc=%s: %s\n"
                                 % (proc.returncode,
                                    (proc.stderr or "").strip().splitlines()[-1:]))
        except Exception as exc:  # noqa: BLE001 —— 刷新失败不影响服务，下一轮再试
            sys.stderr.write("[hive-dashboard] 刷新异常: %s\n" % exc)
        time.sleep(max(5, int(interval)))


def load_card_file(path):
    """读一张任务卡 → (card_dict, body)。

    复用 `hive.parse_card`（frontmatter 的唯一权威解析器），**不 shell 出去调 CLI**：
    `hive prompt` 会先 `git fetch`，一次网络等待就能把 HTTP 线程拖死。
    """
    from hive import parse_card          # 延迟导入：hive.py 很大，只在真要用时付代价
    return parse_card(Path(path))


def render_dispatch(card, agent, fmt, body):
    """调 dispatch_prompt 渲染派发指令（纯函数，无副作用）。"""
    import dispatch_prompt
    return dispatch_prompt.render_dispatch_prompt(
        card, agent=agent or None, output_format=fmt, body=body)


def load_status_record(status_path, tid):
    """从状态快照里找一张卡的运行时记录；读不到就返回 None。

    快照是 `hive status --json` 落的盘（refresh_loop 维护），这里只读不写。
    任何读盘/解析失败都降级成 None —— 复制按钮退回纯卡面渲染，不该因为
    快照坏了就 500。状态根节点兼容两种形态：裸数组 / {tasks: [...]}。
    """
    try:
        raw = Path(status_path).read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        return None
    for rec in tasks:
        if isinstance(rec, dict) and rec.get("id") == tid:
            return rec
    return None


# ---- 任务计划（计划 Tab）----
# 计划真身放在 plan_store里（JSON + 原子写），这里只负责路由、鉴权和审计。
# 进度一律从当前状态快照现算：另存一份进度计数就会出现「快照更新了、
# 计划还显示 3/8」的二义性。
def load_status_map(status_path):
    """快照 → {tid: 行}。读不到就返回空表（计划照常显示，进度按 0 算）。"""
    try:
        raw = Path(status_path).read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        return {}
    return {str(r.get("id")): r for r in tasks if isinstance(r, dict) and r.get("id")}


def plan_module():
    """延迟导入 plan_store：单独拷走 dashboard 时也要给出清晰的 500 而不是 ImportError。"""
    import plan_store  # noqa: WPS433  同目录模块，按需导入
    return plan_store


def _plan_actor(who):
    """把鉴权结果整理成 plan_store 用的身份；本机回环也算 controller。

    `require_write_auth` 在 auth=off 时返回 {agent: loopback, role: controller}，
    这正是老行为（本机就是身份边界）。这里的额外处理只有一处：把空 agent
    补成 loopback，避免计划记录出现 created_by="" 而在页面上无法判定归属。
    """
    who = dict(who or {})
    if not str(who.get("agent") or "").strip():
        who["agent"] = "loopback" if who.get("mode") == "off" else "anonymous"
    return who


# 计划指令的增量游标：只处理「上次之后」的集群留言。
# 首次读到最新位置但不执行历史，避免把旧聊天里的 /plan 文本重放一遍。
_INBOUND_CURSOR = 0.0
_INBOUND_READY = False


SERVE_STARTED_AT = time.time()


def service_freshness():
    """服务端代码是否比进程新：True 表示「改了 serve.py 但没重启」。

    只看导入时刻与各模块 mtime 的先后，不做哈希 —— 目的是给一句可操作的提示
    （`dashboard-service.sh restart`），不是做完整性校验。
    """
    stale = []
    for name in ("serve.py", "plan_store.py"):
        try:
            mtime = (BASE_DIR / name).stat().st_mtime
        except OSError:
            continue
        if mtime > SERVE_STARTED_AT + 1:      # +1s 容忍同一秒内的写入
            stale.append(name)
    return {
        "started_at": datetime.fromtimestamp(SERVE_STARTED_AT).astimezone()
                              .isoformat(timespec="seconds"),
        "stale_modules": stale,
        "restart_hint": ("改了 %s 但进程还是旧的：运行 "
                         "`sh scripts/dashboard-service.sh restart`" % "、".join(stale))
                        if stale else "",
    }


def build_handler(status_path, write_auth="off"):
    """用闭包把状态文件路径注入请求处理器（避免全局变量）。

    ``write_auth`` 为 "on" 时写操作必须带 token；"off" 表示本机回环、放行。
    """

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

        def _send_json(self, code, payload, extra_headers=None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8",
                       extra_headers=extra_headers)

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
            if path == API_CLUSTER:
                self.serve_cluster_get()
                return
            if path == API_DISPATCH_PROMPT:
                self.serve_dispatch_prompt()
                return
            if path == API_CHAT:
                self.serve_chat_get()
                return
            if path == API_PLANS:
                self.serve_plans_get()
                return
            entry = STATIC_FILES.get(path)
            if entry is None:
                self._send_json(404, {"error": "未找到资源: %s" % path})
                return
            self._send_file(entry[0], entry[1])

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        # ---- 鉴权 ----
        def client_token(self):
            """从请求里取 token：Bearer → X-Hive-Token → cookie。"""
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            direct = (self.headers.get("X-Hive-Token") or "").strip()
            if direct:
                return direct
            cookie = self.headers.get("Cookie") or ""
            for part in cookie.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME and value:
                    return value.strip()
            return ""

        def require_write_auth(self):
            """写操作的统一鉴权；通过返回身份信息，不通过已经回了 401 并返回 None。"""
            if write_auth == "off":
                return {"agent": "loopback", "role": "controller", "mode": "off"}
            who = _resolve_dashboard_token(self.client_token())
            if not who:
                self._send_json(401, {
                    "ok": False, "auth": "required",
                    "error": "写操作需要 token：hive agent enroll <controller> 拿 token，"
                             "再在看板右上角「登录」里填；或向后端带 "
                             "Authorization: Bearer <token>",
                })
                return None
            return who

        def is_controller(self, who) -> bool:
            return str((who or {}).get("role") or "") in ("controller", "admin")

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == LOGIN_PATH:
                self.serve_login()
                return
            if path == API_CONFIG:
                self.serve_config_post()
                return
            if path == API_TASK:
                self.serve_task_post()
                return
            if path == API_PUSH:
                self.serve_push()
                return
            if path == API_CHAT:
                self.serve_chat_post()
                return
            if path == API_PLANS:
                self.serve_plans_post()
                return
            self._send_json(404, {"error": "未找到资源: %s" % path})

        # ---- /api/login：写入 HttpOnly cookie，后续写操作凭它鉴权 ----
        def serve_login(self):
            length = self._read_length()
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON: %s" % exc})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"ok": False, "error": "顶层必须是对象"})
                return
            who = _resolve_dashboard_token(str(body.get("token") or "").strip())
            if not who:
                append_audit({"action": "login", "ok": False, "source": self.client_address[0],
                              "detail": "token 无效"})
                self._send_json(401, {"ok": False, "error": "token 无效或已吊销"})
                return
            cookie = ("%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400"
                      % (COOKIE_NAME, who.get("token") or str(body.get("token")).strip()))
            append_audit({"action": "login", "ok": True, "agent": who.get("agent"),
                          "role": who.get("role"), "source": self.client_address[0]})
            self._send_json(200, {"ok": True, "agent": who.get("agent"),
                                  "role": who.get("role")},
                            extra_headers={"Set-Cookie": cookie})

        def _read_length(self) -> int:
            try:
                return int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                return 0

        # ---- /api/config：系统只读信息 + 服务端共享看板默认（GET/POST） ----
        def system_info(self):
            hc = self.load_hive_config()
            agents_dir = HIVE_ROOT / "agents"
            agents = []
            if agents_dir.is_dir():
                for p in sorted(agents_dir.glob("*.json")):
                    if p.name == ".gitkeep":
                        continue
                    try:
                        agents.append(json.loads(p.read_text(encoding="utf-8")))
                    except ValueError:
                        pass
            cstate, crunning = load_cluster_state()
            leader = (cstate or {}).get("leader") or {}
            return {
                "status_file": status_path,
                "dashboard_dir": str(BASE_DIR),
                # 进程启动时刻 vs 磁盘上 serve.py 的 mtime：静态文件每请求重读、
                # serve.py 常驻内存，改了代码不重启就会出现「页面是新代码、后端是旧
                # 路由」（/api/plans 404 就这么来的）。前端据此提示重启，别让人干瞪眼。
                "service": service_freshness(),
                "hub": hc.get("hub", os.environ.get("HIVE_HUB", "")),
                "repos": hc.get("repos", {}) or {},
                "agents": agents,
                "cluster": {
                    "running": crunning,
                    "state_file": str(cluster_state_dir() / "state.json"),
                    "port": cluster_port(),
                    "term": (cstate or {}).get("term", 0),
                    "leader": leader.get("name") or "",
                    "leader_url": leader.get("url") or "",
                    "alive": (cstate or {}).get("alive", 0),
                    "known": (cstate or {}).get("known", 0),
                },
            }

        def load_dashboard_config(self):
            try:
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except ValueError:
                return {}

        def load_hive_config(self):
            try:
                return json.loads(HIVE_CONFIG_PATH.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                return {}

        def save_hive_config(self, cfg):
            HIVE_CONFIG_PATH.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        def apply_system_config(self, system):
            """把前端提交的系统核心属性落到本地文件。

            - hub / repos → hive-config.json
            - agents      → 与 agents/ 目录对账：写/更新提交的，删掉未提交但已存在的
            不自动 git 提交，避免意料之外的推送。
            """
            hc = self.load_hive_config()
            if "hub" in system:
                hc["hub"] = "" if system.get("hub") is None else str(system.get("hub"))
            if "repos" in system and isinstance(system.get("repos"), dict):
                repos = {}
                for k, v in system["repos"].items():
                    if not k:
                        continue
                    repos[str(k)] = str(v)
                hc["repos"] = repos
            if "agents" in system and isinstance(system.get("agents"), list):
                self._reconcile_agents(system["agents"])
            self.save_hive_config(hc)

        def _reconcile_agents(self, agents):
            agents_dir = HIVE_ROOT / "agents"
            agents_dir.mkdir(parents=True, exist_ok=True)
            seen = set()
            for a in agents:
                if not isinstance(a, dict):
                    raise ValueError("agent 必须是对象")
                name = (a.get("name") or "").strip()
                if not name:
                    raise ValueError("agent 缺少 name")
                if not AGENT_NAME_RE.match(name):
                    raise ValueError("agent name 不合法（只允许字母数字 . _ -，且不能以 . 开头）: %s" % name)
                mode = a.get("mode") or "exec"
                if mode not in AGENT_MODES:
                    raise ValueError("agent %s 的 mode 不合法: %s" % (name, mode))
                try:
                    mc = int(a.get("max_concurrency") or 1)
                except (TypeError, ValueError):
                    mc = 1
                if mc < 1:
                    mc = 1
                repos = {}
                raw = a.get("repos") or {}
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if k:
                            repos[str(k)] = str(v)
                # 已存在的 agent 保留 created 时间戳与 accept 接单窗口
                created = ""
                accept_window = a.get("accept_window_minutes")
                existing = agents_dir / ("%s.json" % name)
                if existing.exists():
                    try:
                        old = json.loads(existing.read_text(encoding="utf-8"))
                        created = old.get("created", "")
                        if accept_window in (None, ""):
                            accept_window = old.get("accept_window_minutes")
                    except ValueError:
                        created = ""
                cfg = {
                    "name": name,
                    "host": (a.get("host") or "").strip().lower(),
                    "command": a.get("command") or "",
                    "mode": mode,
                    "manual": mode == "manual",
                    "abilities": _as_str_list(a.get("abilities")),
                    "capabilities": _as_str_list(a.get("capabilities")),
                    "repos": repos,
                    "launch": a.get("launch") or "",
                    "worktrees": a.get("worktrees") or "",
                    "max_concurrency": mc,
                    "created": created or _iso_now(),
                }
                if mode == "accept":
                    try:
                        cfg["accept_window_minutes"] = (
                            int(accept_window) if accept_window not in (None, "") else 180)
                    except (TypeError, ValueError):
                        cfg["accept_window_minutes"] = 180
                existing.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                seen.add(name)
            # 删除不在提交集合里的 agent（保留 .gitkeep）
            for p in agents_dir.glob("*.json"):
                if p.name == ".gitkeep":
                    continue
                if p.stem not in seen:
                    p.unlink()

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
            if not isinstance(body, dict):
                self._send_json(400, {"error": "顶层必须是对象"})
                return

            incoming_system = body.get("system")
            if isinstance(incoming_system, dict):
                try:
                    self.apply_system_config(incoming_system)
                except ValueError as exc:
                    self._send_json(400, {"error": "系统配置写入失败: %s" % exc})
                    return

            incoming = body.get("dashboard")
            if isinstance(incoming, dict):
                cur = self.load_dashboard_config()
                cur.update(incoming)   # 合并后持久化到服务端共享默认
                try:
                    CONFIG_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    self._send_json(500, {"error": "配置写入失败: %s" % exc})
                    return
                dashboard = cur
            else:
                dashboard = self.load_dashboard_config()
            self._send_json(200, {"system": self.system_info(), "dashboard": dashboard})

        # ---- /api/dispatch-prompt：只读渲染「可粘贴的派发指令」 ----
        def serve_dispatch_prompt(self):
            """把一张卡渲染成派发指令交给前端复制。

            纯只读：不领卡、不改状态、不写盘。所有失败都回 JSON（400/404/500），
            绝不把异常栈直接甩给浏览器。
            """
            query = parse_qs(urlparse(self.path).query)
            tid = (query.get("task") or [""])[0].strip()
            if not DISPATCH_TID_RE.match(tid):
                self._send_json(400, {"error": "task 必须形如 T-0001，收到: %r" % tid})
                return
            agent = (query.get("agent") or [""])[0].strip()
            if agent and not AGENT_NAME_RE.match(agent):
                self._send_json(400, {"error": "agent 不合法: %s" % agent})
                return
            fmt = ((query.get("format") or ["markdown"])[0].strip().lower()
                   or "markdown")
            if fmt not in DISPATCH_FORMATS:
                self._send_json(400, {"error": "format 只支持 %s，收到: %s"
                                             % ("/".join(DISPATCH_FORMATS), fmt)})
                return
            card_path = Path(TASKS_DIR) / ("%s.md" % tid)
            if not card_path.is_file():
                self._send_json(404, {"error": "任务卡不存在: %s" % tid})
                return
            try:
                card, body = load_card_file(card_path)
                # 状态感知：待合入的卡渲染「给主控的合入指令」，异常卡附带溯源块，
                # 其余状态保持原派发指令（卡上有 repo 时多一行「代码仓」）。快照读不到就当没记录。
                import dispatch_prompt
                record = load_status_record(status_path, tid)
                mode = dispatch_prompt.detect_mode(record)
                text = dispatch_prompt.render_status_prompt(
                    card, record, agent=agent or None,
                    output_format=fmt, body=body)
            except Exception as exc:  # noqa: BLE001 —— 卡面坏掉也是 400，不是 500 裸栈
                self._send_json(400, {"error": "渲染失败: %s" % exc})
                return
            self._send_json(200, {"task": tid, "format": fmt,
                                  "mode": mode, "text": text})

        # ---- /api/status：每次请求都重新读盘 ----
        def serve_status(self):
            try:
                raw = Path(status_path).read_text(encoding="utf-8-sig")
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
            if not isinstance(data, (list, dict)):
                self._send_json(500, {"error": "状态文件根节点应为数组"})
                return
            if isinstance(data, dict):
                # `hive status --json --board` 的形态：{"tasks": [...], "board": {...}}。
                # 只透传这两个键，其余键不猜也不编。
                tasks = data.get("tasks")
                if not isinstance(tasks, list):
                    self._send_json(500, {"error": "状态文件对象缺少 tasks 数组"})
                    return
                self._merge_pushed(tasks)
                self._merge_cluster(tasks)
                payload = {"tasks": tasks}
                if isinstance(data.get("board"), dict):
                    payload["board"] = data["board"]
                self._send_json(200, payload, extra_headers=self._snapshot_headers())
                return
            self._merge_pushed(data)
            self._merge_cluster(data)
            self._send_json(200, data, extra_headers=self._snapshot_headers())

        def _snapshot_headers(self):
            """快照生成时刻（文件 mtime，epoch 秒）。

            为什么需要它：刷新循环一旦停摆，/api/status 仍然 200，页面照旧渲染一份
            几小时前的数据，人却以为它是实时的（2026-10-28 就因此误判「退回没生效」）。
            把 mtime 交给前端，页面才能自己说「这份数据是几分钟前的」。
            """
            try:
                return {"X-Snapshot-Mtime": "%d" % int(os.stat(status_path).st_mtime)}
            except OSError:
                return {}

        def _merge_pushed(self, rows):
            """把 agent HTTP 直推的进度覆盖到快照行上（HTTP 比 git 通道更快）。

            只有 HTTP 的 updated_at 不早于 git 通道的 pushed_at 时才覆盖，
            免得一份旧快照把新进度顶掉。
            """
            pushed = load_pushed()
            if not pushed:
                return
            for row in rows:
                if not isinstance(row, dict):
                    continue
                p = pushed.get(row.get("id"))
                if not isinstance(p, dict):
                    continue
                if (_ts(p.get("updated_at")) is not None
                        and _ts(row.get("pushed_at")) is not None
                        and _ts(p["updated_at"]) < _ts(row["pushed_at"])):
                    continue
                for src, dst in (("progress", "progress"), ("state", "push_state"),
                                 ("step", "push_step"), ("message", "push_message")):
                    if src in p:
                        row[dst] = p[src]
                if p.get("updated_at"):
                    row["pushed_at"] = p["updated_at"]
                row["push_source"] = "http"

        # ---- /api/push：agent 主动上报进度（HTTP 加速通道）----
        def serve_cluster_get(self):
            state, running = load_cluster_state()
            self._send_json(200, {"ok": True, "running": running, "state": state})

        def _merge_cluster(self, rows):
            """把集群里的实时进度覆盖到快照行上。

            与 _merge_pushed 同一套规则：只有更新（updated_at 不早于 git 快照里的
            pushed_at）才覆盖，避免一份旧集群状态把新进度顶回去。
            """
            state, _running = load_cluster_state()
            progress = cluster_progress_map(state)
            if not progress:
                return
            for row in rows:
                if not isinstance(row, dict):
                    continue
                p = progress.get(row.get("id"))
                if not isinstance(p, dict):
                    continue
                p_ts = _ts(p.get("updated_at"))
                row_ts = _ts(row.get("pushed_at"))
                if p_ts is not None and row_ts is not None and p_ts < row_ts:
                    continue
                for src, dst in (("progress", "progress"), ("state", "push_state"),
                                 ("step", "push_step"), ("message", "push_message")):
                    if p.get(src) not in (None, ""):
                        row[dst] = p[src]
                if p.get("updated_at"):
                    row["pushed_at"] = p["updated_at"]
                row["push_source"] = "cluster"

        def serve_push(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON: %s" % exc})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"ok": False, "error": "顶层必须是对象"})
                return
            tid = (body.get("task") or body.get("tid") or "").strip()
            if not TID_RE.match(tid):
                self._send_json(400, {"ok": False, "error": "task 不合法: %s" % tid})
                return
            agent = (body.get("agent") or "").strip()
            if agent and not AGENT_NAME_RE.match(agent):
                self._send_json(400, {"ok": False, "error": "agent 不合法: %s" % agent})
                return
            prog = body.get("progress")
            try:
                prog = None if prog is None else max(0, min(100, int(prog)))
            except (TypeError, ValueError):
                prog = None
            data = load_pushed()
            cur = data[tid] if isinstance(data.get(tid), dict) else {}
            cur["task"] = tid
            if agent:
                cur["agent"] = agent
            if body.get("state"):
                cur["state"] = str(body["state"])
            if prog is not None:
                cur["progress"] = prog
            if body.get("step"):
                cur["step"] = str(body["step"])
            if body.get("message"):
                cur["message"] = str(body["message"])
            cur["updated_at"] = body.get("updated_at") or _iso_now()
            data[tid] = cur
            save_pushed(data)
            self._send_json(200, {"ok": True, "pushed": cur})

        # ---- /api/chat：跨机留言（读免鉴权，写要 token，与 /api/task 同口径）----
        def serve_chat_get(self):
            q = parse_qs(urlparse(self.path).query)
            try:
                since = float((q.get("since") or ["0"])[0] or 0)
            except (TypeError, ValueError):
                since = 0.0
            try:
                limit = int((q.get("limit") or ["200"])[0] or 200)
            except (TypeError, ValueError):
                limit = 200
            limit = max(1, min(limit, 500))
            scope = (q.get("scope") or [""])[0]
            ok, body = _cluster_chat_http(
                "GET", {"since": "%.6f" % since, "limit": str(limit), "scope": scope})
            if ok and isinstance(body, dict) and body.get("ok"):
                self._send_json(200, {
                    "ok": True, "source": "cluster",
                    "messages": body.get("messages") or [],
                    "last_epoch": body.get("last_epoch") or 0.0,
                })
                return
            # 集群没跑：退回本地文件。看板因此永远有得看、有得发。
            msgs = chat_local_read(since=since, scope=scope, limit=limit)
            self._send_json(200, {
                "ok": True, "source": "local", "offline": True,
                "messages": msgs,
                "last_epoch": max([float(m.get("epoch") or 0) for m in msgs] or [0.0]),
            })

        def serve_chat_post(self):
            length = self._read_length()
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON: %s" % exc})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"ok": False, "error": "顶层必须是对象"})
                return
            who = self.require_write_auth()
            if who is None:
                return
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json(400, {"ok": False, "error": "text 不能为空"})
                return
            scope = norm_chat_scope(body.get("scope"))
            kind = str(body.get("kind") or "note")
            if kind not in CHAT_KINDS:
                kind = "note"
            sender = str(body.get("from") or "").strip()
            if not sender:
                agent = str((who or {}).get("agent") or "").strip()
                sender = ("human:%s" % agent) if agent and agent != "loopback" else "human"
            payload = {
                "text": text[:CHAT_MAX_TEXT],
                "from": sender,
                "scope": scope,
                "kind": kind,
                "reply_to": str(body.get("reply_to") or ""),
            }

            # 计划指令（/plan … 或 @command:plan …）：不当作普通留言存掉，
            # 先执行再把「指令 + 结果」作为一条系统留言发回同一频道 ——
            # 这样即使发起的人关了页面，频道里也留得下这次操作的痕迹。
            try:
                ps = plan_module()
                is_cmd = ps.looks_like_plan_command(text)
            except ImportError:
                ps, is_cmd = None, False
            if is_cmd:
                self.serve_plan_command(text, ps, who, scope, sender)
                return

            ok, resp = _cluster_chat_http("POST", None, payload)
            if ok and isinstance(resp, dict) and resp.get("ok") and resp.get("message"):
                append_audit({"action": "chat.post", "ok": True, "agent": sender,
                              "source": self.client_address[0],
                              "detail": "%s %s" % (scope, payload["text"][:60])})
                self._send_json(200, {"ok": True, "source": "cluster",
                                      "message": resp["message"]})
                return
            msg = self.deliver_chat_local(payload)
            if msg is None:
                self._send_json(500, {"ok": False, "error": "本地留言文件写不进去"})
                return
            self._send_json(200, {"ok": True, "source": "local", "offline": True,
                                  "message": msg})

        def deliver_chat_local(self, payload):
            msg = {
                "id": "%s-%d" % (socket.gethostname().split(".")[0], time.time_ns()),
                "ts": _iso_now(),
                "epoch": time.time(),
                "from": payload["from"],
                "node": "dashboard-local",
                "node_name": "local",
                "scope": payload["scope"],
                "kind": payload.get("kind") or "note",
                "text": payload["text"],
                "reply_to": payload.get("reply_to") or "",
            }
            return msg if chat_local_append(msg) else None

        def post_chat_note(self, text, scope, sender="hive-plan"):
            """把一条系统留言发到频道：先试集群，集群不在就落本地。"""
            payload = {"text": text[:CHAT_MAX_TEXT], "from": sender, "scope": scope,
                       "kind": "note", "reply_to": ""}
            ok, resp = _cluster_chat_http("POST", None, payload)
            if ok and isinstance(resp, dict) and resp.get("ok"):
                return resp.get("message")
            return self.deliver_chat_local(payload)

        def serve_plan_command(self, text, ps, who, scope, sender):
            """执行一条计划指令：结果既写审计，也作为一条系统留言回到频道。"""
            who = _plan_actor(who)
            result = ps.run_command(text, who, load_status_map(status_path))
            ok = bool(result.get("ok"))
            if ok:
                reply = "📋 %s" % (result.get("message") or "计划指令已执行")
            else:
                reply = "📋 计划指令失败：%s\n%s" % (result.get("error") or "未知原因",
                                                  result.get("usage") or "")
            self.post_chat_note(reply, scope)
            append_audit({"action": "chat.plan-cmd", "ok": ok, "agent": sender,
                          "role": who.get("role"), "source": self.client_address[0],
                          "plan": ((result.get("plan") or {}).get("id") or ""),
                          "detail": text[:120]})
            if not ok:
                self._send_json(result.get("code") or 400, {
                    "ok": False, "error": result.get("error") or "计划指令失败",
                    "usage": result.get("usage") or ""})
                return
            self._send_json(200, {"ok": True, "plan_command": True,
                                  "message": result.get("message") or "",
                                  "plan": result.get("plan"),
                                  "reply": reply})

        # ---- /api/plans：任务计划（计划 Tab 的唯一读写口）----
        #
        # 读：GET /api/plans[?include_deleted=1&status=active]
        #     顺带按当前快照做一次自动完成同步（满足规则的计划直接进「已完成」），
        #     这样哪怕页面关着，agent 交完最后一张卡再打开也能看到计划已完成。
        # 写：POST /api/plans {action, ...}；权限 = 创建者本人 或 controller。
        def serve_plans_get(self):
            q = parse_qs(urlparse(self.path).query)
            include_deleted = (q.get("include_deleted") or ["0"])[0] in ("1", "true", "yes")
            status = (q.get("status") or [""])[0].strip() or None
            try:
                ps = plan_module()
            except ImportError as exc:
                self._send_json(500, {"ok": False, "error": "计划模块不可用: %s" % exc})
                return
            tasks_by_id = load_status_map(status_path)
            consumed = self.consume_plan_commands(ps, tasks_by_id)
            try:
                _progress, auto_done = ps.sync_progress(tasks_by_id, return_completed=True)
                for pid in auto_done:
                    # 自动完成也是一次状态变更：写一行审计，事后能解释「谁把它完成的」
                    append_audit({"action": "plan.auto-complete", "plan": pid, "ok": True,
                                  "agent": "rule", "role": "system",
                                  "source": self.client_address[0],
                                  "detail": "关联任务全部完成，按规则自动完成"})
                items = ps.list_plans(tasks_by_id, include_deleted=include_deleted,
                                     status=status)
            except ps.PlanError as exc:
                self._send_json(exc.code, {"ok": False, "error": exc.message})
                return
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": "计划文件读写失败: %s" % exc})
                return
            counts = {"active": 0, "completed": 0, "closed": 0}
            for plan in items:
                if plan["status"] in counts:
                    counts[plan["status"]] += 1
            payload = {"ok": True, "plans": items, "counts": counts,
                       "source": "plan_store"}
            if consumed:
                payload["commands"] = consumed
            self._send_json(200, payload)

        def consume_plan_commands(self, ps, tasks_by_id):
            """把集群留言里新到的计划指令执行掉（任意机器发的都算）。

            agent 走 `hive chat post` / MCP，消息直接进集群、不经过看板输入框；
            计划真身只在本机，所以由看板在读留言时补上这一步。

            增量游标：只问「上次之后」的留言。首次运行直接把游标推到当前最新，
            **不**回头执行历史留言 —— 否则一装上就会把几个月前聊天里随手写的
            `/plan …` 全部重放一遍。消息 id 账本是第二道保险（防止游标丢失后重放）。
            """
            global _INBOUND_CURSOR, _INBOUND_READY
            since = _INBOUND_CURSOR
            try:
                ok, body = _cluster_chat_http(
                    "GET", {"since": "%.6f" % since, "limit": "200"})
                if not ok or not isinstance(body, dict) or not body.get("ok"):
                    return []
                msgs = body.get("messages") or []
                last = float(body.get("last_epoch") or 0.0)
                if not _INBOUND_READY:
                    # 首次：只记位置，不执行历史
                    _INBOUND_READY = True
                    _INBOUND_CURSOR = max(last, since)
                    return []
                if not msgs:
                    _INBOUND_CURSOR = max(last, since)
                    return []
                results = ps.apply_inbound(msgs, tasks_by_id)
                _INBOUND_CURSOR = max([last, since] + [
                    float(m.get("epoch") or 0) for m in msgs if isinstance(m, dict)])
            except Exception as exc:  # noqa: BLE001  指令消费失败不能拖垮读接口
                sys.stderr.write("[hive-dashboard] 计划指令消费失败: %s\n" % exc)
                return []
            for res in results:
                append_audit({"action": "chat.plan-cmd", "ok": bool(res.get("ok")),
                              "agent": res.get("from"), "role": "inbound",
                              "plan": ((res.get("plan") or {}).get("id") or ""),
                              "msg": res.get("message_id"),
                              "detail": (res.get("message") or res.get("error") or "")[:200]})
                if res.get("ok"):
                    self.post_chat_note("📋 %s" % res.get("message"), res.get("scope") or "hall")
            return [{"id": r.get("message_id"), "ok": bool(r.get("ok")),
                     "message": r.get("message") or r.get("error") or ""} for r in results]

        def serve_plans_post(self):
            length = self._read_length()
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON: %s" % exc})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"ok": False, "error": "顶层必须是对象"})
                return

            action = str(body.get("action") or "").strip()
            who = self.require_write_auth()
            if who is None:
                append_audit({"action": "plan.%s" % (action or "?"), "ok": False,
                              "source": self.client_address[0], "detail": "未鉴权"})
                return
            who = _plan_actor(who)

            try:
                ps = plan_module()
            except ImportError as exc:
                self._send_json(500, {"ok": False, "error": "计划模块不可用: %s" % exc})
                return

            plan_id = str(body.get("plan_id") or body.get("id") or "").strip()
            audit_base = {"action": "plan.%s" % (action or "?"), "plan": plan_id,
                          "agent": who.get("agent"), "role": who.get("role"),
                          "source": self.client_address[0]}
            try:
                if action == "create":
                    plan = ps.create_plan(
                        body.get("prefix"), body.get("title"),
                        body.get("description") or "",
                        task_ids=body.get("task_ids"),
                        created_by=who.get("agent"),
                        pinned=bool(body.get("pinned")),
                        auto_complete=body.get("auto_complete") is not False)
                    append_audit({**audit_base, "plan": plan["id"], "ok": True,
                                  "detail": "新建 %s %s" % (plan["prefix"], plan["title"])})
                    self._send_json(200, {"ok": True, "plan": plan})
                    return
                if action == "update":
                    fields = body.get("fields")
                    if not isinstance(fields, dict):
                        # 也允许把字段直接铺在顶层（前端表单更省事）
                        fields = {k: body[k] for k in
                                  ("prefix", "title", "description", "task_ids",
                                   "pinned", "auto_complete", "status") if k in body}
                    plan = ps.update_plan(plan_id, who, fields)
                    append_audit({**audit_base, "ok": True,
                                  "detail": "更新 %s" % ",".join(sorted(fields.keys()))})
                    self._send_json(200, {"ok": True, "plan": plan})
                    return
                if action == "complete":
                    plan = ps.complete_plan(plan_id, who)
                elif action == "close":
                    plan = ps.close_plan(plan_id, who)
                elif action == "delete":
                    plan = ps.delete_plan(plan_id, who, confirm=bool(body.get("confirm")))
                elif action in ("pin", "unpin"):
                    plan = ps.set_pinned(plan_id, who, action == "pin")
                else:
                    append_audit({**audit_base, "ok": False, "detail": "不支持的动作"})
                    self._send_json(400, {"ok": False,
                                          "error": "不支持的计划操作: %s" % (action or "（空）")})
                    return
            except ps.PlanError as exc:
                append_audit({**audit_base, "ok": False, "code": exc.code,
                              "detail": exc.message})
                self._send_json(exc.code, {"ok": False, "error": exc.message})
                return
            except OSError as exc:
                append_audit({**audit_base, "ok": False, "detail": "写盘失败"})
                self._send_json(500, {"ok": False, "error": "计划文件写入失败: %s" % exc})
                return
            append_audit({**audit_base, "ok": True,
                          "detail": "%s %s" % (plan["id"], plan["title"])})
            self._send_json(200, {"ok": True, "plan": plan})

        # ---- /api/task：看板按钮 -> 调用 hive CLI 真正执行任务操作 ----
        def serve_task_post(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON: %s" % exc})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"ok": False, "error": "顶层必须是对象"})
                return

            action = body.get("action")
            tid = (body.get("tid") or "").strip()
            if action not in TASK_ACTIONS:
                self._send_json(400, {"ok": False, "error": "不支持的操作: %s" % action})
                return
            if not TID_RE.match(tid):
                self._send_json(400, {"ok": False, "error": "任务号不合法: %s" % tid})
                return
            who = self.require_write_auth()
            if who is None:
                append_audit({"action": action, "task": tid, "ok": False,
                              "source": self.client_address[0], "detail": "未鉴权"})
                return
            # close/reopen 是不可逆或半不可逆的运维动作，只许 controller 角色做；
            # 搁置三件套同理（会影响 agent 能领到什么卡）。
            # retry/unassign 退回待领取，worker 角色自己做自己的卡也合理。
            if action in ("close", "reopen") + SHELF_ACTIONS and not self.is_controller(who):
                self._send_json(403, {
                    "ok": False,
                    "error": "%s 需要 controller 角色（当前 %s）"
                             % (action, who.get("agent") or "?")})
                append_audit({"action": action, "task": tid, "ok": False,
                              "agent": who.get("agent"), "detail": "角色不足"})
                return

            note = str(body.get("note") or body.get("reason") or "看板操作")
            argv = hive_argv()
            if action == "close":
                # 有 result 记 done、没有记 closed（作废）由 hive 自己判定；
                # 前端要"强制完成/强制作废"时显式传 done=true/false。
                argv += ["close", tid, "--note", note]
                forced = body.get("done")
                if forced is True:
                    argv += ["--as-done"]
                elif forced is False:
                    argv += ["--as-cancelled"]
            elif action == "retry":
                # agent 可选：不传就是「原 agent 用原分支重跑」（hive retry 保留 owner）
                agent = (body.get("agent") or "").strip()
                if agent and not AGENT_NAME_RE.match(agent):
                    self._send_json(400, {"ok": False,
                                          "error": "agent 不合法: %s" % agent})
                    return
                argv += ["retry", tid]
                if agent:
                    argv += ["--agent", agent]
                if body.get("fresh") is True:
                    argv += ["--fresh"]
            elif action == "reopen":
                argv += ["reopen", tid, "--reason", note]
            elif action in SHELF_ACTIONS:
                # 搁置 / 取消搁置 / 另立新卡：前两个走独立 CLI，第三个是
                # unshelve --spawn-new（原卡保持搁置，只派生一张新 ready 卡）。
                argv += ["shelve" if action == "shelve" else "unshelve",
                         tid, "--reason", note]
                if action == "respawn":
                    title = str(body.get("title") or "").strip()
                    # 标题会原样写进卡面 frontmatter，换行/控制字符能把 YAML 打坏
                    if (not title or len(title) > SHELF_TITLE_MAX
                            or any(ord(c) < 32 for c in title)):
                        self._send_json(400, {
                            "ok": False,
                            "error": "新卡标题不能为空、不能含控制字符，且不超过 %d 字"
                                     % SHELF_TITLE_MAX})
                        return
                    argv += ["--spawn-new", "--title", title]
            else:  # unassign
                argv += ["unassign", tid, "--reason", note]

            try:
                proc = subprocess.run(argv, capture_output=True, text=True,
                                     encoding="utf-8", env=PYTHON_ENV,
                                     timeout=HIVE_OP_TIMEOUT, **no_window_kwargs())
            except FileNotFoundError:
                append_audit({"action": action, "task": tid, "ok": False,
                              "agent": who.get("agent"), "detail": "找不到 hive.py"})
                self._send_json(500, {"ok": False, "error": "找不到 hive.py: %s" % HIVE_PY})
                return
            except subprocess.TimeoutExpired:
                append_audit({"action": action, "task": tid, "ok": False,
                              "agent": who.get("agent"), "detail": "超时"})
                self._send_json(504, {"ok": False,
                                      "error": "操作超时（%d 秒）" % HIVE_OP_TIMEOUT})
                return

            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                append_audit({"action": action, "task": tid, "ok": False,
                              "agent": who.get("agent"), "note": note,
                              "code": proc.returncode,
                              "detail": (err or out or "")[-300:],
                              "source": self.client_address[0]})
                self._send_json(200, {"ok": False, "code": proc.returncode,
                                      "error": (err or out or "命令执行失败")[-600:]})
                return
            append_audit({"action": action, "task": tid, "ok": True,
                          "agent": who.get("agent"), "role": who.get("role"),
                          "note": note, "source": self.client_address[0]})
            # 成功：重新生成状态文件，让看板即时反映变化
            self._refresh_status()
            resp = {"ok": True, "output": out[-600:]}
            # 另立新卡时把新任务号回给前端，toast 里能直接点出来
            m = re.search(r"NEW_TASK=(T-\d{4,})", out)
            if m:
                resp["new_task"] = m.group(1)
            self._send_json(200, resp)

        def _refresh_status(self):
            """操作成功后重跑 `hive status --json` 覆盖状态文件，使看板即时更新。"""
            try:
                proc = subprocess.run(
                    hive_argv("status", "--json"),
                    capture_output=True, text=True, encoding="utf-8",
                    env=PYTHON_ENV, timeout=HIVE_OP_TIMEOUT,
                    **no_window_kwargs())
                if proc.returncode == 0 and proc.stdout.strip():
                    write_status_atomic(status_path, proc.stdout)
                else:
                    sys.stderr.write("[hive-dashboard] 状态重生成失败 rc=%s\n"
                                     % proc.returncode)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write("[hive-dashboard] 状态重生成异常: %s\n" % exc)

        # ---- 日志：静音常规访问，只保留 4xx/5xx ----
        def log_message(self, fmt, *args):
            if getattr(self, "_last_code", 200) >= 400:
                sys.stderr.write("[hive-dashboard] %s\n" % (fmt % args))

    return HiveHandler


def main(argv=None):
    parser = argparse.ArgumentParser(description="蜂巢 Hive 看板静态服务器（零依赖）")
    parser.add_argument("--status", default="sample-status.json",
                        help="hive status --json 导出的状态文件路径（默认 sample-status.json）")
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT,
                        help=f"监听端口（默认 {DEFAULT_DASHBOARD_PORT}）")
    parser.add_argument("--host", default=None,
                        help="监听地址（默认 $HIVE_DASHBOARD_HOST，缺省 0.0.0.0；"
                             "收回本机写 127.0.0.1）")
    parser.add_argument("--auto-refresh", dest="auto_refresh", action="store_true",
                        help="后台定期跑 hive status --json 重生成状态文件（辅通道）")
    parser.add_argument("--refresh-interval", dest="refresh_interval", type=int, default=30,
                        help="--auto-refresh 的间隔秒数（默认 30，最小 5）")
    parser.add_argument("--auth", choices=["auto", "on", "off"], default=None,
                        help="写操作鉴权：auto=非回环绑定就要 token（默认）、"
                             "on=始终要 token、off=关闭（也可用 $HIVE_DASH_AUTH）")
    args = parser.parse_args(argv)

    # 绑定地址：命令行 > 环境变量 > hive_ports 默认值（不在本文件写第二套默认）
    if not args.host:
        args.host = (os.environ.get(DASHBOARD_HOST_ENV) or "").strip() \
            or DEFAULT_DASHBOARD_HOST

    if args.auth:
        os.environ[AUTH_MODE_ENV] = args.auth
    write_auth = _auth_mode(args.host)

    status_path = str(Path(args.status).expanduser())
    if args.auto_refresh:
        threading.Thread(target=refresh_loop,
                         args=(status_path, args.refresh_interval), daemon=True).start()
        print("自动刷新: 每 %d 秒重生成状态文件" % max(5, args.refresh_interval))
    handler = build_handler(status_path, write_auth=write_auth)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print("http://%s:%d" % ("127.0.0.1" if args.host in ("0.0.0.0", "") else args.host, args.port))
    print("状态文件: %s" % status_path)
    if write_auth == "off":
        print("写操作: 免鉴权（本机回环）")
    else:
        print("写操作: 需要 token（hive agent enroll <controller> 生成，右上角登录）")
        print("审计日志: %s" % _audit_log_path())
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
