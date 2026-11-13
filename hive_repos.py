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
import re
import subprocess
import sys
from pathlib import Path

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