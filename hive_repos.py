#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机代码仓路径解析（`sync-repos` / `land` / `prune` 共用）。

为什么需要它
------------
同一个逻辑仓名在不同机器上路径、目录名都可能不同：

    FreeBSD  /home/skywalk/dswork/hive
    Windows  G:/dswork/duan-light-merge/hive

旧逻辑（T-0147）只在 ``agents/*.json`` 里找「host 匹配 + 路径存在」。一旦
机器名变了（新 jail / 新主机 / hostname 被改）、挂载点变了，或者某台机器
压根没登记 agent，就会退化到「任意机器声明的第一个存在的路径」：
轻则 ``sync-repos`` 报一堆「本机路径不存在」（信息是别的机器的），
重则快进到同名但无关的目录 —— 这就是「收到错误的包信息」。

解析顺序（先本地、后共享；每一步都验证是不是 git 仓）
------------------------------------------------------
1. 环境变量 ``HIVE_REPO_<NAME>``          —— 人当场指定，最高优先；
2. 本机记忆 ``~/.hive/repo-bindings.json`` —— 本机自己记住/问过用户的选择；
3. 本机 agent 登记（host 匹配）            —— 显式配置，路径必须是 git 仓；
4. 协调仓自身（名字 ``hive``）             —— 它就是 hive 仓；
5. 工作区扫描                              —— 按 git remote 识别（目录名只是弱证据），
                                             同一工作区里优先浅层；
6. 主控记忆 ``<协调仓>/repo-paths.json``    —— 本机没有 agent 节点时，主控替它记；
7. 还得不出唯一答案 → **不猜**：``status=ambiguous`` / ``missing``，
   候选一并返回，由调用方问用户或提示 ``hive repo bind``。

工作区约定：所有代码仓放同一个工作目录（默认 ``~/dswork``，可用
``HIVE_WORKSPACE`` 或 config.json 的 ``workspace`` 指定），目录名可以不等于
仓名（如 ``light-merge`` 对应仓 ``light``），靠 git remote 识别。
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

# hive_lsa 是 H-LSA 节拍的唯一真源（T-0350）：T2 周期、±20% jitter、
# 三档常量都从这里取；残缺部署（只拷了几个 py 文件）时退回内置等价实现。
try:
    import hive_lsa as _lsa
except ImportError:  # pragma: no cover - 只在残缺部署上走到
    _lsa = None

ENV_WORKSPACE = "HIVE_WORKSPACE"
ENV_BINDINGS = "HIVE_REPO_BINDINGS"
ENV_NO_PROMPT = "HIVE_NO_PROMPT"

# 扫描保护：家目录下可能有很大的无关目录树；命中上限就停，宁可让用户
# 用 `hive repo bind` 明确指定，也不把一条命令拖成几分钟。
MAX_SCAN_DIRS = 4000
MAX_SCAN_DEPTH = 2
SKIP_DIR_NAMES = {
    "node_modules", "__pycache__", ".git", ".hg", ".svn", "venv", ".venv",
    "env", "site-packages", "build", "dist", "target", "coverage", "htmlcov",
}


# --------------------------------------------------------------------------- #
# 路径/远端基础判断
# --------------------------------------------------------------------------- #

def is_git_repo(path) -> bool:
    """path 本身是不是 git 仓（.git 目录，或 worktree 的 .git 文件）。

    故意不做 `rev-parse --show-toplevel`：那会把「仓里的子目录」也判成仓，
    而这里要的是仓根；只看 path 自己有没有 .git 既快又不会误判子目录。
    """
    p = Path(path)
    try:
        return p.is_dir() and ((p / ".git").is_dir() or (p / ".git").is_file())
    except OSError:
        return False


_GIT_CACHE: dict[tuple[str, str], str] = {}


def _git(args: list[str], cwd) -> str:
    key = (str(cwd), " ".join(args))
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(cwd), text=True, capture_output=True,
            timeout=5, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        out = (p.stdout or "").strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        out = ""
    _GIT_CACHE[key] = out
    return out


def remote_url(path) -> str:
    """取 origin 的 URL；没有 origin 就取第一个 remote。失败返回空串。"""
    url = _git(["remote", "get-url", "origin"], path)
    if url:
        return url
    first = _git(["remote"], path).splitlines()
    if first:
        return _git(["remote", "get-url", first[0].strip()], path)
    return ""


def repo_basename(url: str) -> str:
    """从远端 URL / 本地路径里取仓名（去 .git、小写）。

    兼容 ``https://host/group/hive.git``、``git@host:group/hive.git``、
    ``/tmp/light.git``、``G:/dswork/light`` 以及带反斜杠的 Windows 路径。
    """
    u = (url or "").strip().replace("\\", "/").rstrip("/")
    if not u:
        return ""
    if u.lower().endswith(".git"):
        u = u[:-4].rstrip("/")
    tail = u.rsplit("/", 1)[-1]
    if ":" in tail:                     # scp 风格 host:repo（repo 里没有斜杠）
        tail = tail.rsplit(":", 1)[-1]
    return tail.strip().lower()


def matches_name(name: str, path, url: str = "") -> str:
    """这个路径像不像逻辑仓 ``name``：返回 ``remote`` / ``dir`` / ``""``。

    ``remote`` 是强证据（仓名一致，目录可以叫任何名字）；
    ``dir`` 是弱证据（目录名一致但远端不同/没有远端）。
    """
    low = (name or "").strip().lower()
    if not low:
        return ""
    if url and repo_basename(url) == low:
        return "remote"
    try:
        if Path(path).name.strip().lower() == low:
            return "dir"
    except (OSError, ValueError):
        pass
    return ""


def env_override(name: str) -> str:
    """``HIVE_REPO_HIVE`` / ``HIVE_REPO_LIGHT_PRISM`` 形式的显式指定。"""
    key = "HIVE_REPO_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    return (os.environ.get(key) or "").strip()


# --------------------------------------------------------------------------- #
# 本机记忆
# --------------------------------------------------------------------------- #

def default_bindings_path() -> Path:
    env = (os.environ.get(ENV_BINDINGS) or "").strip()
    if env:
        return Path(env).expanduser()
    state = (os.environ.get("HIVE_STATE") or "").strip()
    root = Path(state).expanduser() if state else Path.home() / ".hive"
    return root / "repo-bindings.json"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class BindingStore:
    """本机代码仓记忆：``{hosts: {host: {name: {path, remote, source, bound_at}}}}``。

    按 host 分桶而不是平铺：家目录可能被多台机器共享（NFS/漫游配置），
    另一台机器的路径绝不能拿来当本机的答案 —— 那正是本模块要消灭的 bug。
    """

    def __init__(self, path: Path | None = None, host: str = ""):
        self.path = Path(path) if path else default_bindings_path()
        self.host = (host or "").strip().lower() or "unknown"
        self.data = self._load()
        self.writable = True

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        hosts = data.get("hosts")
        if not isinstance(hosts, dict):
            hosts = {}
        data["hosts"] = hosts
        data.setdefault("version", 1)
        return data

    def _section(self) -> dict:
        sec = self.data["hosts"].setdefault(self.host, {})
        if not isinstance(sec, dict):       # 脏数据：重建该 host 段
            sec = {}
            self.data["hosts"][self.host] = sec
        return sec

    def get(self, name: str) -> dict | None:
        entry = self._section().get(name)
        if isinstance(entry, str):          # 兼容早期「只有路径」的写法
            return {"path": entry}
        return entry if isinstance(entry, dict) else None

    def all(self) -> dict[str, dict]:
        out = {}
        for name, entry in self._section().items():
            if isinstance(entry, str):
                out[name] = {"path": entry}
            elif isinstance(entry, dict):
                out[name] = entry
        return out

    def set(self, name: str, path, remote: str = "", source: str = "") -> dict:
        entry = {
            "path": str(Path(path).expanduser()),
            "remote": remote or "",
            "source": source or "manual",
            "bound_at": _now_iso(),
        }
        self._section()[name] = entry
        self._save()
        return entry

    def remove(self, name: str) -> bool:
        sec = self._section()
        if name not in sec:
            return False
        del sec[name]
        self._save()
        return True

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            self.writable = False
            raise OSError(f"写不了本机仓记忆 {self.path}: {e}") from e


# --------------------------------------------------------------------------- #
# 工作区扫描
# --------------------------------------------------------------------------- #

def default_roots(hive_dir=None, extra_roots=None) -> list[Path]:
    """扫描根（有序）。

    显式 ``HIVE_WORKSPACE``（或 config.json 的 ``workspace``）一旦给出，
    就**只扫这些根**：这是人和测试用来钉死「本机工作目录」的开关，
    不能被家目录下的历史目录带偏。

    否则依次尝试：显式 extra → ``~/dswork`` → ``/workspace``（容器/jail 约定）
    → 协调仓所在目录 → 家目录。
    """
    srcs: list[str] = []
    env = (os.environ.get(ENV_WORKSPACE) or "").strip()
    if env:
        # 分号/逗号都接受；Windows 盘符里的冒号不会出现在分隔位置。
        srcs.extend(x for x in re.split(r"[;,]", env) if x.strip())
    else:
        srcs.extend(str(p) for p in (extra_roots or []))
        srcs.append(str(Path.home() / "dswork"))
        srcs.append("/workspace")
    if not env:
        if hive_dir is not None:
            srcs.append(str(Path(hive_dir).expanduser().parent))
        srcs.append(str(Path.home()))

    out: list[Path] = []
    for s in srcs:
        try:
            p = Path(s).expanduser()
            if not p.is_dir():
                continue
            rp = p.resolve()
        except (OSError, ValueError):
            continue
        if rp in out:
            continue
        # 已被前面的根覆盖（子目录）就不重复扫；但后面更宽的根（如家目录）
        # 不能把前面更具体的工作区（如 ~/dswork）从优先级里挤掉 —— 保留它，
        # 重复的候选按路径去重时仍用第一次（更靠前）的 root 编号。
        if any(rp == o or rp.is_relative_to(o) for o in out):
            continue
        out.append(rp)
    return out


def scan_repos(roots: list[Path], max_depth: int = MAX_SCAN_DEPTH) -> list[dict]:
    """扫描工作区里的 git 仓，返回 ``[{path, remote, root, depth}]``。

    只认「目录自己带 .git」的仓根；进到仓里就不再往下走（子目录/嵌套 worktree
    由解析阶段再判）。隐藏目录、node_modules、venv 等一律跳过。
    """
    found: list[dict] = []
    seen: set[str] = set()

    def add(p: Path, root_i: int, depth: int) -> None:
        key = str(p)
        if key in seen:
            return
        seen.add(key)
        found.append({"path": p, "remote": remote_url(p),
                      "root": root_i, "depth": depth})

    for root_i, root in enumerate(roots):
        if is_git_repo(root):
            add(root, root_i, 0)
            continue
        budget = MAX_SCAN_DIRS
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and budget > 0:
            directory, depth = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                if budget <= 0:
                    break
                name = entry.name
                if name.startswith(".") or name in SKIP_DIR_NAMES:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                budget -= 1
                p = Path(entry.path)
                if is_git_repo(p):
                    add(p, root_i, depth + 1)
                    continue
                if depth + 1 < max_depth:
                    stack.append((p, depth + 1))
    return found


def _pick_candidate(cands: list[dict]) -> tuple[dict | None, list[dict]]:
    """从同名候选里挑唯一答案；挑不出来就返回候选让上层问人。

    规则刻意保守：
    * 先看最早的扫描根（工作区优先级）；
    * 同一根内优先 ``remote`` 强匹配，再按目录深度，浅层优先
      （「所有仓都放同一个工作目录」的习惯）；
    * 仍并列 → 不猜。
    """
    if not cands:
        return None, []
    best_root = min(c["root"] for c in cands)
    same_root = [c for c in cands if c["root"] == best_root]
    strong = [c for c in same_root if c["kind"] == "remote"]
    pool = strong or same_root
    best_depth = min(c["depth"] for c in pool)
    top = [c for c in pool if c["depth"] == best_depth]
    if len(top) == 1:
        return top[0], top
    return None, top


def candidates_for(name: str, index: list[dict]) -> list[dict]:
    out = []
    for c in index:
        kind = matches_name(name, c["path"], c.get("remote") or "")
        if not kind:
            continue
        out.append({**c, "kind": kind})
    out.sort(key=lambda c: (c["root"], 0 if c["kind"] == "remote" else 1,
                            c["depth"], str(c["path"])))
    return out


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def _public(cand: dict) -> dict:
    return {"path": str(cand["path"]), "remote": cand.get("remote") or "",
            "kind": cand.get("kind") or "", "depth": cand.get("depth", 0),
            "root": cand.get("root", 0)}


def _ok(name: str, path, source: str, verified: bool,
        notes: list[str] | None = None,
        candidates: list[dict] | None = None) -> dict:
    return {"name": name, "path": str(Path(path).expanduser()),
            "source": source, "verified": bool(verified), "status": "ok",
            "candidates": [_public(c) for c in (candidates or [])],
            "notes": list(notes or [])}


def _one(name: str, *, hive_dir, local_paths, controller, bindings, index_get,
         self_name: str) -> dict:
    notes: list[str] = []

    # 1) 环境变量：人当场指定的，优先于一切记忆
    env_path = env_override(name)
    if env_path:
        p = Path(env_path).expanduser()
        if is_git_repo(p):
            url = remote_url(p)
            return _ok(name, p, "env",
                       bool(url) and matches_name(name, p, url) == "remote", notes)
        notes.append(f"环境变量指定的路径不是 git 仓：{p}")

    # 2) 本机记忆
    entry = bindings.get(name) if bindings is not None else None
    if entry and entry.get("path"):
        p = Path(str(entry["path"])).expanduser()
        if is_git_repo(p):
            url = remote_url(p)
            kind = matches_name(name, p, url)
            if url and kind != "remote":
                notes.append(f"本机记忆的路径 remote={url} 与仓名 {name} 不符，"
                             "仍按记忆使用；不对就 hive repo unbind 后重新 bind")
            return _ok(name, p, "local-memory", kind == "remote", notes)
        notes.append(f"本机记忆里的路径已不可用（不是 git 仓或已删除）：{p}")

    # 3) 本机 agent 登记（显式配置）；存在但不是 git 仓的路径必须忽略，
    #    旧逻辑只判 exists，正是「快进到无关目录」的来源之一。
    lp = (local_paths or {}).get(name)
    if lp:
        p = Path(lp).expanduser()
        if is_git_repo(p):
            url = remote_url(p)
            kind = matches_name(name, p, url)
            if url and kind != "remote":
                notes.append(f"本机 agent 登记的 remote={url} 与仓名 {name} 不符，"
                             "仍按登记使用；不对就 hive repo bind 改掉")
            return _ok(name, p, "agent", kind == "remote", notes)
        if p.exists():
            notes.append(f"本机 agent 登记的路径存在但不是 git 仓，已忽略：{p}")
        else:
            notes.append(f"本机 agent 登记的路径不存在：{p}")

    # 4) 协调仓自身就是 hive 仓
    if name == (self_name or "hive") and hive_dir and is_git_repo(hive_dir):
        return _ok(name, hive_dir, "coord", True, notes)

    # 5) 工作区扫描（按 remote 认仓，目录名只是弱证据）
    cands = candidates_for(name, index_get())
    chosen, top = _pick_candidate(cands)
    if chosen is not None:
        notes.append(f"按工作区扫描确认：{chosen['path']}"
                     f"（{chosen['kind']} 匹配）")
        return _ok(name, chosen["path"], "workspace-scan",
                   chosen["kind"] == "remote", notes, cands)
    if top:
        return {"name": name, "path": None, "source": "", "verified": False,
                "status": "ambiguous",
                "candidates": [_public(c) for c in top], "notes": notes}

    # 6) 主控记忆（本机没有 agent 节点时，主控替它记的那份）
    cp = (controller or {}).get(name)
    if cp:
        p = Path(cp).expanduser()
        if is_git_repo(p):
            url = remote_url(p)
            return _ok(name, p, "controller-memory",
                       matches_name(name, p, url) == "remote", notes)
        notes.append(f"主控记忆里的路径在本机不可用（不是 git 仓或不存在）：{p}")

    # 7) 不猜
    return {"name": name, "path": None, "source": "", "verified": False,
            "status": "missing", "candidates": [], "notes": notes}


def resolve_all(names, *, hive_dir, local_paths=None, controller=None,
                bindings=None, roots=None, self_name: str = "hive",
                extra_roots=None) -> dict[str, dict]:
    """解析一批逻辑仓名 → ``{name: 结果}``。

    结果字段：``path``（ok 时有值）、``source``（env/local-memory/agent/
    coord/workspace-scan/controller-memory）、``verified``（是否由 remote
    强匹配确认）、``status``（ok/ambiguous/missing）、``candidates``、``notes``。
    """
    local_paths = local_paths or {}
    controller = controller or {}
    roots = (list(roots) if roots is not None
             else default_roots(hive_dir, extra_roots))
    cache: dict[str, list[dict]] = {}

    def index_get() -> list[dict]:
        # 惰性扫描：所有仓都能由 env/本机记忆/agent/协调仓认出来时，
        # 一次 git 扫描都不做（land / claim 这类高频命令受影响最大）。
        if "v" not in cache:
            cache["v"] = scan_repos(roots)
        return cache["v"]

    out: dict[str, dict] = {}
    for name in sorted(set(names)):
        out[name] = _one(name, hive_dir=hive_dir, local_paths=local_paths,
                         controller=controller, bindings=bindings,
                         index_get=index_get, self_name=self_name)
    return out


def has_candidates(res: dict) -> bool:
    return bool(res.get("candidates"))


def hint_for(name: str, res: dict | None = None) -> str:
    """给用户/agent 的下一步命令提示。"""
    if res and res.get("source") == "workspace-scan" and res.get("path"):
        return f"已按扫描记忆：hive repo bind {name} {res['path']}"
    return f"请指定本机路径：hive repo bind {name} <path>（或 hive repo scan 看候选）"


def ask_path(name: str, res: dict, *, stream=None) -> str | None:
    """交互式问用户选哪个候选；非 TTY / HIVE_NO_PROMPT 时返回 None。

    只返回选择，不写记忆（调用方写，便于 dry-run 和测试）。
    """
    if (os.environ.get(ENV_NO_PROMPT) or "").strip() in ("1", "true", "yes"):
        return None
    stream = stream or sys.stdin
    try:
        if not stream.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    cands = res.get("candidates") or []
    print(f"[hive] 无法确定本机 {name} 仓的路径。", file=sys.stderr)
    for i, c in enumerate(cands, 1):
        how = "remote 匹配" if c.get("kind") == "remote" else "目录名匹配"
        print(f"  {i}) {c['path']}  [{how}]", file=sys.stderr)
    print("  也可以直接粘贴一个路径；回车跳过（跳过则不同步这个仓）。",
          file=sys.stderr)
    try:
        answer = input(f"{name} 用哪个？> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return None
    if not answer:
        return None
    if answer.isdigit():
        idx = int(answer)
        if 1 <= idx <= len(cands):
            return cands[idx - 1]["path"]
        print(f"[hive] 编号超出范围：{answer}", file=sys.stderr)
        return None
    return answer


# --------------------------------------------------------------------------- #
# T-0353：协调仓自动同步 —— 三态判定 + T2/T0 节流器
#
# 设计约束（见任务卡「不要做什么」）：
#   * Git 仍是事实账：这里只读 git 判定状态，绝不用内存 LSDB 覆盖卡面；
#   * 脏工作区零写操作：不 merge / reset / checkout / stash / clean，
#     也不写工作区里的任何文件（节流状态落在 git 内部目录）；
#   * T2 周期 1800s ±20% jitter 兜底，T0 事件 / T1 组播 head_sha 变化可提前，
#     但受最小间隔约束；status 每 20s 一轮不该无条件连 Gitea；
#   * ls-remote 的 10s TTL 缓存在 hive.py:fetch_if_changed() 里原样保留，
#     本模块只决定「要不要走到那一步」。
#
# status 与 sync-repos 共用同一份判定与同一个节流器：入口不同、口径一致。
# --------------------------------------------------------------------------- #

SYNC_T2_INTERVAL_S = float(getattr(_lsa, "LS_REFRESH_S", 1800.0))
SYNC_JITTER = float(getattr(_lsa, "JITTER", 0.20))
# 事件触发（T0/T1）提前同步的最小间隔：防抖下限，**不是**同步周期。
# 周期恒为 SYNC_T2_INTERVAL_S（1800s ±20%），不压到分钟级。
SYNC_MIN_INTERVAL_S = 60.0
SYNC_STATE_BASENAME = "hive_sync_state.json"           # 协调仓自动同步窗口
# 代码仓 sync-repos 与协调仓 status 共用同一个 SyncThrottle / 同一组 T2/T0
# 常量，但各记一份窗口：否则一次 status 就会把代码仓同步压掉 30 分钟。
SYNC_REPOS_STATE_BASENAME = "hive_repo_sync_state.json"

SYNC_ENV_TIER = "HIVE_SYNC_TIER"            # T0 / T1 / T2，默认 T2
SYNC_ENV_HEAD_SHA = "HIVE_SYNC_HEAD_SHA"    # T1 组播通告的 origin/main 头
SYNC_ENV_EVENT = "HIVE_SYNC_EVENT"          # 收到 T0 触发 LSA
SYNC_ENV_FORCE = "HIVE_SYNC_FORCE"          # 人工/显式强制本轮对账
SYNC_ENV_STATE = "HIVE_SYNC_STATE_FILE"     # 覆盖节流状态文件路径（测试隔离）
SYNC_ENV_DISABLE = "HIVE_SYNC_NO_AUTOSYNC"  # 紧急回退阀：关掉自动同步
SYNC_ENV_DIRTY_FF = "HIVE_SYNC_DIRTY_FF"    # 脏但无重叠时仍允许 ff（默认开）


def dirty_ff_enabled() -> bool:
    """**显式** opt-in：脏工作区里「改动与传入文件不重叠」时是否允许 ff 追平。

    默认关闭（``HIVE_SYNC_DIRTY_FF=1`` 才开）。原因：`hive status` 的
    「脏工作区零写 / 字节级不动」是 L5 验收口径，不能因为想自动更新就默认
    改掉它。需要自愈的入口（`hive autoupdate`）**自己**传 allow_dirty_ff=True，
    于是策略跟着入口走：status 保守、autoupdate 自愈，两边口径都说得清。
    """
    return (os.environ.get(SYNC_ENV_DIRTY_FF) or "0").strip().lower() in (
        "1", "true", "yes", "on")

# 三态 + 两个无害态
CLEAN_FF = "clean-ff"                # 干净且落后、可 ff-only → autosync
DIRTY_FF = "dirty-ff-safe"           # 脏，但改动与传入文件不重叠 → 仍可安全 ff
CLEAN_DIVERGED = "clean-diverged"    # 干净但分叉 → 人工裁决，不 merge
DIRTY_WORKTREE = "dirty"             # 脏 → 零写操作
UP_TO_DATE = "up-to-date"            # 已最新或本地领先
NO_REMOTE = "no-remote"              # 没有 origin/main

BANNER_DIRTY = (
    "本地有未提交改动，自动同步已跳过；请先由改动主人提交或保存这些改动，"
    "再在协调仓执行 git pull（或 hive sync-repos）后刷新看板")
BANNER_DIVERGED = (
    "本地与 origin/main 分叉，需人工裁决；已跳过自动合并，"
    "卡面状态可能不是最新")
BANNER_OVERLAP = (
    "本地未提交改动与 origin/main 传入的提交重叠，自动同步已跳过；"
    "先提交或 stash 这些文件，再执行 git pull（或 hive sync-repos）")
BANNER_THROTTLED = (
    "本地协调仓落后 origin/main；将在下一次同步窗口自动更新，"
    "也可手动执行 git pull（或 hive sync-repos）后立即刷新")

try:  # Windows 无窗出口，缺模块时退回空 kwargs（与 hive.py 一致）
    from hive_compat import no_window_kwargs as _no_window
except ImportError:  # pragma: no cover - 残缺部署
    def _no_window() -> dict:
        return {}


def _git_live(args: list[str], cwd, timeout: float = 10.0) -> tuple[int, str, str]:
    """**不走缓存**的只读 git 调用。

    `_git()` 的 `_GIT_CACHE` 会把 `git status --porcelain` 的结果钉死，
    autosync 之后必须重新判定，所以三态判定一律走这个实时版本。
    """
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(cwd), text=True, capture_output=True,
            timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            **_no_window())
    except (OSError, subprocess.SubprocessError):
        return -1, "", ""
    return p.returncode, (p.stdout or ""), (p.stderr or "")


# --------------------------------------------------------------------------- #
# 陈旧 git 锁：识别 + 归档（自动更新自愈）
#
# 为什么必须做：index.lock 是 git 的互斥闸。一个进程被 SIGKILL / 断电 /
# 「重启看板」打断在写索引的半路，就会留下一个 0 字节锁文件。此后所有**写
# 索引**的操作（merge / checkout / add / reset）全部报
#   「无法创建 .git/index.lock：文件已存在」/「另一 git 进程似乎正在此仓库中运行」
# 而 fetch / ls-remote 照旧成功 —— 于是自动同步看起来「在跑」，实际每次
# merge 都失败；掉队的节点再也不会自己追上来，人只看到一句吓人的锁提示。
#
# 安全边界（宁可不动，也不误删正在用的锁）：
#   * 只看已知的写锁：index.lock / HEAD.lock / shallow.lock；
#   * 「无人持有」且「年龄 ≥ HIVE_LOCK_STALE_S（默认 120s）」两条同时成立；
#   * 有持有者且进程还活着 → 一律不动；查不清持有者时把门槛抬到 600s；
#   * 从不 unlink：改名成 <name>.stale-<epoch> 留证据，可回溯、可统计；
#   * HIVE_NO_LOCK_REPAIR=1 一键关掉（应急阀）。
# --------------------------------------------------------------------------- #

LOCK_NAMES = ("index.lock", "HEAD.lock", "shallow.lock")
LOCK_STALE_AFTER_S = 120.0
LOCK_STALE_UNKNOWN_HOLDER_S = 600.0     # 查不清持有者时的保守门槛
LOCK_ENV_STALE_S = "HIVE_LOCK_STALE_S"
LOCK_ENV_NO_REPAIR = "HIVE_NO_LOCK_REPAIR"
# git 的锁冲突文案：中英文都要认（FreeBSD 上是中文 gettext，CI 上是英文）
LOCK_ERROR_TOKENS = (
    "index.lock", "head.lock", "shallow.lock",
    "another git process", "另一 git 进程", "无法创建", "unable to create",
)


def git_dir_of(repo) -> Path:
    """仓库的 git 目录（工作树里也对；.git 文件 / worktree 都由 git 自己答）。"""
    rc, out, _ = _git_live(["rev-parse", "--absolute-git-dir"], repo)
    if rc == 0 and out.strip():
        return Path(out.strip())
    return Path(repo) / ".git"


def lock_holders(lock_path) -> tuple[list[int], bool]:
    """谁正持有这个文件：返回 ``(pids, 是否查得清)``。

    普通用户只能看到自己（及同组）进程打开的 fd，所以「查得清」只是尽力而为：
    查不清时调用方必须用更保守的年龄门槛，绝不因为「我没看见」就当无人持有。
    """
    path = str(lock_path)
    if not os.path.exists(path):
        return [], True
    # FreeBSD：`procstat -a -f` 按进程列 fd 全路径。注意不能用 `fstat -f`：
    # 它的 -f 是「限定在同一个文件系统」，不是「这个文件」，会把整盘进程都列出来。
    if os.path.exists("/usr/bin/procstat"):
        try:
            p = subprocess.run(["procstat", "-a", "-f"], capture_output=True,
                               text=True, timeout=15.0)
        except (OSError, subprocess.SubprocessError):
            return [], False
        if p.returncode != 0:
            return [], False
        pids = []
        for line in (p.stdout or "").splitlines():
            line = line.rstrip()
            if not line or not line.endswith(path):
                continue
            parts = line.split(None, 1)
            if parts and parts[0].isdigit():
                pids.append(int(parts[0]))
        return sorted(set(pids)), True
    # Linux：扫 /proc/<pid>/fd
    if os.path.isdir("/proc"):
        pids = []
        try:
            names = os.listdir("/proc")
        except OSError:
            return [], False
        for name in names:
            if not name.isdigit():
                continue
            fd_dir = "/proc/%s/fd" % name
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        if os.readlink(os.path.join(fd_dir, fd)) == path:
                            pids.append(int(name))
                            break
                    except OSError:
                        continue
            except OSError:
                continue
        return sorted(set(pids)), True
    return [], False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # 别人的进程：活着，只是看不见
        return True
    except OSError:
        return False
    return True


def inspect_locks(repo, *, now: float | None = None,
                  stale_after: float | None = None) -> dict:
    """只读巡锁：每个锁的年龄、持有者、判定（stale / busy / young / unknown）。

    不做任何写操作；给 ``hive doctor`` / 看板 / 自愈入口共用一个口径。
    """
    now = float(time.time() if now is None else now)
    try:
        stale_after = float(os.environ.get(LOCK_ENV_STALE_S)
                            or LOCK_STALE_AFTER_S)
    except ValueError:
        stale_after = LOCK_STALE_AFTER_S
    if stale_after <= 0:
        stale_after = LOCK_STALE_AFTER_S
    if stale_after is None:
        stale_after = LOCK_STALE_AFTER_S

    gdir = git_dir_of(repo)
    locks = []
    for name in LOCK_NAMES:
        p = gdir / name
        try:
            st = p.stat()
        except OSError:
            continue
        age = max(0.0, now - st.st_mtime)
        pids, known = lock_holders(p)
        alive = [pid for pid in pids if _pid_alive(pid)]
        if alive:
            verdict = "busy"
            reason = "有存活进程正持有：%s" % ",".join(str(x) for x in alive)
        elif not known:
            verdict = "stale" if age >= max(stale_after,
                                            LOCK_STALE_UNKNOWN_HOLDER_S) else "unknown"
            reason = "查不清持有者，按更保守的门槛判定"
        else:
            verdict = "stale" if age >= stale_after else "young"
            reason = ("无持有者且已 %ds 未动" % int(age) if verdict == "stale"
                      else "刚创建 %ds，可能有人正在写" % int(age))
        locks.append({
            "name": name, "path": str(p), "age_s": age,
            "size": st.st_size, "holders": alive, "holders_known": known,
            "verdict": verdict, "reason": reason,
        })
    return {"git_dir": str(gdir), "stale_after_s": stale_after,
            "locks": locks,
            "stale": [lk["name"] for lk in locks if lk["verdict"] == "stale"]}


def repair_stale_locks(repo, *, now: float | None = None,
                       dry_run: bool = False) -> dict:
    """把判定为 stale 的锁**改名归档**（不是删除），返回结构化结果。

    调用方拿 ``repaired`` 判断是否需要重试一次 git 写操作。任何异常都不抛，
    只记在 ``error`` 里 —— 自愈失败绝不能把 status / 定期任务拖崩。
    """
    if (os.environ.get(LOCK_ENV_NO_REPAIR) or "").strip() in ("1", "true", "yes"):
        return {"git_dir": str(git_dir_of(repo)), "found": [], "repaired": [],
                "skipped": "HIVE_NO_LOCK_REPAIR=1，锁自愈已关闭", "error": ""}
    try:
        scan = inspect_locks(repo, now=now)
    except Exception as e:                      # noqa: BLE001 - 自愈要兜底
        return {"git_dir": "", "found": [], "repaired": [], "skipped": "",
                "error": "%s: %s" % (type(e).__name__, e)}
    found = [lk for lk in scan["locks"] if lk["verdict"] == "stale"]
    repaired, skipped = [], []
    for lk in found:
        if dry_run:
            repaired.append({**lk, "archived_as": ""})
            continue
        src = Path(lk["path"])
        dst = src.with_name("%s.stale-%d" % (src.name, int(now or time.time())))
        try:
            os.replace(src, dst)
        except OSError as e:
            skipped.append({**lk, "error": str(e)})
            continue
        repaired.append({**lk, "archived_as": str(dst)})
    return {"git_dir": scan["git_dir"], "found": [lk["path"] for lk in found],
            "repaired": repaired, "skipped": skipped, "error": ""}


def is_lock_error(text: str) -> bool:
    """这段 git 输出是不是「被锁挡住」（而不是真的合并冲突）。"""
    low = (text or "").lower()
    return any(tok.lower() in low for tok in LOCK_ERROR_TOKENS)


def git_write(repo, args: list[str], *, timeout: float = 60.0,
              retries: int = 1) -> tuple[int, str, str]:
    """执行**写**类 git 命令；撞锁时先自愈再重试一次。

    只对「锁」这一类失败重试：真正的 ff 失败 / 冲突原样返回给调用方，
    绝不用重试把真实错误吞掉。返回 ``(rc, stdout, stderr)``。
    """
    out_rc, out, err = _git_live(args, repo, timeout=timeout)
    if out_rc == 0 or not is_lock_error(err):
        return out_rc, out, err
    for _ in range(max(0, retries)):
        rep = repair_stale_locks(repo)
        if not rep["repaired"]:
            break
        out_rc, out, err = _git_live(args, repo, timeout=timeout)
        if out_rc == 0 or not is_lock_error(err):
            break
    return out_rc, out, err


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def dirty_paths(repo) -> list[str]:
    """工作区里**会被 merge 撞到**的路径：已跟踪改动 + 未跟踪新文件。

    未跟踪文件一般不会被 ff 改动，但远端**新增**同名文件时 git 会拒绝合并
    （untracked working tree files would be overwritten），所以也要参与重叠判定。
    """
    rc, out, _ = _git_live(["diff", "--name-only", "HEAD"], repo)
    tracked = _lines(out) if rc == 0 else []
    rc2, out2, _ = _git_live(
        ["ls-files", "-o", "--exclude-standard"], repo)
    untracked = _lines(out2) if rc2 == 0 else []
    return sorted(set(tracked) | set(untracked))


def incoming_paths(repo, remote_ref: str = "origin/main") -> list[str]:
    """``HEAD..remote`` 之间会被快进带进来的文件（相对仓库根）。"""
    rc, out, _ = _git_live(["diff", "--name-only", "HEAD", remote_ref], repo)
    return sorted(set(_lines(out))) if rc == 0 else []


def assess_sync_state(repo, *, remote_ref: str = "origin/main",
                      allow_dirty_ff: bool = False) -> dict:
    """只读三态判定：**绝不执行任何写操作**。

    返回 dict（字段同时也是 ``sync_health`` 的诊断来源）::

        clean / porcelain / head_sha / remote_sha / behind / ff_possible /
        diverged / state / action / reason / banner

    ``behind`` 沿用旧 ``local_stale`` 的口径：HEAD 与 origin/main 不同、
    且 origin/main 不是 HEAD 的祖先（落后或分叉）。autosync 成功后
    HEAD 追平远端，``behind`` 自然转 False。
    """
    path = Path(repo)
    rc, porcelain, _ = _git_live(["status", "--porcelain"], path)
    dirty_text = porcelain.strip() if rc == 0 else ""
    clean = (rc == 0 and not dirty_text)

    _, head_out, _ = _git_live(["rev-parse", "--verify", "-q", "HEAD"], path)
    head_sha = head_out.strip()
    _, remote_out, _ = _git_live(
        ["rev-parse", "--verify", "-q", remote_ref], path)
    remote_sha = remote_out.strip()

    # HEAD 是 origin/main 的祖先 → 可以 ff-only 追平
    rc_ff, _, _ = _git_live(
        ["merge-base", "--is-ancestor", "HEAD", remote_ref], path)
    ff_possible = (rc_ff == 0 and bool(head_sha))
    # origin/main 是 HEAD 的祖先 → 本地已追平/领先，不算落后
    rc_ahead, _, _ = _git_live(
        ["merge-base", "--is-ancestor", remote_ref, "HEAD"], path)
    behind = bool(head_sha and remote_sha and head_sha != remote_sha
                  and rc_ahead != 0)
    diverged = bool(behind and not ff_possible)

    dirty_files: list[str] = []
    overlap: list[str] = []
    if not clean:
        dirty_files = dirty_paths(path)
        if allow_dirty_ff and not behind:
            # 脏，但远端没有新提交：没什么可同步的，别报「允许自动同步」
            state, action = UP_TO_DATE, "none"
            reason = "工作区有未提交改动，但已与 origin/main 一致，无需同步"
            banner = ""
        elif allow_dirty_ff and ff_possible and remote_sha:
            overlap = sorted(set(dirty_files) & set(incoming_paths(path, remote_ref)))
            if not overlap:
                state, action = DIRTY_FF, "autosync"
                reason = ("工作区有 %d 个未提交/未跟踪文件，但都不在本次快进带入的"
                          "文件里，ff 不会碰到它们，允许自动同步" % len(dirty_files))
                banner = ""
            else:
                state, action = DIRTY_WORKTREE, "skip-dirty"
                reason = ("本地未提交改动与 origin/main 传入的提交重叠（%d 个文件），"
                          "为避免覆盖手头改动，自动同步已跳过" % len(overlap))
                banner = BANNER_OVERLAP
        else:
            state, action = DIRTY_WORKTREE, "skip-dirty"
            reason = "本地有未提交改动，自动同步已跳过"
            banner = BANNER_DIRTY
    elif not remote_sha:
        state, action = NO_REMOTE, "none"
        reason = "找不到 origin/main，无法对账"
        banner = ""
    elif behind and ff_possible:
        state, action = CLEAN_FF, "autosync"
        reason = "干净工作区且可快进，允许自动同步"
        banner = ""
    elif diverged:
        state, action = CLEAN_DIVERGED, "manual"
        reason = "本地与 origin/main 分叉，需人工裁决"
        banner = BANNER_DIVERGED
    else:
        state, action = UP_TO_DATE, "none"
        reason = "已与 origin/main 一致（或本地领先）"
        banner = ""

    return {
        "clean": clean,
        "porcelain": dirty_text,
        "head_sha": head_sha,
        "remote_sha": remote_sha,
        "behind": behind,
        "ff_possible": ff_possible,
        "diverged": diverged,
        "state": state,
        "action": action,
        "reason": reason,
        "banner": banner,
        # —— 脏工作区的可操作细节（看板/agent 据此决定是「等」还是「动手」）——
        "dirty_files": dirty_files,
        "overlap_files": overlap,
        "dirty_ff": state == DIRTY_FF,
    }


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in (
        "1", "true", "yes", "on")


def sync_env_hints() -> dict:
    """控制面（T-0351/T-0352）喂进来的节拍信号，全部可选。"""
    tier = (os.environ.get(SYNC_ENV_TIER) or "").strip().upper()
    if tier not in ("T0", "T1", "T2"):
        tier = "T2"
    return {
        "tier": tier,
        "peer_head_sha": (os.environ.get(SYNC_ENV_HEAD_SHA) or "").strip(),
        "event": _env_flag(SYNC_ENV_EVENT),
        "force": _env_flag(SYNC_ENV_FORCE),
        "disabled": _env_flag(SYNC_ENV_DISABLE),
    }


class SyncThrottle:
    """T2 1800s ±20% jitter 兜底 + T0/T1 提前触发（受最小间隔约束）。

    状态持久化在 **git 内部目录**（``git rev-parse --absolute-git-dir``），
    不进工作区、不进 ``git status --porcelain``；因此「脏工作区字节级不变」
    这一回归与节流记账可以同时成立。短命令（status）跨进程也能记住窗口。
    """

    def __init__(self, repo, *, state_path=None, clock=None, rng=None,
                 t2_interval_s: float | None = None,
                 min_interval_s: float | None = None,
                 state_basename: str | None = None):
        self.repo = Path(repo)
        self.clock = clock or time.time
        self.rng = rng or random.random
        self.state_basename = state_basename or SYNC_STATE_BASENAME
        self.t2_interval_s = float(
            t2_interval_s if t2_interval_s is not None else SYNC_T2_INTERVAL_S)
        self.min_interval_s = float(
            min_interval_s if min_interval_s is not None else SYNC_MIN_INTERVAL_S)
        override = (os.environ.get(SYNC_ENV_STATE) or "").strip()
        self.state_path = Path(state_path or override or self._default_state_path())

    def _default_state_path(self) -> Path:
        rc, out, _ = _git_live(
            ["rev-parse", "--absolute-git-dir"], self.repo)
        if rc == 0 and out.strip():
            return Path(out.strip()) / self.state_basename
        return self.repo / ".git" / self.state_basename

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        """原子落盘。写失败绝不能拖垮 status（只是记账降级）。"""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(
                self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def next_t2_at(self, now: float) -> float:
        """下一次 T2 兜底时刻，必带 ±20% jitter，且不等于整周期。"""
        if _lsa is not None:
            return float(_lsa.next_deadline(
                now, self.t2_interval_s, rng=self.rng, jitter=SYNC_JITTER))
        u = float(self.rng())
        if not 0.0 <= u < 1.0:
            u = 0.5
        factor = 1.0 + (2.0 * u - 1.0) * SYNC_JITTER
        if factor == 1.0:
            factor = 1.0 + SYNC_JITTER * 0.5
        return now + self.t2_interval_s * factor

    def check(self, *, now: float | None = None,
              tier: str = "T2", peer_head_sha: str = "",
              local_remote_sha: str = "", event: bool = False,
              force: bool = False) -> dict:
        """本轮要不要对账 / 要不要问 Gitea（纯判定，**不写任何文件**）。

        ``due``        —— 本轮允许做一次对账；
        ``ask_remote`` —— 允许走 ``fetch_if_changed()`` 的 ls-remote 前置比对；
        ``tier``       —— 触发档位 T0 / T1 / T2（写进 sync_health）。
        """
        now = float(self.clock() if now is None else now)
        if tier not in ("T0", "T1", "T2"):
            tier = "T2"
        st = self._load()
        last = float(st.get("last_sync_at") or 0.0)
        next_t2 = float(st.get("next_t2_at") or 0.0)
        cold = not st or not last

        # T1 组播带来的 head_sha：与本地已知 origin/main 相同 → 连
        # ls-remote 都不必发；只有 sha 不同才允许前置比对。
        head_changed = bool(peer_head_sha and local_remote_sha
                            and peer_head_sha != local_remote_sha)
        head_same = bool(peer_head_sha and local_remote_sha
                         and peer_head_sha == local_remote_sha)

        early = event or tier in ("T0", "T1") or head_changed
        due = False
        ask = False
        out_tier = tier
        if force:
            due, ask = True, True
            out_tier = "T0" if tier in ("T0", "T1") else tier
            reason = "显式/人工触发对账"
        elif cold:
            # 冷启动基线：第一次必须建立窗口，之后 30 分钟内不再重复。
            due, ask = True, True
            out_tier = "T2"
            reason = "冷启动基线对账（T2）"
            next_t2 = self.next_t2_at(now)
        elif early and (now - last) >= self.min_interval_s:
            due, ask = True, True
            out_tier = "T0" if (event or tier == "T0") else "T1"
            reason = ("T0 事件触发提前同步" if (event or tier == "T0")
                      else "T1 组播 head_sha 变化，提前同步")
        elif early:
            due, ask = False, False
            reason = "事件被最小间隔抑制（防抖）"
        elif next_t2 and now >= next_t2:
            due, ask = True, True
            out_tier = "T2"
            reason = "T2 周期兜底对账到期"
            next_t2 = self.next_t2_at(now)
        else:
            due, ask = False, False
            out_tier = "T2"
            reason = "T2 窗口内，跳过远端对账（不连 Gitea）"

        # 组播已经确认 sha 一致：本轮只做本地判定，零 Gitea 流量。
        if head_same and not force:
            ask = False
            reason = "T1 组播 head_sha 与本地一致，无需问 Gitea"

        return {
            "due": due,
            "ask_remote": ask,
            "tier": out_tier,
            "reason": reason,
            "now": now,
            "last_sync_at": last,
            "next_t2_at": next_t2 or self.next_t2_at(now),
            "lsdb_seq": int(st.get("lsdb_seq") or 0),
            "min_interval_s": self.min_interval_s,
            "t2_interval_s": self.t2_interval_s,
            "jitter": SYNC_JITTER,
        }

    def commit(self, decision: dict, *, head_sha: str = "",
               remote_sha: str = "", reason: str = "") -> dict:
        """一次对账真的做完了才调用：推进 seq、重排带 jitter 的 T2 窗口。"""
        now = float(decision.get("now") or self.clock())
        st = self._load()
        st.update({
            "last_sync_at": now,
            "next_t2_at": self.next_t2_at(now),
            "lsdb_seq": int(st.get("lsdb_seq") or 0) + 1,
            "last_tier": decision.get("tier") or "T2",
            "last_reason": reason or decision.get("reason") or "",
            "head_sha": head_sha,
            "remote_sha": remote_sha,
        })
        self._save(st)
        return st


def build_sync_health(assess: dict, decision: dict, state: dict | None = None,
                      *, autosynced: bool = False,
                      throttled: bool = False, sync_error: str = "",
                      lock_repaired: list | None = None) -> dict:
    """``status --json`` 的 ``sync_health`` 结构化对象。

    七个契约字段：lsdb_seq / last_sync_at / head_sha / remote_sha /
    behind / tier / reason；其余为诊断扩展，不覆盖 Git 事实账。
    """
    st = state or {}
    last = float(st.get("last_sync_at") or decision.get("last_sync_at") or 0.0)
    last_iso = ""
    if last:
        try:
            from datetime import datetime as _dt
            last_iso = _dt.fromtimestamp(last).isoformat(timespec="seconds")
        except (OSError, ValueError, OverflowError):
            last_iso = ""
    tier = str(decision.get("tier") or "T2")
    if tier not in ("T0", "T1", "T2"):
        tier = "T2"
    reason = str(decision.get("reason") or assess.get("reason") or "")
    if assess["state"] in (DIRTY_WORKTREE, DIRTY_FF):
        reason = assess["reason"]          # 脏：明确写出「跳过」还是「不重叠可同步」
    elif assess["state"] == CLEAN_DIVERGED:
        reason = assess["reason"]          # 分叉：明确写「需人工裁决」
    return {
        "lsdb_seq": int(st.get("lsdb_seq") or decision.get("lsdb_seq") or 0),
        "last_sync_at": last_iso,
        "last_sync_ts": last,
        "head_sha": assess["head_sha"],
        "remote_sha": assess["remote_sha"],
        "behind": bool(assess["behind"]),
        "tier": tier,
        "reason": reason,
        # —— 诊断扩展（T-0354 看板可读）——
        "state": assess["state"],
        "action": assess["action"],
        "clean": bool(assess["clean"]),
        "autosynced": bool(autosynced),
        "throttled": bool(throttled),
        "next_t2_at": float(decision.get("next_t2_at") or 0.0),
        "t2_interval_s": float(decision.get("t2_interval_s")
                               or SYNC_T2_INTERVAL_S),
        "jitter": float(decision.get("jitter") or SYNC_JITTER),
        "min_interval_s": float(decision.get("min_interval_s")
                                or SYNC_MIN_INTERVAL_S),
        "banner": assess["banner"],
        # —— 脏工作区细节：看板/agent 据此区分「等一下」与「必须人工介入」——
        "dirty_ff": bool(assess.get("dirty_ff")),
        "dirty_files": list(assess.get("dirty_files") or []),
        "overlap_files": list(assess.get("overlap_files") or []),
        # 合并失败的真实原因（以前被 `check=False` 静默吞掉，人只看到「一直落后」）
        "sync_error": str(sync_error or ""),
        "lock_repaired": list(lock_repaired or []),
        # 需要人工/agent 介入：脏改动与传入提交重叠，光等不会自己好
        "blocked": bool(assess["state"] == DIRTY_WORKTREE
                        and assess.get("overlap_files")),
    }
