#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive —— 多机 AI Agent 协同开发的任务总线（git-as-queue）。

设计要点
--------
* 协调仓库（hive repo）只存三样东西：任务卡 tasks/、收件箱 inbox/、结果 results/。
* 领取 = 向协调仓库 push 一个独占分支 claim/<task>；push 被拒 = 被别人抢走。
  依赖 git 服务端的引用更新原子性，不需要额外的锁服务。
* 结果 = push 分支 res/<task>，内容是 results/<task>.json；不碰 main。
* 所有 worker 都是「拉取式」：只要能 git fetch/push，就能工作，不需要被 SSH 进来。
* 代码仓库（light / lightmem / lightharness …）各自独立，任务卡里写 repo 名字，
  worker 用自己的本地克隆 + 每任务一个 git worktree 干活。

子命令
------
  init        初始化协调仓库
  agent add   登记一台机器/一个 agent（能力、仓库路径、启动命令）
  task new    新建任务卡
  assign      把任务放进某个 agent 的收件箱
  work        worker 主循环（领取 → 干活 → 提交 → 写结果）
  status      看板
  close       标记任务完成
  claim       手工领取（调试用）
  gc          回收过期租约

只用 Python 标准库 + git，Windows / FreeBSD / Linux 同一份代码。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

VERSION = "0.9.13"
IDENT = ["-c", "user.name=hive-worker", "-c", "user.email=hive@localhost"]
DEFAULT_STATE = Path.home() / ".hive"
DEFAULT_MAX_PER_REPO = 1
# 一张卡最多被看门狗**自动**回收几次；超了就转 blocked 退出流水线，不再无限重派。
# 0 = 一次都不许自动重派（首次判死即 blocked）。人工显式 `hive retry` 不受此限。
DEFAULT_MAX_RETRIES = 3

# 网络类 git 操作（fetch / push / clone / ls-remote）的默认超时（秒）。
# subprocess.run 不传 timeout 就是无限等：2026-10-25 Gitea 约 12 小时不可达，
# `hive land --apply` 就静默卡死在 fetch 上，只能靠杀进程才发现。
# 可用环境变量 HIVE_NET_TIMEOUT 覆盖；<= 0 表示不限制（保留手动逃生口）。
NET_TIMEOUT_S = 120.0
NET_TIMEOUT_ENV = "HIVE_NET_TIMEOUT"

# claim 抢锁失败后的带抖动指数退避（T-0146）。
#
# 非快进拒绝仍是 claim 原子性的唯一来源（绝不加 --force）；这里只决定「失败之后」
# 是立刻放弃还是退一小步再试。参数集中在这里，环境变量可覆盖：
#   HIVE_CLAIM_BACKOFF_BASE    第一次退避的基数（秒，默认 0.5）
#   HIVE_CLAIM_BACKOFF_MAX     单次退避上限（秒，默认 8）
#   HIVE_CLAIM_BACKOFF_JITTER  抖动比例 0..1（默认 0.5，加在指数基数上）
#   HIVE_CLAIM_MAX_RETRIES     最多重试几次（默认 3；总 push 次数 = 1 + 这个数）
#
# 上限是「次数」而不是「时长」：每次 fetch/push 自身还有 HIVE_NET_TIMEOUT 兜底，
# 所以最坏总耗时也是有界的，不会出现无限退避。
CLAIM_BACKOFF_BASE_S = 0.5
CLAIM_BACKOFF_MAX_S = 8.0
CLAIM_BACKOFF_JITTER = 0.5
CLAIM_MAX_RETRIES = 3
CLAIM_BACKOFF_BASE_ENV = "HIVE_CLAIM_BACKOFF_BASE"
CLAIM_BACKOFF_MAX_ENV = "HIVE_CLAIM_BACKOFF_MAX"
CLAIM_BACKOFF_JITTER_ENV = "HIVE_CLAIM_BACKOFF_JITTER"
CLAIM_MAX_RETRIES_ENV = "HIVE_CLAIM_MAX_RETRIES"

TASK_FIELDS = [
    "id", "title", "repo", "base", "state", "owner", "agent", "fresh",
    "priority", "needs", "paths", "locks", "verify", "depends_on",
    "lease_minutes", "timeout_minutes", "created", "retries", "max_retries",
]

# 文件级锁（任务卡 `locks` 字段）的判定在 conflict.py。放 try 里是因为 hive.py 允许被
# 单独拷进协调仓（`init` 只拷 hive.py）：缺模块时不该连 `status` 都起不来，
# 只有真用到 `locks` 才报错（见 Hive._paths_conflict）。
try:
    import conflict
except ImportError:  # pragma: no cover - 只在缺 conflict.py 的残缺部署上走到
    conflict = None

PROMPT_HEADER = """\
你是「蜂巢（Hive）」的一名任务执行者。你只负责下面这一张任务卡，不要越界。

## 硬性规则

1. 工作目录就是为你单独创建的 git worktree，分支 `task/{tid}`；不要 cd 到别的目录。
2. 只修改任务卡 `paths` 允许的路径；需要改其它文件时，停下来在 `.hive/summary.md` 里说明原因。
3. 不要 `git push`，不要切分支，不要动 `main`。提交由 worker 统一完成（你也可以自行小步 commit）。
4. 完成后必须写 `.hive/summary.md`（中文，5–15 行）：做了什么、改了哪些文件、跑了什么验证、遗留什么风险。
5. 如果做不完，也要写 `.hive/summary.md`，把卡住的原因和下一步写清楚，不要沉默退出。

## 任务卡
"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def log(msg: str) -> None:
    print(f"[hive {now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def current_host() -> str:
    """当前机器名（小写）。多机协作时用它区分 agent 归属哪台机器。"""
    for name in (os.uname().nodename if hasattr(os, "uname") else "",
                 os.environ.get("COMPUTERNAME", ""),
                 os.environ.get("HOSTNAME", "")):
        if name:
            return str(name).strip().lower()
    return ""


def agent_host(cfg: dict) -> str:
    """agent 配置里声明的机器名（小写）；没写返回空串。"""
    return str((cfg or {}).get("host") or "").strip().lower()


class HiveError(Exception):
    pass


def run(cmd, cwd=None, check=True, input=None, timeout=None, shell=False, env=None):
    # 钉死 utf-8，别让编码跟着 locale 跑（T-0143）。
    #
    # text=True 的隐含语义是「stdin/stdout 都用 locale 编码」。Windows 上 locale
    # 是 cp936，于是 commit_file_to_ref 用 `git hash-object -w --stdin` 送进去的
    # 结果 JSON 会按 GBK 落盘（实测 results/T-0142.json 全是 GBK 字节）；Linux 侧
    # 再 `git show` 取回、按 UTF-8 解码就直接 UnicodeDecodeError，status / doctor
    # 整条命令崩掉。
    #
    # 两侧一起收口：encoding 保证写进去的永远是 UTF-8（治本，跨平台同一份编码），
    # errors 保证读进来的非法字节只替换不抛异常（兜底，最坏显示 U+FFFD，而不是
    # 整个看板消失）。所有外部命令都走这一个出口，所以只改这里。
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, text=True, input=input,
            capture_output=True, timeout=timeout, shell=shell, env=env,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        # timeout=None 永远不会走到这里；一旦设了超时，就必须把「挂住」变成
        # 一条人能看懂、能行动的 HiveError，而不是无限等或裸 TimeoutExpired。
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        err = HiveError(
            f"命令超时（{timeout} 秒内未返回，已终止）: {text}\n"
            f"如果是网络类 git 操作（fetch/push/clone/ls-remote），说明远端不可达"
            f"或网络中断；先用 `git remote -v` 确认远端地址，再检查连通性"
            f"（ping / curl）后重试。"
        )
        err.timed_out = True
        raise err from None
    if check and p.returncode != 0:
        raise HiveError(
            f"命令失败({p.returncode}): {cmd if isinstance(cmd, str) else ' '.join(cmd)}\n"
            f"{p.stderr.strip()[-2000:]}"
        )
    return p


# 进程内 git 互斥锁：worker 并发跑多张卡时，多个任务线程会同时 fetch / 建 worktree /
# commit **同一个**代码仓，而 git 对这些是竞争写（实测并发 `git worktree add` 会以
# 退出码 255 互踩，导致某张卡「创建 worktree 失败」）。用一把全局锁把它们串起来；
# 只锁 git 通道，不锁 run()：agent 调用（Popen）与 verify 命令仍并行，不影响吞吐。
_GIT_LOCK = threading.RLock()


def git(args, cwd, check=True, input=None):
    with _GIT_LOCK:
        return run(["git", *args], cwd=cwd, check=check, input=input)


def git_ok(args, cwd) -> bool:
    return git(args, cwd, check=False).returncode == 0


def net_timeout() -> float | None:
    """网络类 git 操作的超时秒数；HIVE_NET_TIMEOUT 可覆盖，<= 0 表示不限制。"""
    raw = (os.environ.get(NET_TIMEOUT_ENV) or "").strip()
    if not raw:
        return NET_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        log(f"{NET_TIMEOUT_ENV}={raw!r} 不是数字，按默认 {NET_TIMEOUT_S:g}s 处理")
        return NET_TIMEOUT_S
    return value if value > 0 else None


def _env_number(name: str, default, cast, minimum: float = 0.0):
    """读一个非负数字环境变量；空/非法/越界都退回默认值并留一条 log。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        log(f"{name}={raw!r} 不是数字，按默认 {default} 处理")
        return default
    if value < minimum:
        log(f"{name}={raw!r} 小于 {minimum}，按默认 {default} 处理")
        return default
    return value


def claim_backoff_config() -> dict:
    """本次 claim 的退避参数快照。

    每次 claim 读一次，避免重试途中环境变量被改而出现前后不一致。
    读取模块常量（而不是写死数字），测试可以直接 monkeypatch。
    """
    base = _env_number(CLAIM_BACKOFF_BASE_ENV, CLAIM_BACKOFF_BASE_S, float)
    top = _env_number(CLAIM_BACKOFF_MAX_ENV, CLAIM_BACKOFF_MAX_S, float)
    if top < base:
        top = base
    ratio = _env_number(CLAIM_BACKOFF_JITTER_ENV, CLAIM_BACKOFF_JITTER, float)
    ratio = min(1.0, max(0.0, ratio))
    retries = _env_number(CLAIM_MAX_RETRIES_ENV, CLAIM_MAX_RETRIES, int)
    return {"base": base, "max": top, "jitter": ratio, "retries": int(retries)}


def claim_backoff_delay(attempt: int, cfg: dict | None = None,
                        rng=None) -> float:
    """第 attempt 次重试前该睡多久（attempt 从 1 开始）。

    指数增长：base * 2**(attempt-1)，再叠加一个 [0, jitter*base] 的随机量；
    单次结果不超过 max。抖动取「只加不减」，所以只要没撞到上限，间隔就是
    严格递增的（隐藏测试里不用靠运气）。
    """
    cfg = cfg or claim_backoff_config()
    attempt = max(1, int(attempt))
    # 防止 attempts 极大时 2**n 把 float 撑爆；反正最后都会被 max 截断
    nominal = cfg["base"] * (2 ** min(attempt - 1, 60))
    jitter = (rng or random).uniform(0.0, cfg["jitter"] * nominal)
    return min(cfg["max"], nominal + jitter)


def _claim_sleep(seconds: float) -> None:
    """独立一层是为了测试能替换掉真实等待；KeyboardInterrupt 会原样向上冒。"""
    time.sleep(seconds)


# git push 失败的文本里，哪些属于「值得再试一次」的暂时性错误。
# 注意 non-fast-forward 也在里面：它可能是真的被抢，也可能是本地 origin/claim
# 引用陈旧 —— 到底哪种，要 fetch 之后看 claim 分支有没有主（见 Worker.claim）。
CLAIM_TRANSIENT_TOKENS = (
    "non-fast-forward", "fetch first", "stale info", "rejected",
    "timed out", "timeout", "deadline exceeded",
    "could not resolve", "temporary failure in name resolution",
    "connection refused", "connection reset", "connection timed out",
    "network is unreachable", "no route to host",
    "remote end hung up", "the remote end hung up unexpectedly",
    "unexpected disconnect", "early eof", "rpc failed",
    "cannot lock ref", "failed to update ref",
    "ssl", "tls", "http 500", "http 502", "http 503", "http 504",
    "service unavailable", "temporarily unavailable",
)


def _claim_error_is_retryable(err: Exception) -> bool:
    if getattr(err, "timed_out", False):
        return True
    text = str(err).lower()
    return any(tok in text for tok in CLAIM_TRANSIENT_TOKENS)


def _claim_error_summary(err: Exception | None) -> str:
    if err is None:
        return "未知错误"
    text = str(err).strip()
    first = text.splitlines()[0] if text else err.__class__.__name__
    return first[:200]


def _claim_holder(info) -> str:
    """claim_info 可能返回 dict、旧格式字符串或 None；统一取 agent 名。"""
    if isinstance(info, dict):
        return str(info.get("agent") or "")
    return ""


def _cleanup_claim_artifacts(hive_dir, tid: str) -> None:
    """清掉一次失败 claim 可能留下的本地半成品（best-effort，绝不抛）。"""
    root = Path(hive_dir).expanduser() / ".hive" / "claim"
    for name in (f"{tid}.json", tid):
        p = root / name
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file() or p.is_symlink():
                p.unlink()
        except OSError:
            pass
    try:
        root.rmdir()  # 只在空目录时成功；还有别的卡的状态就不动
    except OSError:
        pass


def _net_remote(args) -> str:
    """从 git 参数里猜远端，只用于超时提示（猜不出就按 origin 说）。"""
    if not args:
        return "origin"
    sub = args[0]
    if sub in ("fetch", "push", "ls-remote"):
        for a in args[1:]:
            if not a.startswith("-"):
                return a
    elif sub == "clone":
        # clone 的 URL 可能排在 --depth 1 / --branch x 这类「选项+值」后面，
        # 优先认像地址的 token，别把 1 当成远端报出去。
        for a in args[1:]:
            if a.startswith("-"):
                continue
            if ("://" in a or a.endswith(".git") or a.startswith(("/", "~"))
                    or ("@" in a and ":" in a)):
                return a
    return "origin"


def git_net(args, cwd, check=True, input=None):
    """网络类 git 操作（fetch/push/clone/ls-remote）统一入口：必带超时。

    本地 git 操作（rev-parse / show / hash-object / commit-tree / merge …）**不要**
    走这里：慢机器上大仓的本地操作也可能很慢，误杀比不设超时更糟。
    """
    timeout = net_timeout()
    with _GIT_LOCK:
        try:
            return run(["git", *args], cwd=cwd, check=check, input=input, timeout=timeout)
        except HiveError as e:
            if not getattr(e, "timed_out", False):
                raise
            remote = _net_remote(args)
            limit = f"{timeout:g}s" if timeout else "不限"
            err = HiveError(
                f"{e}\n远端: {remote} 在 {limit} 内没有响应。建议先查连通性："
                f"`git remote -v` 看地址，再 ping / curl 对应端口；确认可用后重试。"
                f"超时阈值可用环境变量 {NET_TIMEOUT_ENV} 调整（<= 0 关闭）。"
            )
            err.timed_out = True
            raise err from None


def rev_parse(cwd, ref) -> str | None:
    p = git(["rev-parse", "--verify", "-q", ref], cwd, check=False)
    return p.stdout.strip() if p.returncode == 0 else None


# --------------------------------------------------------------------------- #
# 任务卡：极简 Markdown frontmatter（只支持标量与标量列表）
# --------------------------------------------------------------------------- #

def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v in ("null", "~", ""):
        return None
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_card(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise HiveError(f"{path} 缺少 frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise HiveError(f"{path} frontmatter 没有结束标记")
    head, body = text[4:end], text[end + 5:]
    data: dict = {}
    key = None
    for lineno, line in enumerate(head.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") or line.startswith("- "):
            if key is None or not isinstance(data.get(key), list):
                raise HiveError(f"{path}:{lineno} 列表项没有归属: {line!r}")
            # 列表项一律按字符串处理：verify 里的 "true"/"false" 是命令，不是布尔值
            data[key].append(_unquote(line.split("- ", 1)[1]))
        elif ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if not v or v == "[]":
                data[k], key = [], (k if not v else None)
            else:
                data[k], key = _scalar(v), None
        else:
            raise HiveError(f"{path}:{lineno} 无法解析: {line!r}")
    return data, body


# verify 强度：只检查痕迹的是 weak，真的会执行被测对象的才是 strong。
# 判断刻意保守 —— 拿不准就算 weak。混合 shell 管线按「最弱的一环」算，
# 所以 T-0101 那条 `grep && grep && pytest` 会被判成 weak（正是想要的信号）。
STRONG_TOKENS = (
    "pytest", "unittest", "npm test", "npm run test", "yarn test",
    "go test", "cargo test", "make test", "make check", "ctest",
    "tox", "nosetests", "rspec", "phpunit", "dotnet test",
)
WEAK_TOKENS = (
    "grep", "test -f", "test -d", "test -e", "[ -f", "[ -d", "[ -e",
    "which", "echo", "wc -l", "wc -c", "cat", "ls ", "head -", "tail -",
    "git log", "git diff", "git show", "find ", "stat ", "file ",
)


def _verify_strength(cmd: str) -> str:
    """一条 verify 命令是 strong（真跑测试）还是 weak（只查痕迹）。"""
    if not cmd or not str(cmd).strip():
        return "weak"
    text = str(cmd).lower()
    # 完整管线拆开，按最弱的一环算：`grep x && pytest` 里只要有一段是弱检查就算 weak
    parts = [p.strip() for p in text.replace(";", "&&").split("&&") if p.strip()]
    for part in parts:
        if any(w in part for w in WEAK_TOKENS):
            return "weak"
    if any(t in text for t in STRONG_TOKENS):
        return "strong"
    return "weak"


def _verify_verdict(verify: list, with_codes: bool = True) -> dict:
    """给一张卡的所有 verify 一个总体结论。

    `verify` 有两种形状：任务卡 frontmatter 里是**字符串列表**（还没有执行结果），
    结果 json 里是 `{cmd, code, tail}` 字典列表。这里都要能吃下。
    `with_codes=False` 时只看强弱、不看退出码（给卡面/草稿用）。
    """
    entries = []
    for v in (verify or []):
        if isinstance(v, dict):
            cmd = str(v.get("cmd") or "")
            entries.append({"cmd": cmd, "code": v.get("code"),
                            "strength": _verify_strength(cmd)})
        else:
            cmd = str(v)
            entries.append({"cmd": cmd, "code": None,
                            "strength": _verify_strength(cmd)})
    has_strong = any(e["strength"] == "strong" for e in entries)
    if not with_codes:
        return {"verdict": "strong" if has_strong else "weak",
                "verify": entries, "has_strong": has_strong}
    all_green = bool(entries) and all(e["code"] == 0 for e in entries)
    if not all_green:
        verdict = "fail"
    elif has_strong:
        verdict = "strong-pass"
    else:
        verdict = "weak-pass"
    return {"verdict": verdict, "verify": entries, "has_strong": has_strong}


# 「agent 自己承认没真验」的口供。正则而非固定词表 —— T-0101 那张卡写的是
# 「未能启动…未得到实际测试结果」，靠枚举词永远追不上措辞的变化。
CONFESS_RE = re.compile(
    r"未能(?:验证|运行|启动|测试|执行|跑)|没能(?:跑|验证|测试)|"
    r"无法(?:验证|运行|启动|测试|执行)|没(?:有)?跑(?:测试|过)?|"
    r"未(?:测试|验证|得到实际|能验证)|跑不了|没法跑|未实际运行")

def changed_files(wt: Path) -> list[str]:
    """worktree 里相对 HEAD 改动过的文件（含未跟踪的）。重命名取新名字。"""
    out = []
    # core.quotePath=false：git 默认把非 ASCII 路径转义成八进制
    # （docs/操作手册.md → "docs/\346\223\245..."），中文名文件会被误判成越界
    # -uall：默认会把「整个未跟踪目录」折叠成目录名（docs/），
    # 里面的文件就看不见了，paths 检查会漏
    p = git(["-c", "core.quotePath=false", "status", "--porcelain", "-uall"],
            wt, check=False)
    for line in p.stdout.splitlines():
        if len(line) <= 3:
            continue
        name = line[3:].strip().strip('"')
        if " -> " in name:
            name = name.split(" -> ")[-1].strip().strip('"')
        if name:
            out.append(name)
    return out


def out_of_paths(declared, files) -> list[str]:
    """改动里不在任务卡声明的 paths 白名单内的文件。

    paths 为空表示不约束（返回空列表）；声明了就按「文件本身 or 它的子路径」判定。
    """
    allow = [str(p).strip().rstrip("/") for p in (declared or []) if str(p).strip()]
    if not allow:
        return []
    bad = []
    for f in files:
        if any(f == a or f.startswith(a + "/") for a in allow):
            continue
        bad.append(f)
    return bad


def strict_paths(cfg: dict) -> bool:
    """越界是否直接判失败。默认只警告，避免一上来就把正常流程卡死。"""
    v = (os.environ.get("HIVE_STRICT_PATHS") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    return bool(cfg.get("strict_paths"))


def _fmt_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def write_card(path: Path, data: dict, body: str) -> None:
    lines = ["---"]
    for k in TASK_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}:")
            else:
                lines.append(f"{k}:")
                lines.extend(f"  - {_fmt_scalar(x)}" for x in v)
        else:
            lines.append(f"{k}: {_fmt_scalar(v)}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# git 底层：把「一个文件」提交成某个分支，不打断当前工作区
# --------------------------------------------------------------------------- #

def commit_file_to_ref(repo: Path, path: str, content: str, ref: str,
                       parent: str = "origin/main", message: str = "",
                       force: bool = False) -> str:
    """构造 commit(parent=origin/main, tree=main+<path>) 并 push 到 refs/heads/<ref>。

    push 非快进会被 git 服务端拒绝 —— 这正是 claim 的原子性来源（保持非快进）。
    force=True 时改走 --force-with-lease：用于 result，新结果必须能顶掉上一次，
    否则重跑一次就在 `res/<tid>` 上非快进失败，结果永远落不进去。
    返回 commit sha；失败抛 HiveError。
    """
    parent_sha = rev_parse(repo, parent)
    if not parent_sha:
        raise HiveError(f"{repo} 找不到 {parent}，先 git fetch")
    blob = git(["hash-object", "-w", "--stdin"], repo, input=content).stdout.strip()
    # 用独立 index：即使多个 worker 共用同一个 clone，也不会互相踩 index
    fd, idx = tempfile.mkstemp(prefix="hive-index-")
    os.close(fd)
    env = {**os.environ, "GIT_INDEX_FILE": idx}
    try:
        run(["git", "read-tree", parent_sha], cwd=repo, env=env)
        run(["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
            cwd=repo, env=env)
        tree = run(["git", "write-tree"], cwd=repo, env=env).stdout.strip()
        commit = run(
            ["git", *IDENT, "commit-tree", tree, "-p", parent_sha,
             "-m", message or f"hive: {path}"],
            cwd=repo, env=env,
        ).stdout.strip()
        git_net(["push", *(["--force-with-lease"] if force else []),
                 "origin", f"{commit}:refs/heads/{ref}"], repo)
        return commit
    finally:
        try:
            os.unlink(idx)
        except OSError:
            pass


def read_ref_file(repo: Path, ref: str, path: str):
    p = git(["show", f"{ref}:{path}"], repo, check=False)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.stdout


PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 任务卡状态机。加新状态时务必同步改这几个集合，别让「在飞」的判断散落在各处字符串比较里。
STATES = ("ready", "assigned", "running", "needs_review",
          "conflict", "blocked", "done")
IN_FLIGHT_STATES = ("assigned", "running")   # 占了并发额度、worker 会去接的
# 需要人或另一个 agent 介入才能继续的：dispatch / worker 都跳过，doctor 要报
HANDOFF_STATES = ("needs_review", "conflict", "blocked")
# blocked 与 conflict 分开：conflict 是「必须尽快解」，doctor 按 error 报；blocked 是等资源
ERROR_STATES = ("conflict",)


def _pct(values, p: int) -> float:
    if not values:
        return 0.0
    k = max(0, min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1)))))
    return float(values[k])


def _fmt_dur(seconds: float) -> str:
    if not seconds:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# --------------------------------------------------------------------------- #
# 协调仓库
# --------------------------------------------------------------------------- #

class Hive:
    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory).expanduser().resolve()
        if not (self.dir / ".git").exists():
            raise HiveError(f"{self.dir} 不是 git 仓库（先 hive init）")

    # -- 基本读写 ---------------------------------------------------------- #
    def fetch(self) -> None:
        git_net(["fetch", "--prune", "origin",
                 "+refs/heads/*:refs/remotes/origin/*"], self.dir)

    def sync_main(self) -> None:
        """把协调仓库的本地 main 对齐到 origin/main，写操作前调用。

        只 fetch 不 reset 是不够的：卡片和结果是**工作区文件**，不 reset 读到的还是旧卡。
        更麻烦的是自举场景 —— 协调仓库和代码仓是同一个，`land` 刚把 main 推走，
        紧接着 `close` 还在旧的本地 main 上提交，`git push origin main` 必然非快进被拒。

        安全边界：本地领先（有 origin 没有的提交）就不动，让 push 正常把它们带上去；
        落后或分叉才 reset —— 且工作区必须是干净的，否则宁可报错也不丢人的改动。
        """
        self.fetch()
        head = rev_parse(self.dir, "HEAD")
        remote = rev_parse(self.dir, "origin/main")
        if not remote or not head or head == remote:
            return
        ahead = git(["merge-base", "--is-ancestor", "origin/main", "HEAD"],
                    self.dir, check=False).returncode == 0
        if ahead:
            return
        # 只看**已跟踪**文件的改动：reset --hard 会丢的就是这些。
        # 未跟踪的文件（草稿、编辑器 lock 文件）reset 不会动，不该拦着同步 ——
        # 否则总控只要有个零散文件，land → close 就会在这里卡死。
        dirty = git(["status", "--porcelain", "--untracked-files=no"],
                    self.dir, check=False).stdout.strip()
        if dirty:
            raise HiveError(
                "协调仓库落后于 origin/main，且有已跟踪文件被改动，不敢自动 reset。"
                "先处理掉这些改动（或手动 git pull）再跑：" + dirty.replace("\n", " ")[:200])
        git(["reset", "--hard", "-q", "origin/main"], self.dir)

    def _sync_for_commit(self) -> None:
        """只提交指定路径前，把协调仓对齐到 origin/main（close / land 收尾用）。

        和 sync_main 的分工：sync_main 服务「要读最新卡 / 要跑任务」的场景，落后且脏时
        宁可报错也不 reset（T-0129 立下的防数据丢失底线，不动它）；而 close 这类操作
        **只提交 tasks/<tid>.md + results/<tid>.json 两个指定路径**，本来就不该被
        工作区里无关的未提交改动拦住——多 agent 共用一台机器、主仓有人写到一半，那是
        日常（T-0133/T-0135 两晚被这块石头绊倒：合并推成功了，关卡却被拦下）。

        做法：落后就试 `merge --ff-only`。git 保证 ff 只更新 HEAD→origin/main 之间
        真正变化的文件——未提交改动只要不在其中就原样保留；万一真在其中，git 自己
        就会拒绝（"would be overwritten by merge"），不会丢任何东西。分叉等拿不到的
        情形才报错，并把脏文件列出来让人处理。
        """
        self.fetch()
        head = rev_parse(self.dir, "HEAD")
        remote = rev_parse(self.dir, "origin/main")
        if not remote or not head or head == remote:
            return
        if git(["merge", "--ff-only", "origin/main"], self.dir,
               check=False).returncode == 0:
            return
        dirty = git(["status", "--porcelain", "--untracked-files=no"],
                    self.dir, check=False).stdout.strip()
        raise HiveError(
            "协调仓库落后于 origin/main 且无法快进合并（未提交改动与传入改动重叠，"
            "或本地已分叉）。先处理掉这些改动再跑：" + dirty.replace("\n", " ")[:200])

    def _add(self, paths: list[str]) -> None:
        """只 stage 这些路径（已删除但仍被跟踪的也算，好把删除记进去）。

        路径不存在且没被跟踪时跳过 —— `git add -- 不存在的路径` 会直接 fatal。
        """
        want = []
        for p in paths:
            if (self.dir / p).exists():
                want.append(p)
            elif git(["ls-files", "--", p], self.dir, check=False).stdout.strip():
                want.append(p)
        if want:
            git(["add", "-A", "--", *want], self.dir)

    def commit_and_push(self, message: str, paths: list[str] | None = None) -> bool:
        """提交并推 main。给了 paths 就只提交这些文件。

        为什么必须收口：协调仓库的 main 是审计记录（卡片、结果、租约都在上面）。
        以前无条件 `git add -A`，总控工作区里任何未提交的改动都会被卷进这一次提交 ——
        提交信息说「加一张卡」，内容却是一个完整功能，事后没法靠 git log 回溯。
        已经发生过两次（MCP 那 705 行、sync_main 那个修复）。

        没给 paths 时保留老的全量行为（向后兼容），但会打一条告警。
        """
        if paths is not None:
            self._add(paths)
        else:
            log("commit_and_push 未指定 paths：将提交工作区全部改动"
                "（总控自己的手头活也会被卷进去）")
            git(["add", "-A"], self.dir)
        if git(["diff", "--cached", "--quiet"], self.dir, check=False).returncode == 0:
            return False
        git([*IDENT, "commit", "-m", message], self.dir)
        git_net(["push", "origin", "main"], self.dir)
        return True

    def _card_paths(self, tid: str, owner: str | None = None) -> list[str]:
        """一张卡在协调仓库里涉及的文件：任务卡 + 收件箱信号（+ 归档结果）。"""
        out = [f"tasks/{tid}.md"]
        if owner:
            out.append(f"inbox/{owner}/{tid}.md")
        out.append(f"results/{tid}.json")
        return out

    def cards(self) -> dict[str, dict]:
        out = {}
        for p in sorted((self.dir / "tasks").glob("T-*.md")):
            data, body = parse_card(p)
            data["_body"], data["_path"] = body, p
            out[data["id"]] = data
        return out

    def card(self, tid: str) -> dict:
        p = self.dir / "tasks" / f"{tid}.md"
        if not p.exists():
            raise HiveError(f"没有任务 {tid}")
        data, body = parse_card(p)
        data["_body"], data["_path"] = body, p
        return data

    def save_card(self, data: dict, body: str | None = None) -> None:
        p = data["_path"]
        write_card(p, data, data["_body"] if body is None else body)

    def agent(self, name: str) -> dict:
        p = self.dir / "agents" / f"{name}.json"
        if not p.exists():
            raise HiveError(f"没有登记 agent {name}")
        return json.loads(p.read_text(encoding="utf-8"))

    def claim_info(self, tid: str):
        return read_ref_file(self.dir, f"origin/claim/{tid}", f"claims/{tid}.json")

    def result_info(self, tid: str):
        # 先看进行中的 res 分支，再看 close 后归档到 main 的 results/
        data = read_ref_file(self.dir, f"origin/res/{tid}", f"results/{tid}.json")
        if data is not None:
            return data
        local = self.dir / "results" / f"{tid}.json"
        if local.exists():
            try:
                return json.loads(local.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return read_ref_file(self.dir, "origin/main", f"results/{tid}.json")

    # -- 命令 -------------------------------------------------------------- #
    def cmd_assign(self, tid: str, agent: str) -> None:
        self._assign_local(tid, agent)
        self.commit_and_push(f"assign {tid} -> {agent}",
                             self._card_paths(tid, agent))
        log(f"{tid} 已分派给 {agent}")

    def cmd_close(self, tid: str, note: str = "") -> None:
        # 不是 sync_main：close 只提交 tasks/<tid>.md + results/<tid>.json 两个指定
        # 路径，用 ff 合并对齐即可，不该被主仓里无关的未提交改动拦住（T-0136）
        self._sync_for_commit()
        card = self.card(tid)
        res = read_ref_file(self.dir, f"origin/res/{tid}", f"results/{tid}.json")
        if res is not None:
            # 归档到 main：分支可以删，审计不会丢
            (self.dir / "results").mkdir(exist_ok=True)
            (self.dir / "results" / f"{tid}.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        card["state"] = "done"
        body = card["_body"] + (f"\n\n---\n\n**完成备注（{iso()}）**：{note}\n" if note else "")
        self.save_card(card, body)
        self.commit_and_push(f"close {tid}" + (f": {note}" if note else ""),
                             [f"tasks/{tid}.md", f"results/{tid}.json"])
        # 收口：删掉 claim/res 分支，Gitea 分支列表里只剩进行中的任务
        for ref in (f"claim/{tid}", f"res/{tid}"):
            if rev_parse(self.dir, f"origin/{ref}"):
                try:
                    p = git_net(["push", "origin", f":refs/heads/{ref}"],
                                self.dir, check=False)
                except HiveError as e:
                    # 卡片已经 close 并推上去了，删分支只是收口；网络超时不能
                    # 反过来把一次成功的 close 报成失败。
                    log(f"删不掉 {ref}（{str(e).splitlines()[0]}），保留不影响结果")
                    continue
                if p.returncode == 0:
                    log(f"已删除分支 {ref}")
                else:
                    log(f"删不掉 {ref}（可能受保护），保留不影响结果")
        log(f"{tid} 已关闭")

    # -- 合入 / land ------------------------------------------------------- #
    def code_repo(self, repo_name: str) -> Path:
        """找代码仓的本地路径：任意 agent 登记过就行（同名的都指向同一份内容）。"""
        path = self.repos().get(repo_name)
        if not path:
            raise HiveError(f"没有 agent 登记代码仓 {repo_name!r} 的本地路径")
        if not path.is_dir() or not (path / ".git").exists():
            raise HiveError(f"本地代码仓不可用: {path}")
        return path

    def landable(self) -> list[str]:
        """可以合入的卡：结果是 done、verify 全绿、还没 close、依赖都已 close。"""
        cards = self.cards()
        out = []
        for tid, c in cards.items():
            if c.get("state") == "done":
                continue
            res = self.result_info(tid)
            if not res or res.get("status") != "done":
                continue
            if any(v.get("code") for v in (res.get("verify") or [])):
                continue
            if any((cards.get(d) or {}).get("state") != "done"
                   for d in (c.get("depends_on") or [])):
                continue
            out.append(tid)
        return sorted(out)

    def land_one(self, tid: str, apply: bool = False, note: str = "") -> dict:
        """把一张已完成的任务分支合进代码仓 main，再 close。

        默认不推任何东西：先在临时 worktree 里把合并做一遍，这样冲突是**真的**冲突，
        不是靠猜。 dry-run 时装作合并完就收工（并把这个 worktree 拆掉）。
        """
        plan: dict = {"task": tid, "repo": "", "branch": f"task/{tid}", "head": None,
                      "status": "blocked", "strategy": "", "problems": [],
                      "conflicts": [], "merged": False, "closed": False}
        card = self.card(tid)
        plan["repo"] = card.get("repo") or ""
        target = card.get("base") or "main"
        res = self.result_info(tid) or {}

        if card.get("state") == "done":
            plan["problems"].append("已经 close 过了")
            return plan
        if not res:
            plan["problems"].append("还没有结果")
            return plan
        if res.get("status") != "done":
            plan["problems"].append(f"结果是 {res.get('status')!r}，不是 done")
            return plan
        bad = [str(v.get("cmd")) for v in (res.get("verify") or []) if v.get("code")]
        if bad:
            plan["problems"].append("verify 未全绿: " + " | ".join(bad))
            return plan
        missing = [d for d in (card.get("depends_on") or [])
                   if (self.cards().get(d) or {}).get("state") != "done"]
        if missing:
            plan["problems"].append("依赖未 close: " + ", ".join(missing))
            return plan

        repo_name = card.get("repo") or ""
        try:
            repo = self.code_repo(repo_name)
        except HiveError as e:
            plan["problems"].append(str(e))
            return plan

        git_net(["fetch", "--prune", "origin"], repo)
        base, branch = f"origin/{target}", f"origin/task/{tid}"
        base_head = rev_parse(repo, base)
        task_head = rev_parse(repo, branch)
        if not task_head:
            plan["problems"].append(f"代码仓里没有 {branch} 分支")
            return plan
        if task_head == base_head:
            plan["problems"].append(f"已经合进 {target} 了（内容一致，无需合并）")
            return plan

        wt = DEFAULT_STATE / "land" / tid
        try:
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(["worktree", "add", "-B", f"land/{tid}", str(wt), base], repo)
            # merge 要建 commit，临时 worktree 里不一定有 git 身份 —— 显式带上 IDENT，
            # 别指望环境的 user.name（CI / 容器里经常没有）
            try:
                git([*IDENT, "merge", "--ff-only", "--no-edit", branch], wt)
                plan["strategy"] = "ff"
            except HiveError:
                git(["merge", "--abort"], wt, check=False)
                m = git([*IDENT, "merge", "--no-edit", "--no-ff", branch], wt, check=False)
                if m.returncode != 0:
                    conflicts = [l.strip() for l in git(
                        ["diff", "--name-only", "--diff-filter=U"], wt,
                        check=False).stdout.splitlines() if l.strip()]
                    if conflicts:
                        plan["status"] = "conflict"
                        try:
                            self._set_state(
                                tid, "conflict",
                                f"land 合并冲突（{len(conflicts)} 个文件），需人工解决",
                                "land", keep_owner=True)
                            plan["state_set"] = "conflict"
                        except HiveError as e:
                            log(f"{tid} 标记 conflict 失败：{e}")
                        plan["conflicts"] = conflicts
                        plan["problems"].append(
                            f"合并冲突（{len(conflicts)} 个文件）："
                            + ", ".join(conflicts[:8]))
                    else:
                        # 不是文件冲突（比如 git 身份缺失、目标被锁），把 git 的话带出来
                        plan["status"] = "failed"
                        tail = " | ".join(
                            (m.stdout + m.stderr).strip().splitlines()[-3:])
                        plan["problems"].append(f"合并失败: {tail}")
                    return plan
                plan["strategy"] = "merge"
            plan["head"] = rev_parse(wt, "HEAD")
            if not apply:
                plan["status"] = "would-merge"
                return plan
            try:
                git_net(["push", "origin",
                         f"{plan['head']}:refs/heads/{target}"], repo)
            except HiveError as e:
                if getattr(e, "timed_out", False):
                    # 超时和「被别人抢先推了」是两回事：别让人误以为是抢锁失败。
                    plan["status"] = "failed"
                    plan["problems"].append(f"推送 {target} 超时：{e}")
                else:
                    plan["problems"].append(
                        f"推送 {target} 失败（可能别人刚推过）: {e}")
                return plan
            plan["merged"] = True
        finally:
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)
            git(["branch", "-D", f"land/{tid}"], repo, check=False)

        try:
            self.cmd_close(tid, note=note or f"land {tid}")
        except HiveError as e:
            # 合并已经推上去了：必须说清"代码合入了、只是卡片没关成"，并给出收尾命令，
            # 不能裸抛同步错误让人以为整个 land 失败（T-0133/T-0135 都被这个坑过）
            plan["merged"] = True
            plan["status"] = "merged-not-closed"
            plan["problems"].append(
                f"合并已推入 {target}，但关卡失败：{e}。"
                f"处理好协调仓的未提交改动后跑 hive close {tid} 收尾")
            return plan
        plan["closed"] = True
        plan["status"] = "landed"
        return plan

    def cmd_land(self, tid: str | None = None, apply: bool = False, note: str = "",
                 all_tasks: bool = False, as_json: bool = False, limit: int | None = None):
        self.fetch()
        if not tid and not all_tasks:
            raise HiveError("land 需要给任务号，或加 --all")
        if all_tasks:
            targets = self.landable()
            if limit:
                targets = targets[:int(limit)]
        else:
            if tid not in self.cards():
                raise HiveError(f"没有任务 {tid}")
            targets = [tid]
        if not targets:
            log("没有可以合入的卡")
            results = []
        else:
            results = [self.land_one(t, apply=apply, note=note) for t in targets]
        if as_json:
            print(json.dumps(results if all_tasks else results[0],
                             ensure_ascii=False, indent=2))
            return results if all_tasks else (results[0] if results else None)
        for r in results:
            self._print_land(r, apply)
        return results if all_tasks else (results[0] if results else None)

    @staticmethod
    def _print_land(r: dict, apply: bool) -> None:
        tag = {"landed": "已合入并关闭", "would-merge": "可以合入（dry-run，没动）",
               "merged-not-closed": "已合入但未关闭（需补 hive close）",
               "conflict": "合并冲突", "failed": "合并失败",
               "blocked": "不满足合入条件"}.get(r["status"], r["status"])
        extra = f" [{r['strategy']}]" if r.get("strategy") else ""
        print(f"{r['task']}  {tag}{extra}  branch={r['branch']}  head={r.get('head') or '-'}")
        for p in r["problems"]:
            print(f"    问题: {p}")
        for c in r["conflicts"]:
            print(f"    冲突文件: {c}")
        if r["status"] == "would-merge":
            print("    加 --apply 真的合入")

    # -- 自动分派 / 体检 / 统计 ------------------------------------------- #
    def agents(self) -> dict[str, dict]:
        out = {}
        for p in sorted((self.dir / "agents").glob("*.json")):
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out[cfg.get("name") or p.stem] = cfg
        return out

    def repos(self) -> dict[str, Path]:
        """从 agent 配置收集代码仓 {名字: 路径}。

        多机协作：不同机器的 agent 会把各自的本地路径登记成同一个仓名
        （FreeBSD 的 /home/... 与 Windows 的 G:/... 都叫 "hive"）。解析优先级：
        1) 本机 agent（host 匹配）声明且路径存在；
        2) 任意 agent 声明且路径存在；
        3) 都没有 → 退回首个声明，让报错信息能带出路径。
        """
        here = current_host()
        agents = self.agents()
        ordered = sorted(agents.items(),
                         key=lambda kv: 0 if agent_host(kv[1]) == here else 1)
        out: dict[str, Path] = {}
        fallback: dict[str, Path] = {}
        for _name, cfg in ordered:
            for name, path in (cfg.get("repos") or {}).items():
                if name in out:
                    continue
                p = Path(path)
                fallback.setdefault(name, p)
                if p.exists():
                    out[name] = p
        for name, p in fallback.items():      # 本机都没有的仓：退回首个声明
            out.setdefault(name, p)
        return out

    def _load_counts(self, cards: dict) -> dict[str, int]:
        # 已分派但还没 claim 的也要算负载，否则 dispatch 会把同一台机器塞爆
        load: dict[str, int] = {}
        for tid, c in cards.items():
            # 只扫在飞的卡，别为几百张历史卡逐个 git show
            if c.get("state") not in IN_FLIGHT_STATES:
                continue
            if self.result_info(tid):
                continue
            claim = self.claim_info(tid)
            if claim:
                a = claim.get("agent") or "?"
            elif c.get("state") == "assigned" and c.get("owner"):
                a = c["owner"]
            else:
                continue
            load[a] = load.get(a, 0) + 1
        return load

    @staticmethod
    def _path_overlap(x: str, y: str) -> bool:
        """两条路径声明是否相互覆盖（含父子目录）。"""
        nx, ny = x.strip().rstrip("/"), y.strip().rstrip("/")
        if not nx or not ny:
            return True          # 空串等于「任何地方」
        if nx == ny:
            return True
        return nx.startswith(ny + "/") or ny.startswith(nx + "/")

    def _paths_conflict(self, a, b, a_locks=None, b_locks=None) -> bool:
        """两张卡声明的 paths / locks 会不会打架。

        判定顺序：

        1. **双方都声明了非空 `locks`**（文件级锁）→ 交给 `conflict._locks_conflict`
           按精确文件集合判：`src/a.py` 与 `src/b.py` 不冲突，`src/` 与 `src/a.py` 冲突。
        2. **否则回退旧规则**（向后兼容，旧卡行为一字不变）：任一方没声明 paths 就算
           冲突（未声明 = 可能改任何地方）；都声明了就按目录前缀重叠判（`_path_overlap`）。

        注意「保守优先」没被削弱：只有**两张卡都主动写了 `locks`** 才走精确逻辑，
        少写一方仍然是保守冲突 —— 否则少写个字段就能绕过并发保护。
        """
        if conflict is None:
            if a_locks or b_locks:
                raise HiveError(
                    "任务卡用了 locks 字段，但 hive.py 同目录下没有 conflict.py；"
                    "把 conflict.py 一起拷过来（或删掉卡上的 locks 改回 paths）")
            la, lb = [], []
        else:
            la = conflict.normalize_locks(a_locks)
            lb = conflict.normalize_locks(b_locks)
        if la and lb:
            return conflict._locks_conflict(la, lb)
        pa = [p for p in (a or []) if str(p).strip()]
        pb = [p for p in (b or []) if str(p).strip()]
        if not pa or not pb:
            return True
        return any(self._path_overlap(x, y) for x in pa for y in pb)

    def _in_flight(self, cards: dict) -> list[tuple[str, dict]]:
        """在飞的卡：已 assigned/running 且还没有结果。"""
        out = []
        for tid, c in cards.items():
            if c.get("state") not in IN_FLIGHT_STATES:
                continue
            if self.result_info(tid):
                continue
            out.append((tid, c))
        return out

    def dep_done(self, dep: str) -> bool:
        res = self.result_info(dep)
        return bool(res and res.get("status") == "done")

    def _assign_local(self, tid: str, agent: str) -> None:
        self.agent(agent)  # 校验已登记
        card = self.card(tid)
        card["state"] = "assigned"
        card["owner"] = agent
        self.save_card(card)
        inbox = self.dir / "inbox" / agent
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{tid}.md").write_text(
            f"# {tid} -> {agent}\n\n"
            f"assigned_at: {iso()}\n\n"
            f"任务卡：`tasks/{tid}.md`\n",
            encoding="utf-8",
        )

    def cmd_dispatch(self, apply: bool = False, limit: int | None = None,
                     only_agent: str | None = None, as_json: bool = False,
                     max_per_repo: int | None = None,
                     dry_run: bool = False,
                     only_host: str | None = None) -> None:
        # --dry-run 与默认行为一致；即使同时给了 --apply，也以 dry-run 为准
        apply = bool(apply) and not bool(dry_run)
        self.fetch()
        cards = self.cards()
        agents = self.agents()
        if not agents:
            raise HiveError("还没有登记任何 agent")
        if max_per_repo is None:
            try:
                max_per_repo = int(os.environ.get("HIVE_MAX_PER_REPO")
                                   or DEFAULT_MAX_PER_REPO)
            except ValueError:
                max_per_repo = DEFAULT_MAX_PER_REPO
        max_per_repo = max(1, int(max_per_repo or DEFAULT_MAX_PER_REPO))
        load = self._load_counts(cards)
        # 并发锁的粒度是 paths，不是 repo：同一 repo 里改互不相干目录的卡可以同时跑。
        # （自举阶段 hive 是唯一在开发的仓，按 repo 计数等于全串行）
        occupied = [(tid, c) for tid, c in self._in_flight(cards)]
        # needs_review / conflict / blocked 都跳过：要么等人、要么等资源、要么等解冲突
        ready = [c for c in cards.values()
                 if c.get("state") == "ready" and not c.get("owner")]
        ready.sort(key=lambda c: (PRIORITY_ORDER.get(c.get("priority"), 9), c["id"]))

        plan: list[tuple[dict, str, str]] = []
        skipped: list[tuple[str, str]] = []
        planned = dict(load)
        for c in ready:
            if limit and len(plan) >= limit:
                skipped.append((c["id"], f"超出本次 --limit {limit}"))
                continue
            repo = c.get("repo") or ""
            # 现阶段只派 hive 仓：其它代码仓的 worktree/合并约定还没收口
            if repo != "hive":
                skipped.append(
                    (c["id"], f"非 hive 仓任务暂不派发（repo={repo or '未填'}）"))
                continue
            missing = [d for d in (c.get("depends_on") or []) if not self.dep_done(d)]
            if missing:
                skipped.append((c["id"], "依赖未就绪: " + ", ".join(missing)))
                continue
            needs = set(c.get("needs") or [])
            cands: list[str] = []
            blocked: list[str] = []
            for name, a in agents.items():
                if only_agent and name != only_agent:
                    continue
                if only_host and agent_host(a) != only_host.strip().lower():
                    blocked.append(
                        f"机器不匹配（{name} 在 {agent_host(a) or '未标注'}，要 {only_host}）")
                    continue
                if c.get("agent") and name != c.get("agent"):
                    continue
                if repo not in (a.get("repos") or {}):
                    blocked.append(f"仓不匹配（{name} 未登记 {repo}）")
                    continue
                # abilities 是新的能力注册表；capabilities 继续兼容旧配置
                have = set(a.get("abilities") or []) | set(a.get("capabilities") or [])
                lack = needs - have
                if lack:
                    blocked.append(
                        f"能力缺失（{name} 缺 {', '.join(sorted(lack))}）")
                    continue
                mode = a.get("mode") or ("manual" if a.get("manual") else "exec")
                if mode == "manual":
                    blocked.append(f"人工优先（{name} 是 manual 模式）")
                    continue
                if mode == "verify" and (c.get("paths") or []):
                    blocked.append(
                        f"人工优先（{name} 是验收模式，不自动接改代码的卡）")
                    continue
                max_agent = int(a.get("max_concurrency") or 1)
                if planned.get(name, 0) >= max_agent:
                    blocked.append(
                        f"负载已满（{name} {planned.get(name, 0)}/{max_agent}）")
                    continue
                # 两张卡都写了 locks 就按文件级锁精确判（见 conflict.py），
                # 否则仍按 paths 目录前缀判 —— 旧卡行为不变。
                clash = [t for t, bc in occupied
                         if (bc.get("repo") or "") == repo
                         and self._paths_conflict(c.get("paths"), bc.get("paths"),
                                                  c.get("locks"), bc.get("locks"))]
                if len(clash) >= max_per_repo:
                    blocked.append(
                        f"paths/locks 冲突：与在飞的 {clash[0]} 抢同一片文件"
                        f"（{repo} 同组上限 {max_per_repo}）")
                    continue
                cands.append(name)
            if not cands:
                seen: list[str] = []
                for r in blocked:
                    if r not in seen:
                        seen.append(r)
                skipped.append((c["id"], "；".join(seen) or "没有空闲且能力匹配的 agent"))
                continue
            cands.sort(key=lambda n: (planned.get(n, 0),
                                      0 if c.get("agent") == n else 1, n))
            name = cands[0]
            max_agent = int(agents[name].get("max_concurrency") or 1)
            reason = (f"能力匹配；仓匹配（{repo}）；"
                      f"负载 {planned.get(name, 0)}/{max_agent}；"
                      f"paths/locks 与在飞的卡不冲突（同组上限 {max_per_repo}）；依赖就绪")
            plan.append((c, name, reason))
            planned[name] = planned.get(name, 0) + 1
            occupied.append((c["id"], c))  # 后续候选要躲开它占的 paths

        if as_json:
            print(json.dumps({
                "plan": [{"task": c["id"], "agent": n, "repo": c.get("repo"),
                          "priority": c.get("priority"), "reason": r}
                         for c, n, r in plan],
                "skipped": [{"task": t, "reason": r} for t, r in skipped],
                "max_per_repo": max_per_repo,
                "applied": bool(apply),
            }, ensure_ascii=False, indent=2))
            return

        if plan:
            print(f"{'TASK':<9}{'PRI':<5}{'REPO':<8}{'-> AGENT':<16}"
                  f"{'LOAD':<8}REASON")
            for c, n, r in plan:
                max_agent = int(agents[n].get("max_concurrency") or 1)
                print(f"{c['id']:<9}{c.get('priority', ''):<5}{c.get('repo', ''):<8}"
                      f"{'-> ' + n:<16}{planned.get(n, 0)}/{max_agent:<6}派：{r}")
        else:
            print("没有可分派的任务")
        for tid, reason in skipped:
            print(f"  skip {tid}（不派）: {reason}")

        if apply and plan:
            for c, n, _r in plan:
                self._assign_local(c["id"], n)
            summary = ", ".join(f"{c['id']}->{n}" for c, n, _r in plan)
            paths: list[str] = []
            for c, n, _r in plan:
                paths += self._card_paths(c["id"], n)
            self.commit_and_push(f"dispatch: {summary}", paths)
            log(f"已分派 {len(plan)} 张卡")
        elif plan:
            print("（dry-run；加 --apply 才写入）")
        return len(plan)

    def cmd_doctor(self) -> bool:
        errors: list[str] = []
        warns: list[str] = []
        if not (self.dir / ".git").exists():
            print("[ERR] 不是 git 仓库")
            return False
        try:
            self.fetch()
        except HiveError as e:
            warns.append(f"fetch 失败（离线？）: {str(e).splitlines()[0]}")
        if not rev_parse(self.dir, "origin/main"):
            errors.append("缺少 origin/main")

        cards: dict[str, dict] = {}
        for p in sorted((self.dir / "tasks").glob("T-*.md")):
            try:
                data, _body = parse_card(p)
            except HiveError as e:
                errors.append(str(e))
                continue
            tid = data.get("id")
            if tid != p.stem:
                errors.append(f"{p.name}: id={tid!r} 与文件名不一致")
            for k in ("title", "repo", "state"):
                if not data.get(k):
                    errors.append(f"{p.name}: 缺字段 {k}")
            if tid in cards:
                errors.append(f"重复 id {tid}")
            cards[tid] = data

        agents = self.agents()
        if not agents:
            warns.append("还没有登记 agent")
        for name, a in agents.items():
            mode = a.get("mode") or ("manual" if a.get("manual") else "exec")
            if mode not in ("exec", "verify", "manual"):
                errors.append(f"agent {name}: mode={mode!r} 非法")
            if mode == "exec" and not a.get("command"):
                errors.append(f"agent {name}: exec 模式没有 command")
            if not a.get("repos"):
                warns.append(f"agent {name}: 没有配置 repos")
            for repo, path in (a.get("repos") or {}).items():
                if not Path(path).expanduser().exists():
                    warns.append(f"agent {name}: 本机看不到 {repo}={path}（可能不在本机）")

        claim_refs = git(["for-each-ref", "--format=%(refname)",
                          "refs/remotes/origin/claim/"], self.dir).stdout.split()
        running_claims = 0
        for ref in claim_refs:
            tid = ref.rsplit("/", 1)[-1]
            info = read_ref_file(self.dir, ref, f"claims/{tid}.json")
            if tid not in cards:
                warns.append(f"租约 {tid} 没有对应任务卡")
            if not isinstance(info, dict) or not info.get("agent"):
                errors.append(f"租约 {ref} 内容不合法")
                continue
            has_res = self.result_info(tid)
            if not has_res:
                running_claims += 1
            lease = info.get("lease_until")
            if lease and not has_res:
                try:
                    if parse_iso(lease) < now():
                        warns.append(f"任务 {tid} 租约已过期（{info.get('agent')}）")
                except ValueError:
                    errors.append(f"租约 {tid} 的 lease_until 无法解析")

        res_refs = git(["for-each-ref", "--format=%(refname)",
                        "refs/remotes/origin/res/"], self.dir).stdout.split()
        for ref in res_refs:
            tid = ref.rsplit("/", 1)[-1]
            data = read_ref_file(self.dir, ref, f"results/{tid}.json")
            if tid not in cards:
                warns.append(f"结果 {tid} 没有对应任务卡")
            if not isinstance(data, dict):
                errors.append(f"结果 {ref} 不是合法 JSON")
                continue
            if data.get("status") not in ("done", "failed"):
                errors.append(f"结果 {tid} status={data.get('status')!r} 非法")
            if data.get("status") == "done" and any(v.get("code") for v in data.get("verify") or []):
                errors.append(f"结果 {tid} 标了 done 但 verify 有非零退出码")

        inbox = self.dir / "inbox"
        if inbox.is_dir():
            for d in sorted(inbox.iterdir()):
                if d.is_dir() and d.name not in agents:
                    warns.append(f"inbox/{d.name} 没有对应的 agent 登记")

        for p in sorted((self.dir / "results").glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                errors.append(f"归档结果 {p.name} 不是合法 JSON")
                continue
            if p.stem not in cards:
                warns.append(f"归档结果 {p.stem} 没有对应任务卡")
            if data.get("status") not in ("done", "failed"):
                errors.append(f"归档结果 {p.name} status={data.get('status')!r} 非法")

        for tid, c in cards.items():
            st = c.get("state")
            if st in ERROR_STATES:
                errors.append(f"{tid}: 状态 {st} —— 合并冲突，必须尽快解决")
            elif st == "needs_review":
                warns.append(f"{tid}: 状态 needs_review，等人确认")
            elif st == "blocked":
                # 「重试额度烧光」和「等外部资源」是两回事，得分开点名：
                # 前者说明这张卡反复失败、自动重派已停，需要人来决断。
                if self._retries_left(c) <= 0 and int(c.get("retries") or 0) > 0:
                    warns.append(
                        f"{tid}: blocked —— 重试额度烧光（retries="
                        f"{c.get('retries')} / max={self._max_retries(c)}），"
                        f"自动重派已停，需人工决断（hive retry 或改 max_retries）")
                else:
                    warns.append(f"{tid}: blocked（等资源/等外部条件）")
            # 看门狗视角：in-flight 卡的死活（与 hive watchdog 同源判定）
            if st in IN_FLIGHT_STATES:
                if self.result_info(tid) is not None:
                    warns.append(f"{tid}: 有结果却还停在 {st}（land_lag，该去 land 了）")
                else:
                    dclaim = self.claim_info(tid)
                    if dclaim is None:
                        if st == "running":
                            warns.append(f"{tid}: running 但没有 claim（孤儿，watchdog 可回收）")
                        else:
                            warns.append(f"{tid}: assigned 但没有 claim"
                                         f"（worker 可能还没来领，或已死）")
                    elif (dclaim.get("agent") or "") not in agents:
                        warns.append(f"{tid}: claim 的 agent {dclaim.get('agent')} 未登记（僵尸）")
            if st == "done" and not self.result_info(tid):
                warns.append(f"任务 {tid} 已关闭但没有结果记录")
            res_d = self.result_info(tid)
            if res_d and res_d.get("verify"):
                v = _verify_verdict(res_d["verify"])
                if v["verdict"] == "weak-pass":
                    warns.append(
                        f"任务 {tid} 只有弱验收（grep/test -f 之类），"
                        f"成果没被真正执行验证")
            if st == "ready" and c.get("owner"):
                warns.append(f"任务 {tid} 是 ready 但有 owner")
            for n in c.get("needs") or []:
                if not any(n in (set(a.get("abilities") or []) |
                                 set(a.get("capabilities") or []))
                           for a in agents.values()):
                    warns.append(f"任务 {tid} 需要能力 {n!r}，但没有 agent 声明")

        for w in warns:
            print(f"[WARN] {w}")
        for e in errors:
            print(f"[ERR ] {e}")
        if errors:
            print(f"[FAIL] {len(errors)} 个错误，{len(warns)} 个警告")
            return False
        print(f"[ OK ] {len(cards)} 张任务，{len(agents)} 个 agent，"
              f"{running_claims} 个进行中租约，{len(res_refs)} 个待合并结果")
        return True

    def cmd_stats(self, as_json: bool = False, days: int = 0) -> None:
        self.fetch()
        cards = self.cards()
        nowdt = now()
        rows = []
        for tid, c in cards.items():
            claim = self.claim_info(tid) or {}
            res = self.result_info(tid) or {}
            finished = res.get("finished_at") or ""
            if days and finished:
                try:
                    if (nowdt - parse_iso(finished)).days > days:
                        continue
                except ValueError:
                    pass
            rows.append({
                "id": tid,
                "state": c.get("state") or "",
                "repo": c.get("repo") or "",
                "priority": c.get("priority") or "",
                "agent": claim.get("agent") or res.get("agent") or "",
                "status": res.get("status") or "",
                "running": bool(not res.get("status") and
                                (claim or (c.get("state") == "assigned" and c.get("owner")))),
                "duration_s": float(res.get("duration_s") or 0),
                "finished_at": finished,
            })

        done = [r for r in rows if r["status"] == "done"]
        failed = [r for r in rows if r["status"] == "failed"]
        durations = sorted(r["duration_s"] for r in done if r["duration_s"] > 0)
        rate = (len(done) / (len(done) + len(failed)) * 100) if (done or failed) else 0.0
        median = statistics.median(durations) if durations else 0.0
        p90 = _pct(durations, 90)

        by_agent: dict[str, dict] = defaultdict(
            lambda: {"done": 0, "failed": 0, "running": 0, "durations": []})
        for r in rows:
            key = r["agent"] or "(未领取)"
            slot = by_agent[key]
            if r["status"] == "done":
                slot["done"] += 1
                if r["duration_s"]:
                    slot["durations"].append(r["duration_s"])
            elif r["status"] == "failed":
                slot["failed"] += 1
            elif r["running"]:
                slot["running"] += 1
        by_repo = Counter(r["repo"] or "(未填)" for r in rows)

        if as_json:
            print(json.dumps({
                "summary": {
                    "tasks": len(rows), "done": len(done), "failed": len(failed),
                    "rate": round(rate, 1), "median_s": round(median, 1),
                    "p90_s": round(p90, 1),
                    "running": sum(1 for r in rows if r["running"]),
                    "merge": sum(1 for r in rows
                                 if r["status"] == "done" and r["state"] != "done"),
                    "ready": sum(1 for r in rows if r["state"] == "ready" and not r["agent"]),
                    "closed": sum(1 for r in rows if r["state"] == "done"),
                },
                "agents": {k: {"done": v["done"], "failed": v["failed"],
                               "running": v["running"]} for k, v in by_agent.items()},
                "repos": dict(by_repo),
                "tasks": rows,
            }, ensure_ascii=False, indent=2))
            return

        merge = sum(1 for r in rows if r["status"] == "done" and r["state"] != "done")
        print(f"任务 {len(rows)} ｜ 完成 {len(done)} ｜ 失败 {len(failed)} ｜ "
              f"进行中 {sum(1 for r in rows if r['running'])} ｜ 待合并 {merge} ｜ "
              f"待领取 {sum(1 for r in rows if r['state'] == 'ready' and not r['agent'])} ｜ "
              f"已关闭 {sum(1 for r in rows if r['state'] == 'done')}")
        print(f"一次通过率 {rate:.1f}% ｜ 中位耗时 {_fmt_dur(median)} ｜ P90 {_fmt_dur(p90)}")
        if by_agent:
            print("按 agent:")
            for name in sorted(by_agent, key=lambda n: -(by_agent[n]["done"] + by_agent[n]["failed"])):
                v = by_agent[name]
                med = statistics.median(v["durations"]) if v["durations"] else 0
                print(f"  {name:<16} done {v['done']:<3} failed {v['failed']:<3} "
                      f"running {v['running']:<3} 中位 {_fmt_dur(med)}")
        if by_repo:
            print("按仓库: " + "  ".join(f"{k} {v}" for k, v in by_repo.most_common()))

    # -- 复盘摘要 ---------------------------------------------------------- #
    def digest_data(self, days: int = 7) -> dict:
        """汇总最近的结果，把失败原因聚成类。只读，不写任何东西。"""
        self.fetch()
        cards = self.cards()
        cutoff = now() - timedelta(days=days) if days > 0 else None
        rows = []
        for tid, c in cards.items():
            res = self.result_info(tid) or {}
            if not res:
                continue
            finished = res.get("finished_at") or ""
            if cutoff and finished:
                try:
                    if parse_iso(finished) < cutoff:
                        continue
                except ValueError:
                    pass
            rows.append({
                "id": tid, "title": c.get("title") or "",
                "agent": res.get("agent") or "", "status": res.get("status") or "",
                "duration_s": float(res.get("duration_s") or 0),
                "finished_at": finished,
                "error": res.get("error") or "",
                "verify": res.get("verify") or [],
                "repo": c.get("repo") or "",
                "state": c.get("state") or "",
                "out_of_paths": res.get("out_of_paths") or [],
            })
        rows.sort(key=lambda r: r["finished_at"] or r["id"])

        done = [r for r in rows if r["status"] == "done"]
        failed = [r for r in rows if r["status"] == "failed"]
        durs = sorted(r["duration_s"] for r in done if r["duration_s"] > 0)
        total = len(done) + len(failed)

        # 失败归因：从 error 归类，比「失败 3 张」有用得多
        reasons: dict[str, list[str]] = {}
        for r in failed:
            e = r["error"] or "(未说明)"
            if "超时" in e:
                key = "超时（卡可能拆得太大，或环境起不来）"
            elif "verify" in e:
                key = "verify 未通过"
            elif "越界" in e:
                key = "paths 越界（严格模式拦下）"
            elif "推送" in e:
                key = "分支推送失败"
            else:
                key = "其它"
            reasons.setdefault(key, []).append(r["id"])
        for r in done:
            if r["out_of_paths"]:
                reasons.setdefault(
                    "paths 越界（仅警告，未阻断）", []).append(r["id"])

        slow = sorted(done, key=lambda r: -r["duration_s"])[:5]
        # 反复重跑 = 第一版验收就没过。次数记在卡上（retry 会覆盖旧结果，
        # 没法从结果列表里数出来）
        retried = [tid for tid, c in cards.items()
                   if int(c.get("retries") or 0) > 0]

        return {
            "window_days": days,
            "summary": {
                "tasks": len(rows), "done": len(done), "failed": len(failed),
                "pass_rate": round(len(done) / total * 100, 1) if total else 0.0,
                "median_s": round(durs[len(durs) // 2], 1) if durs else 0,
                "p90_s": round(_pct(durs, 90), 1) if durs else 0,
                "slow_count": len(slow),
            },
            "failure_reasons": {k: sorted(set(v)) for k, v in reasons.items()},
            "slowest": [{"id": r["id"], "title": r["title"][:40],
                         "duration_s": round(r["duration_s"], 1),
                         "agent": r["agent"]} for r in slow],
            "retried": sorted(set(retried)),
            "by_agent": {
                a: {"done": sum(1 for r in done if r["agent"] == a),
                    "failed": sum(1 for r in failed if r["agent"] == a)}
                for a in sorted({r["agent"] for r in rows if r["agent"]})
            },
            "tasks": rows,
        }

    def cmd_digest(self, days: int = 7, out: str = "", as_json: bool = False):
        data = self.digest_data(days)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return data
        s = data["summary"]
        lines = [
            f"# hive 复盘摘要（最近 {days} 天）",
            "",
            f"任务 {s['tasks']} ｜ 完成 {s['done']} ｜ 失败 {s['failed']} ｜ "
            f"一次通过率 {s['pass_rate']}%",
            f"中位耗时 {_fmt_dur(s['median_s'])} ｜ P90 {_fmt_dur(s['p90_s'])}",
            "",
        ]
        if data["failure_reasons"]:
            lines.append("## 失败归因")
            lines.append("")
            for reason, tids in sorted(data["failure_reasons"].items(),
                                       key=lambda kv: -len(kv[1])):
                lines.append(f"- **{reason}**（{len(tids)} 张）：{', '.join(tids)}")
            lines.append("")
        if data["slowest"]:
            lines.append("## 耗时最长（可能是卡拆得不够小）")
            lines.append("")
            for r in data["slowest"]:
                lines.append(f"- {r['id']} {_fmt_dur(r['duration_s'])}"
                             f"（{r['agent']}）{r['title']}")
            lines.append("")
        if data["retried"]:
            lines.append("## 反复重跑的卡（第一版验收就没过）")
            lines.append("")
            lines.append("- " + ", ".join(data["retried"]))
            lines.append("")
        if data["by_agent"]:
            lines.append("## 按 agent")
            lines.append("")
            for a, v in data["by_agent"].items():
                lines.append(f"- {a}: done {v['done']} ｜ failed {v['failed']}")
            lines.append("")
        text = "\n".join(lines)
        if out:
            path = Path(out)
            if path.parent and str(path.parent):
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            log(f"复盘摘要已写入 {path}")
        print(text)
        return data

    # -- 手工领取 / 提交 ---------------------------------------------------- #
    def cmd_take(self, tid: str, agent: str) -> None:
        """人工领取：claim + assign + 准备 worktree，不启动 worker。

        顺序很关键：**先抢 claim 锁，抢到才 assign**。旧顺序是先写 assign 再 claim，
        抢锁失败时卡已经变成「owner=A、租约却在 B 手上」的半 assigned 状态。claim
        现在带退避重试，失败路径更长了，更不能在重试之前改卡（T-0146）。
        """
        self.fetch()
        card = self.card(tid)
        if card.get("state") == "done" or self.result_info(tid):
            raise HiveError(f"{tid} 已经有结果或已关闭")
        claim_holder = _claim_holder(self.claim_info(tid))
        if claim_holder and claim_holder != agent:
            raise HiveError(f"{tid} 已被 {claim_holder} 领取")

        w = Worker(self.dir, agent)
        wt = Path(w.cfg.get("worktrees") or (w.state / "wt")).expanduser() / tid
        if not claim_holder and not w.claim(tid):
            # Worker.claim 对「确认有主」的失败不重试、只返回 False；把主人带出来，
            # 别让调用方只看到一句「可能刚被抢走」。
            info = self.claim_info(tid)
            holder = _claim_holder(info) or "别人"
            raise HiveError(f"{tid} 已被 {holder} 领取，抢不到")

        # 锁到手之后才写入 assigned/owner 和 inbox；claim 失败时这里根本不会执行。
        self._assign_local(tid, agent)
        self.commit_and_push(f"take {tid} -> {agent}", self._card_paths(tid, agent))

        if not (wt / ".hive" / "task.md").exists():
            w.prepare(tid)

        print(f"\n=== {tid} 已领取（{agent}）=== "
              f"\n工作目录: {wt}"
              f"\n任务卡:   {wt / '.hive' / 'task.md'}"
              f"\n完成后写: {wt / '.hive' / 'summary.md'}"
              f"\n提交:     python3 hive.py --dir {self.dir} submit {tid}"
              f"\n（如果这个 agent 的 worker 正在跑，它会自动接手，不用再 submit）\n",
              flush=True)

    def cmd_submit(self, tid: str, keep: bool = False, note: str = "") -> None:
        """人工提交：commit + push + verify + 写结果，不依赖 worker 在跑。"""
        self.fetch()
        card = self.card(tid)
        if self.result_info(tid):
            raise HiveError(f"{tid} 已经有结果")
        owner = card.get("owner") or ""
        if not owner:
            raise HiveError(f"{tid} 没有 owner，先 take 或 assign")
        cfg = self.agent(owner)
        repo_path = (cfg.get("repos") or {}).get(card.get("repo") or "")
        if not repo_path:
            raise HiveError(f"agent {owner} 没有配置仓库 {card.get('repo')!r}")
        repo = Path(repo_path).expanduser()
        # 必须和 Worker.prepare 用同一套路径推导，否则设了 HIVE_STATE 时
        # take 建在 A 处、submit 去 B 处找，直接报「worktree 不存在」
        state_root = Path(os.environ.get("HIVE_STATE")
                          or DEFAULT_STATE / "agents" / owner).expanduser()
        wt = Path(cfg.get("worktrees") or (state_root / "wt")).expanduser() / tid
        if not wt.exists():
            raise HiveError(f"worktree 不存在: {wt}")

        head = rev_parse(wt, "HEAD")
        try:
            changed = changed_files(wt)
            oop = out_of_paths(card.get("paths"), changed)
            if oop:
                log(f"{tid} 越界改动 {len(oop)} 个文件：{', '.join(oop[:8])}")
                if strict_paths(cfg):
                    raise HiveError(
                        "越界改动（严格模式），拒绝提交：" + ", ".join(oop[:8]))
            git(["add", "-A"], wt)
            if git(["diff", "--cached", "--quiet"], wt, check=False).returncode != 0:
                git([*IDENT, "commit", "-m",
                     f"hive({tid}): {card.get('title', '')} (manual submit)"], wt)
            head = rev_parse(wt, "HEAD")
            git_net(["push", "--force", "origin",
                     f"HEAD:refs/heads/task/{tid}"], wt)
        except HiveError as e:
            raise HiveError(f"提交/推送失败: {e}")

        verify = []
        for cmd in (card.get("verify") or []):
            p = run(cmd, cwd=wt, check=False, shell=True, timeout=1800)
            verify.append({"cmd": cmd, "code": p.returncode,
                           "tail": (p.stdout + p.stderr).strip().splitlines()[-5:]})
            log(f"{tid} 验证 {p.returncode}: {cmd}")
        ok = all(v["code"] == 0 for v in verify)

        summary_file = wt / ".hive" / "summary.md"
        if summary_file.exists() and summary_file.stat().st_size > 0:
            summary = summary_file.read_text(encoding="utf-8").strip()
        elif note:
            summary = note
        else:
            summary = "（人工 submit，未写 summary）"

        claim = self.claim_info(tid) or {}
        started = claim.get("claimed_at") or iso()
        try:
            duration = (now() - parse_iso(started)).total_seconds()
        except ValueError:
            duration = 0.0
        result = {
            "task": tid, "agent": owner, "mode": "manual",
            "status": "done" if ok else "failed",
            "branch": f"task/{tid}", "base": card.get("base") or "main",
            "head": head, "exit_code": 0, "timed_out": False,
            "started_at": started, "finished_at": iso(),
            "duration_s": round(duration, 1), "verify": verify, "summary": summary,
            "error": "" if ok else "verify 未通过",
            "declared_paths": card.get("paths") or [],
            "changed_files": changed, "out_of_paths": oop,
        }
        commit_file_to_ref(
            self.dir, f"results/{tid}.json",
            json.dumps(result, ensure_ascii=False, indent=2),
            f"res/{tid}", "origin/main",
            f"result {tid}: {result['status']} by {owner} (manual)", force=True,
        )

        if not keep:
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)

        print(f"{tid} {result['status']} by {owner}（{result['duration_s']}s）"
              f"\nbranch: task/{tid}  head: {head}\n"
              f"verify: " + ", ".join(f"{v['code']}" for v in verify), flush=True)

    # -- 审计 -------------------------------------------------------------- #
    def cmd_audit(self, tid: str | None = None, all_cards: bool = False,
                  as_json: bool = False) -> list[dict]:
        """比对「agent 说做了什么」和「diff 里真有什么」，给人一页纸的合并前参考。"""
        self.fetch()
        cards = self.cards()
        targets = ([t for t in sorted(cards) if self.result_info(t)]
                   if all_cards else ([tid] if tid else []))
        if not tid and not all_cards:
            raise HiveError("audit 需要给任务号，或加 --all")
        if tid and tid not in cards:
            raise HiveError(f"没有任务 {tid}")

        reports = []
        for t in targets:
            card = cards.get(t)
            if card is None:
                continue
            res = self.result_info(t) or {}
            warns: list[str] = []
            if not (card.get("verify") or []):
                warns.append("任务卡没有写 verify")
            if not (card.get("paths") or []):
                warns.append("没声明 paths，改动范围不受约束")
            if res.get("out_of_paths"):
                warns.append("有越界改动：" + ", ".join(res["out_of_paths"][:8]))
            # 「agent 自己承认没真验」的口供。用正则而不是固定词表：
            # T-0101 写的是「未能启动…未得到实际测试结果」，一个个枚举词永远追不上措辞
            m = CONFESS_RE.search(res.get("summary") or "")
            if m:
                warns.append(f"summary 自称「{m.group(0)}」，验收结论要打折看")
            reports.append({
                "task": t, "title": card.get("title", ""),
                "state": card.get("state"), "result": res.get("status") or "",
                "agent": res.get("agent") or "",
                "declared_paths": card.get("paths") or [],
                "out_of_paths": res.get("out_of_paths") or [],
                "changed_files": res.get("changed_files") or [],
                "verify": [{"cmd": v.get("cmd"), "code": v.get("code")}
                           for v in (res.get("verify") or [])],
                "summary": res.get("summary") or "",
                "risk": "warn" if warns else "ok",
                "warnings": warns,
            })

        if as_json:
            print(json.dumps(reports if all_cards else
                             (reports[0] if reports else {}),
                             ensure_ascii=False, indent=2))
            return reports
        if not reports:
            print("（没有可审计的卡）")
            return reports
        for r in reports:
            print(f"\n=== {r['task']}  {r['title']}")
            print(f"  状态 {r['state']} ｜ 结果 {r['result'] or '无'} ｜ "
                  f"执行者 {r['agent'] or '-'} ｜ 风险 {r['risk']}")
            print(f"  声明改动范围: {', '.join(r['declared_paths']) or '（未声明）'}")
            if r["changed_files"]:
                print(f"  实际改动 {len(r['changed_files'])} 个文件: "
                      f"{', '.join(r['changed_files'][:12])}")
            if r["out_of_paths"]:
                print(f"  越界: {', '.join(r['out_of_paths'])}")
            for v in r["verify"]:
                print(f"  verify [{v['code']}] {v['cmd']}")
            for w in r["warnings"]:
                print(f"  ! {w}")
        return reports

    def cmd_status(self, as_json: bool = False) -> None:
        self.fetch()
        cards = self.cards()
        rows = []
        for tid, c in cards.items():
            claim = self.claim_info(tid) or {}
            res = self.result_info(tid) or {}
            state = c.get("state")
            if claim and not res and state not in ("done", "blocked"):
                state = "running"  # 看板推导，不写回任务卡
            lease_until = claim.get("lease_until")
            stale = bool(lease_until and not res and parse_iso(lease_until) < now())
            rows.append({
                "id": tid,
                "state": state,
                "owner": c.get("owner") or "",
                "title": c.get("title", ""),
                "repo": c.get("repo") or "",
                "priority": c.get("priority") or "",
                "depends_on": c.get("depends_on") or [],
                "claim": f"{claim.get('agent')}@{claim.get('claimed_at', '')[5:16]}"
                         if claim else "",
                "claim_agent": claim.get("agent") or "",
                "claimed_at": claim.get("claimed_at") or "",
                "lease_until": lease_until or "",
                "stale": stale,
                "result": res.get("status", ""),
                "result_status": res.get("status", ""),
                "duration_s": res.get("duration_s", 0),
                "head": res.get("head") or "",
                "verify": res.get("verify") or [],
                "summary": ((res.get("summary") or "").splitlines() or [""])[0][:60] if res else "",
                "result_summary": res.get("summary") or "",
            })
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        if not rows:
            print("（还没有任务）")
            return
        w = [4, 9, 12, 8, 16, 8]
        print(f"{'ID':<{w[0]}} {'STATE':<{w[1]}} {'OWNER':<{w[2]}} "
              f"{'RESULT':<{w[4]}} {'CLAIM':<{w[5]}} TITLE")
        for r in rows:
            print(f"{r['id']:<{w[0]}} {r['state']:<{w[1]}} {r['owner']:<{w[2]}} "
                  f"{r['result']:<{w[4]}} {r['claim']:<{w[5]}} {r['title'][:50]}")

    # -- 挂起 / 恢复 ------------------------------------------------------- #
    def _set_state(self, tid: str, state: str, reason: str, who: str,
                   keep_owner: bool = False) -> None:
        """把卡改成 needs_review / conflict / blocked，并写进完成备注。"""
        if state not in STATES:
            raise HiveError(f"未知状态 {state!r}（合法值：{', '.join(STATES)}）")
        self.sync_main()
        card = self.card(tid)
        old = card.get("state")
        card["state"] = state
        if not keep_owner and state != "assigned":
            card["owner"] = None if state == "blocked" else card.get("owner")
        body = (card["_body"] + f"\n\n---\n\n**{state}（{iso()}，by {who}）**：{reason}\n")
        self.save_card(card, body)
        self.commit_and_push(f"{state} {tid} by {who}: {reason}",
                             self._card_paths(tid))
        log(f"{tid}: {old} -> {state}（{reason}）")

    def cmd_block(self, tid: str, reason: str = "", who: str = "controller") -> None:
        """挂起一张卡：等外部资源，不是出错。dispatch 和 worker 都会跳过它。"""
        self._set_state(tid, "blocked", reason or "未说明", who)

    def cmd_unblock(self, tid: str, reason: str = "", who: str = "controller") -> None:
        """解除挂起，退回 ready（保留 owner 的话退成 assigned）。"""
        self.sync_main()
        card = self.card(tid)
        if card.get("state") != "blocked":
            raise HiveError(f"{tid} 的状态是 {card.get('state')}，不是 blocked")
        card["state"] = "assigned" if card.get("owner") else "ready"
        body = (card["_body"] + f"\n\n---\n\n**unblock（{iso()}，by {who}）**："
                f"{reason or '已解除挂起'}，回到 {card['state']}\n")
        self.save_card(card, body)
        self.commit_and_push(f"unblock {tid} by {who}", self._card_paths(tid))
        log(f"{tid} 已回到 {card['state']}")

    def cmd_retry(self, tid: str, agent: str | None = None, fresh: bool = False) -> None:
        card = self.card(tid)
        if fresh:
            card["fresh"] = True
        self.fetch()
        for ref in (f"res/{tid}", f"claim/{tid}"):
            if rev_parse(self.dir, f"origin/{ref}"):
                git_net(["push", "origin", f":refs/heads/{ref}"], self.dir,
                        check=False)
                log(f"已删除 {ref}")
        card["state"] = "assigned"
        if agent:
            card["owner"] = agent
        # retry 会覆盖上一次的结果，所以重跑次数只能记在卡上 ——
        # 否则 digest 永远看不出「这张卡反复在重跑」
        try:
            card["retries"] = int(card.get("retries") or 0) + 1
        except (TypeError, ValueError):
            card["retries"] = 1
        # 人工显式 retry 不受 max_retries 熔断限制（那是人在明确要求重跑），
        # 但超出额度时提醒一句，免得人自己也没意识到这张卡已经反复失败很多次了。
        if self._retries_left(card) < 0:
            log(f"提醒：{tid} 已重跑 {card['retries']} 次，超过 max_retries="
                f"{self._max_retries(card)}（自动回收早就停了，这次是你手工 retry）")
        self.save_card(card)
        target = card.get("owner")
        if target:
            inbox = self.dir / "inbox" / target
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / f"{tid}.md").write_text(
                f"# {tid} -> {target}\n\nretry_at: {iso()}\n\n任务卡：`tasks/{tid}.md`\n",
                encoding="utf-8")
        archived = self.dir / "results" / f"{tid}.json"
        if archived.exists():
            archived.unlink()  # 重新入队时把归档也撤掉，否则 worker 会以为已完成
        self.commit_and_push(f"retry {tid}", self._card_paths(tid, target))
        log(f"{tid} 已重新入队 -> {target}")

    def cmd_gc(self, minutes: int) -> None:
        self.fetch()
        cutoff = now() - timedelta(minutes=minutes)
        p = git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin/claim/"], self.dir)
        removed = 0
        for ref in p.stdout.split():
            tid = ref.rsplit("/", 1)[-1]
            if self.result_info(tid):
                continue
            info = read_ref_file(self.dir, ref, f"claims/{tid}.json") or {}
            claimed = info.get("claimed_at")
            if not claimed or parse_iso(claimed) > cutoff:
                continue
            git_net(["push", "origin", f":refs/heads/claim/{tid}"], self.dir,
                    check=False)
            log(f"回收过期租约 {tid}（{info.get('agent')} @ {claimed}）")
            removed += 1
        log(f"gc 完成，回收 {removed} 个")

    # -- 看门狗 ------------------------------------------------------------ #
    def _assigned_at(self, card: dict):
        """这张卡被分派的时间。

        `_assign_local` 实际把 `assigned_at` 写在 `inbox/<owner>/<tid>.md`（不在卡
        body），所以优先读那个文件；读不到再退回卡 body（兼容老卡）。
        """
        tid = card.get("id")
        owner = card.get("owner")
        if owner:
            inbox = self.dir / "inbox" / owner / f"{tid}.md"
            if inbox.exists():
                m = re.search(r"^assigned_at:\s*(\S+)",
                              inbox.read_text(encoding="utf-8"), re.MULTILINE)
                if m:
                    try:
                        return parse_iso(m.group(1))
                    except ValueError:
                        pass
        body = card.get("_body") or ""
        m = re.search(r"^assigned_at:\s*(\S+)", body, re.MULTILINE)
        if not m:
            return None
        try:
            return parse_iso(m.group(1))
        except ValueError:
            return None

    @staticmethod
    def _max_retries(card: dict) -> int:
        """这张卡允许被自动回收几次（卡面 max_retries 优先，缺省 DEFAULT_MAX_RETRIES）。

        注意别写成 `card.get("max_retries") or DEFAULT` —— 0 是假值，
        会被误当成「没设置」而退回默认 3，于是 max_retries=0 永远不生效。
        """
        v = card.get("max_retries")
        if v is None or v == "":
            return DEFAULT_MAX_RETRIES
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return DEFAULT_MAX_RETRIES

    @staticmethod
    def _retries_left(card: dict) -> int:
        """还剩几次自动回收额度。<=0 表示额度用尽，该判死转 blocked 了。"""
        try:
            used = int(card.get("retries") or 0)
        except (TypeError, ValueError):
            used = 0
        return Hive._max_retries(card) - used

    def _reenqueue_ready(self, tid: str, reason: str) -> None:
        """把一张 in-flight 卡退回 ready 池：删 claim 分支、清空 owner、+retries、留底备注。

        与 cmd_retry 的区别：retry 回到 assigned 并保留 owner（同 agent 续跑），
        这里回到 ready 且清空 owner（放回任务池，让 dispatch 重新分派）。

        熔断：自动回收额度（max_retries）用尽后不再退回 ready，转 blocked 退出流水线 ——
        否则一张注定失败的卡会被判死→重派→判死无限循环，常驻的 orchestrate 会一直烧 agent。
        人工显式 `hive retry` 不受此限（那是人在明确要求重跑）。
        """
        if rev_parse(self.dir, f"origin/claim/{tid}"):
            git_net(["push", "origin", f":refs/heads/claim/{tid}"], self.dir,
                    check=False)
        archived = self.dir / "results" / f"{tid}.json"
        if archived.exists():
            archived.unlink()  # 重新入队时把归档也撤掉，否则 worker 会以为已完成
        card = self.card(tid)
        if self._retries_left(card) <= 0:
            used = card.get("retries") or 0
            mx = self._max_retries(card)
            self._set_state(tid, "blocked",
                            f"自动回收额度用尽（retries={used} / max={mx}），"
                            f"停止自动重派以免无限循环；最后一次判定原因：{reason}。"
                            f"确认后人工 hive retry 或改 max_retries。", who="watchdog")
            log(f"{tid} 自动回收额度用尽（{used}/{mx}），转 blocked 等人工")
            return
        card["state"] = "ready"
        card["owner"] = None
        try:
            card["retries"] = int(card.get("retries") or 0) + 1
        except (TypeError, ValueError):
            card["retries"] = 1
        body = (card["_body"]
                + f"\n\n---\n\n**watchdog 回收（{iso()}）**：{reason}，回到 ready\n")
        self.save_card(card, body)
        self.commit_and_push(f"watchdog reclaim {tid}: {reason}", self._card_paths(tid))
        log(f"{tid} 已退回 ready（{reason}）")

    def cmd_watchdog(self, minutes: int = 0, apply: bool = False,
                     as_json: bool = False, orphan_grace: int = 30) -> int:
        """看门狗：找出死掉 / 孤儿 / 僵尸的 in-flight 卡。

        dry-run（默认）只报告、不改动；`--apply` 才把候选卡退回 ready。
        回收的判定（都满足「卡还在 in-flight、且没有 res 结果」为前提）：
          - orphan_running：state=running 但 claim 分支没了（崩溃 / 被 gc 清掉）
          - orphan_assigned：state=assigned 但一直没 claim，且分派时间早于 now-orphan_grace
                             （worker 一直没来领；刚分派的卡在 grace 内不回收，留给它领）
          - zombie：claim 的 agent 没在 agents/ 登记
          - stale：claim 的 lease_until 早于 now-minutes（心跳保证活着的 worker 不会过期）
          - land_lag（只 warn 不回收）：已经有 res 结果却还停在 in-flight，那是 land 的活
        `minutes` 只管 stale；`orphan_grace`（默认 30）只管 orphan_assigned。
        返回将被/已被回收的卡数量。
        """
        self.fetch()
        cards = self.cards()
        nowdt = now()
        grace = timedelta(minutes=minutes)
        ograce = timedelta(minutes=orphan_grace)
        candidates: list[dict] = []
        for tid, c in cards.items():
            if c.get("state") not in IN_FLIGHT_STATES:
                continue
            if self.result_info(tid) is not None:
                candidates.append({"id": tid, "reason": "land_lag", "action": "warn"})
                continue
            claim = self.claim_info(tid)
            if claim is None:
                if c.get("state") == "running":
                    candidates.append({"id": tid, "reason": "orphan_running", "action": "reclaim"})
                else:  # assigned 但还没 claim：可能刚分派，按 orphan_grace 阈值判断
                    at = self._assigned_at(c)
                    if at is not None and at < nowdt - ograce:
                        candidates.append({"id": tid, "reason": "orphan_assigned",
                                          "action": "reclaim"})
                continue
            agent = claim.get("agent")
            if (agent or "") not in self.agents():
                candidates.append({"id": tid, "reason": "zombie", "action": "reclaim"})
                continue
            lease = claim.get("lease_until")
            if lease:
                try:
                    expired = parse_iso(lease) < nowdt - grace
                except ValueError:
                    expired = True
                if expired:
                    candidates.append({"id": tid, "reason": "stale", "action": "reclaim"})

        # 熔断：自动回收额度用尽的，不再退回 ready，改判 blocked 退出流水线
        for c in candidates:
            if c["action"] != "reclaim":
                continue
            card = cards[c["id"]]
            if self._retries_left(card) <= 0:
                c["action"] = "block"
                c["retries"] = int(card.get("retries") or 0)
                c["max_retries"] = self._max_retries(card)

        to_reclaim = [c for c in candidates if c["action"] == "reclaim"]
        to_block = [c for c in candidates if c["action"] == "block"]
        if as_json:
            print(json.dumps({
                "candidates": candidates,
                "would_reclaim": len(to_reclaim) if not apply else 0,
                "would_block": len(to_block) if not apply else 0,
                "reclaimed": len(to_reclaim) if apply else 0,
                "blocked": len(to_block) if apply else 0,
            }, ensure_ascii=False, indent=2))
        if not apply:
            for c in candidates:
                tag = {"reclaim": "回收", "block": "转blocked"}.get(c["action"], "提示")
                log(f"[watchdog] {tag} {c['id']}：{c['reason']}")
            log(f"[watchdog] dry-run：{len(to_reclaim)} 张将被回收、"
                f"{len(to_block)} 张将转 blocked（--apply 才动手）")
            return len(to_reclaim) + len(to_block)
        for c in to_reclaim + to_block:
            self._reenqueue_ready(c["id"], c["reason"])
            verb = "已转 blocked" if c["action"] == "block" else "已回收"
            log(f"[watchdog] {verb} {c['id']}：{c['reason']}")
        log(f"[watchdog] 完成，回收 {len(to_reclaim)} 张、"
            f"转 blocked {len(to_block)} 张")
        return len(to_reclaim) + len(to_block)

    # -- 总控进程 ---------------------------------------------------------- #
    def cmd_orchestrate(self, interval: int = 60, once: bool = False,
                        iterations: int | None = None, dispatch: bool = True,
                        watchdog: bool = True, land: bool = True, doctor: bool = True,
                        dispatch_limit: int | None = None, max_land: int | None = None,
                        watchdog_minutes: int = 180, orphan_grace: int = 45,
                        stop_file: str | None = None, as_json: bool = False) -> None:
        """总控进程（脑）：循环跑 看门狗 → 分派 → 合入 → 体检，替代人工 drive。

        每轮 tick 顺序：
          1. watchdog --apply（回收死卡 / 孤儿 / 僵尸）
          2. dispatch --apply（把 ready 卡分派给空闲且能力匹配的 exec agent）
          3. land --all --apply（把 verify 全绿、依赖就绪的 done 卡合入并 close）
          4. doctor（报告问题，不改动）
        单轮内任何子步骤抛 HiveError 都 catch 住、记 log、继续下一轮，绝不整进程崩。
        安全默认：三个写操作本身都是「带 --apply 才真改」，这里显式传了 --apply，
        不引入新的破坏性开关。manual agent 的卡 dispatch 本来就不碰，留给人工。
        """
        total: dict[str, int] = {"reclaimed": 0, "dispatched": 0, "landed": 0, "ticks": 0}
        while True:
            total["ticks"] += 1
            tick = {"tick": total["ticks"], "reclaimed": 0, "dispatched": 0,
                    "landed": 0, "doctor_ok": True}
            try:
                if watchdog:
                    reclaimed = self.cmd_watchdog(minutes=watchdog_minutes,
                                                 apply=True, orphan_grace=orphan_grace)
                    tick["reclaimed"] = reclaimed
                if dispatch:
                    dispatched = self.cmd_dispatch(apply=True, limit=dispatch_limit)
                    tick["dispatched"] = dispatched
                if land:
                    res = self.cmd_land(all_tasks=True, apply=True, limit=max_land)
                    ok = [r for r in res if isinstance(r, dict) and r.get("closed")]
                    tick["landed"] = len(ok)
                if doctor:
                    tick["doctor_ok"] = self.cmd_doctor()
            except HiveError as e:
                log(f"[orchestrate] tick {total['ticks']} 出错（继续）: {e}")
            total["reclaimed"] += tick["reclaimed"]
            total["dispatched"] += tick["dispatched"]
            total["landed"] += tick["landed"]

            if as_json:
                print(json.dumps(tick, ensure_ascii=False))
            else:
                log(f"[orchestrate] tick {total['ticks']} 完成: "
                    f"回收={tick['reclaimed']} 分派={tick['dispatched']} "
                    f"合入={tick['landed']} 体检={'OK' if tick['doctor_ok'] else '有告警'}")

            if once:
                break
            if iterations and total["ticks"] >= int(iterations):
                log(f"[orchestrate] 已跑满 {iterations} 轮，退出")
                break
            if stop_file and Path(stop_file).exists():
                log(f"[orchestrate] 发现停止文件 {stop_file}，退出")
                break
            time.sleep(interval)

    # -- 分支清理 ---------------------------------------------------------- #
    @staticmethod
    def _default_remote_ref(repo: Path) -> str | None:
        for cand in ("origin/main", "origin/master"):
            if rev_parse(repo, cand):
                return cand
        return None

    @staticmethod
    def _merged_into(repo: Path, branch: str, base: str) -> bool:
        # 真合并（tip 是 base 的祖先）或内容一致（squash 合并后 diff 为空）
        if git_ok(["merge-base", "--is-ancestor", branch, base], repo):
            return True
        return git_ok(["diff", "--quiet", branch, base], repo)

    def cmd_prune(self, repo: str | None = None, apply: bool = False) -> None:
        """清理代码仓里已合并的 task/*、verify/* 分支。默认 dry-run。"""
        self.fetch()
        repos = self.repos()
        if repo:
            if repo not in repos:
                raise HiveError(
                    f"没有登记过代码仓 {repo!r}（已知：{', '.join(sorted(repos)) or '无'}）")
            repos = {repo: repos[repo]}
        if not repos:
            log("没有登记任何代码仓（agent 配置的 repos 为空）")
            return
        # 在飞的卡不许碰：有卡没结果的，分支可能正被 worker 写着
        cards = self.cards()
        busy = {tid for tid, c in cards.items()
                if c.get("state") in IN_FLIGHT_STATES
                and not self.result_info(tid)}
        deleted = kept = 0
        for name, path in sorted(repos.items()):
            if not path.is_dir():
                log(f"{name}: 路径不存在 {path}，跳过")
                continue
            git_net(["fetch", "-q", "origin"], path, check=False)
            base = self._default_remote_ref(path)
            if not base:
                log(f"{name}: 找不到 origin/main 或 origin/master，跳过")
                continue
            p = git(["for-each-ref", "--format=%(refname)",
                     "refs/remotes/origin/task/", "refs/remotes/origin/verify/"],
                    path, check=False)
            branches = [r[len("refs/remotes/origin/"):] for r in p.stdout.split()]
            current = git(["branch", "--show-current"], path, check=False).stdout.strip()
            for br in sorted(branches):
                tid = br.split("/", 1)[1] if "/" in br else ""
                if tid in busy:
                    log(f"{name}/{br}: {tid} 在飞，保留")
                    kept += 1
                    continue
                if not self._merged_into(path, f"origin/{br}", base):
                    log(f"{name}/{br}: 未合并进 {base}，保留")
                    kept += 1
                    continue
                if not apply:
                    log(f"[dry-run] {name}/{br}: 已合并，将删除")
                    deleted += 1
                    continue
                d = git_net(["push", "origin", f":refs/heads/{br}"], path,
                            check=False)
                if d.returncode != 0:
                    log(f"{name}/{br}: 远端删除失败（受保护？），保留")
                    kept += 1
                    continue
                if br != current:  # 本地分支也清；还被 worktree 占着就只删远端
                    git(["branch", "-D", br], path, check=False)
                log(f"{name}/{br}: 已删除（已合并进 {base}）")
                deleted += 1
            # 本地 verify/* 遗留：verify 模式不推远端，worktree 删了分支还在。
            # 这种分支随时能被 worker 按 base 重建，删了不丢东西
            loc = git(["for-each-ref", "--format=%(refname)", "refs/heads/verify/"],
                      path, check=False)
            for ref in sorted(loc.stdout.split()):
                br = ref[len("refs/heads/"):]
                tid = br.split("/", 1)[1] if "/" in br else ""
                if tid in busy:
                    continue
                if not apply:
                    log(f"[dry-run] {name}/{br}: 本地遗留，将删除")
                    deleted += 1
                    continue
                d = git(["branch", "-D", br], path, check=False)
                if d.returncode == 0:
                    log(f"{name}/{br}: 本地遗留，已删除")
                    deleted += 1
                # 删不掉说明还被某个 worktree 占着，不动它
        log(f"prune 完成：删除 {deleted}，保留 {kept}"
            + ("" if apply else "（dry-run，加 --apply 才真删）"))


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

class Worker:
    def __init__(self, hive_dir: str, agent_name: str, keep_worktree=False,
                 hive_state: str | None = None, max_concurrency: int | None = None):
        self.h = Hive(hive_dir)
        self.dir = self.h.dir
        self.name = agent_name
        # 先同步再读配置：agent 的登记信息可能刚由总控推到 main。
        # 用 sync_main 而不是裸 `fetch + reset --hard`：worker 常常跑在**主工作区**上
        # （hive take / work / submit 都是），裸 reset 会把人未提交的已跟踪改动静默抹掉
        # —— 比如正写到一半的 drafts/ 草稿。sync_main 的守卫：对齐或领先就不动工作区，
        # 落后且干净才 reset，落后且脏直接报错（宁可报错也不丢人的改动）。
        self.h.sync_main()
        self.cfg = self.h.agent(agent_name)
        self.keep = keep_worktree
        self.state = Path(hive_state or os.environ.get("HIVE_STATE")
                          or DEFAULT_STATE / "agents" / agent_name).expanduser()
        (self.state / "done").mkdir(parents=True, exist_ok=True)
        (self.state / "logs").mkdir(parents=True, exist_ok=True)
        self.cmd = self.cfg.get("command") or ""
        self.launch = self.cfg.get("launch") or ""
        self.mode = self.cfg.get("mode") or ("manual" if self.cfg.get("manual") else "exec")
        self.manual = self.mode == "manual"
        # 并发上限：显式参数优先（start-worker.sh --max-concurrency），否则用 agent
        # 配置的 max_concurrency；1 表示串行（与旧行为逐字一致）。
        self.max_workers = max(1, int(max_concurrency or self.cfg.get("max_concurrency") or 1))
        if self.mode == "exec" and not self.cmd:
            raise HiveError(f"agent {agent_name} 没配置 command（或设 mode: verify / manual）")

    # -- 队列 -------------------------------------------------------------- #
    def pending(self) -> list[tuple[str, dict]]:
        inbox = self.dir / "inbox" / self.name
        if not inbox.is_dir():
            return []
        out = []
        cards = self.h.cards()
        for p in sorted(inbox.glob("T-*.md")):
            tid = p.stem
            marker = self.state / "done" / tid
            if marker.exists():
                if self.h.result_info(tid):
                    continue
                marker.unlink()  # 总控已 retry，远端结果被清掉，允许重跑
            if self.h.result_info(tid):
                continue
            claim = self.h.claim_info(tid)
            if claim and claim.get("agent") != self.name:
                continue
            card = cards.get(tid)
            if not card or card.get("state") in ("done",) + HANDOFF_STATES:
                continue
            deps = card.get("depends_on") or []
            if any(not self._dep_done(d) for d in deps):
                log(f"{tid} 依赖未完成: {deps}")
                continue
            out.append((tid, card))
        return out

    def _dep_done(self, dep: str) -> bool:
        res = self.h.result_info(dep)
        return bool(res and res.get("status") == "done")

    # -- 领取 -------------------------------------------------------------- #
    def claim(self, tid: str) -> bool:
        """抢 claim/<tid> 锁：失败后带抖动指数退避，区分「被抢」和「暂时失败」。

        * 已被别人领取（fetch 后能确认 claim 有主）：不重试，立即把持有者
          报给调用方（预检到返回 False，抢锁过程中确认则抛 HiveError）；
        * stale ref / timeout / 5xx：先 fetch 刷新引用，再退避重试。总次数由
          HIVE_CLAIM_MAX_RETRIES 控制，超了抛 HiveError（绝不无限退避）；
        * push 的原子性完全不动：仍然是普通 push，非快进拒绝就是拒绝，绝不 force。
        """
        cfg = claim_backoff_config()
        info = self.h.claim_info(tid)
        holder = _claim_holder(info)
        if holder and holder != self.name:
            log(f"{tid} 已被 {holder} 领取，不重试")
            _cleanup_claim_artifacts(self.dir, tid)
            return False
        lease = int(self.h.card(tid).get("lease_minutes") or 180)
        payload = json.dumps({
            "task": tid, "agent": self.name,
            "claimed_at": iso(),
            "lease_until": iso(now() + timedelta(minutes=lease)),
            "host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", ""),
        }, ensure_ascii=False, indent=2)

        last_err: Exception | None = None
        succeeded = False
        refreshed = True
        holder = ""
        try:
            for attempt in range(cfg["retries"] + 1):
                try:
                    commit_file_to_ref(self.dir, f"claims/{tid}.json", payload,
                                       f"claim/{tid}", "origin/main",
                                       f"claim {tid} by {self.name}")
                except HiveError as e:
                    last_err = e
                    if not _claim_error_is_retryable(e):
                        raise
                    # 先刷新引用再判断：本地 origin/claim/<tid> 可能还是旧快照。
                    # fetch 自己也可能超时/抖动，不能因此把已经决定的重试直接掐掉；
                    # 但 fetch 没成功时也别拿旧快照断言「卡有主」。
                    refreshed = True
                    try:
                        self.h.fetch()
                    except HiveError as fe:
                        if not _claim_error_is_retryable(fe):
                            raise
                        refreshed = False
                        log(f"{tid} 重试前 fetch 失败（继续退避）: {_claim_error_summary(fe)}")
                    info = self.h.claim_info(tid)
                    holder = _claim_holder(info)
                    if holder and refreshed:
                        if holder == self.name:
                            # push 报错但远端已经是我的 claim：上一次其实成功了
                            # （典型是响应丢了/超时后才落地），别再从头抢。
                            log(f"{tid} 领取成功（远端已有我的 claim）")
                            succeeded = True
                            return True
                        # 能确认有主：正常的竞争失败，不进退避。
                        raise HiveError(
                            f"{tid} 已被 {holder} 领取（抢锁失败，不重试；"
                            f"push: {_claim_error_summary(e)}）")
                    if holder and not refreshed:
                        log(f"{tid} 本地引用显示 {holder} 持有，但刷新失败，先不据此放弃")
                    if attempt >= cfg["retries"]:
                        break
                    delay = claim_backoff_delay(attempt + 1, cfg)
                    log(f"{tid} 领取失败（{_claim_error_summary(e)}），"
                        f"{delay:.2f}s 后重试（{attempt + 1}/{cfg['retries']}）")
                    _claim_sleep(delay)
                else:
                    log(f"{tid} 领取成功")
                    succeeded = True
                    return True
            # 次数用尽：把「谁拿了」写进错误里，别只报 push 失败。
            if refreshed and holder == self.name:
                succeeded = True
                return True
            if refreshed and holder:
                raise HiveError(
                    f"{tid} 领取失败：已重试 {cfg['retries']} 次仍未抢到，"
                    f"当前持有者 {holder}")
            if holder:
                raise HiveError(
                    f"{tid} 领取失败：已重试 {cfg['retries']} 次仍未成功，"
                    f"本地引用显示 {holder} 持有但刷新失败，无法确认；"
                    f"最后错误：{_claim_error_summary(last_err)}")
            raise HiveError(
                f"{tid} 领取失败：已重试 {cfg['retries']} 次仍未成功"
                f"（可能引用陈旧或网络不稳；最后错误：{_claim_error_summary(last_err)}）")
        finally:
            if not succeeded:
                # 中断（KeyboardInterrupt）或重试用尽：清掉本地可能留下的 claim
                # 半成品；远端失败 push 本身不会改任何引用。
                _cleanup_claim_artifacts(self.dir, tid)

    # -- 租约心跳 ---------------------------------------------------------- #
    @contextmanager
    def lease_heartbeat(self, tid: str):
        """任务运行期间周期性刷新 claim 的 lease_until。

        活着的 worker 自己续租，watchdog 才能放心把「租约过期」判成死亡
        （否则健康的长任务会被错杀）。best-effort：刷新失败只 log，不打断任务。
        """
        stop = threading.Event()
        lease = int(self.h.card(tid).get("lease_minutes") or 180)
        interval = max(5, lease // 3) * 60  # 秒
        t = threading.Thread(target=self._heartbeat_loop, args=(tid, interval, stop),
                            name=f"hb-{tid}", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=5)

    def _heartbeat_loop(self, tid: str, interval: int, stop: threading.Event) -> None:
        while not stop.wait(interval):
            try:
                self._refresh_lease(tid)
            except Exception as e:  # noqa: BLE001  best-effort，绝不影响任务
                log(f"{tid} 心跳刷新失败（忽略）: {e}")

    def _refresh_lease(self, tid: str) -> None:
        """续租：把 claim 的 lease_until 往前推。父提交必须是当前 claim 分支才能快进。"""
        info = self.h.claim_info(tid)
        if not isinstance(info, dict) or info.get("agent") != self.name:
            return  # 没 claim 或不是自己的，no-op
        lease = int(self.h.card(tid).get("lease_minutes") or 180)
        payload = dict(info)
        payload["lease_until"] = iso(now() + timedelta(minutes=lease))
        payload["refreshed_at"] = iso()
        self.h.fetch()
        commit_file_to_ref(
            self.h.dir, f"claims/{tid}.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            f"claim/{tid}", f"origin/claim/{tid}",
            f"lease {tid} by {self.name}")

    # -- 手工领取 / 提交（take / submit） ---------------------------------- #
    def prepare(self, tid: str) -> dict:
        """建 worktree + 写 task.md，不执行、不等待。供 `hive take` 使用。"""
        card = self.h.card(tid)
        repo_name = card.get("repo") or ""
        repo_path = (self.cfg.get("repos") or {}).get(repo_name)
        if not repo_path:
            raise HiveError(f"agent {self.name} 没有配置仓库 {repo_name!r} 的本地路径")
        repo = Path(repo_path).expanduser()
        if not (repo / ".git").exists():
            raise HiveError(f"本地仓库不存在: {repo}")

        git_net(["fetch", "--prune", "origin"], repo)
        wt_root = Path(self.cfg.get("worktrees") or (self.state / "wt")).expanduser()
        wt_root.mkdir(parents=True, exist_ok=True)
        wt = wt_root / tid
        if wt.exists():
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)

        base = f"origin/{card.get('base') or 'main'}"
        if not card.get("fresh") and rev_parse(repo, f"origin/task/{tid}"):
            base = f"origin/task/{tid}"
        git(["worktree", "add", "-B", f"task/{tid}", str(wt), base], repo)

        ex = git(["rev-parse", "--git-path", "info/exclude"], wt).stdout.strip()
        ex_path = (wt / ex).resolve() if not os.path.isabs(ex) else Path(ex)
        try:
            old = ex_path.read_text(encoding="utf-8") if ex_path.exists() else ""
            if ".hive/" not in old:
                ex_path.write_text(old + "\n.hive/\n", encoding="utf-8")
        except OSError:
            pass

        (wt / ".hive").mkdir(exist_ok=True)
        prompt = PROMPT_HEADER.format(tid=tid) + self._render_card(card, wt)
        (wt / ".hive" / "task.md").write_text(prompt, encoding="utf-8")
        return {"card": card, "repo": repo, "wt": wt, "prompt": prompt,
                "summary_file": wt / ".hive" / "summary.md"}

    # -- 执行 -------------------------------------------------------------- #
    def run_tasks(self, tids, max_workers: int | None = None) -> dict:
        """并发跑多张**已领取**的卡：每张一个任务线程，各自独占自己的 worktree。

        * 并发上限取显式 `max_workers`，否则用 `self.max_workers`
          （构造时的 `--max-concurrency` 或 agent 配置，默认 1）。
        * 上限为 1 或只有一张卡时退化回串行 `run_task`，单卡路径与旧行为逐字一致
          （`hive take` / `hive work` 的单卡语义不变）。
        * 单任务线程崩溃/超时**只判本卡失败**，就地转成 failed 结果，不拖垮池里其他
          任务（错误隔离）；每张卡的日志本来就写各自的 `state/logs/<tid>.log`。
        * 返回 `{tid: result}`，与 `run_task` 的单卡结果同构，便于收集与断言。
        """
        tids = list(tids)
        if not tids:
            return {}
        if max_workers is None:
            max_workers = self.max_workers
        max_workers = max(1, int(max_workers))
        if len(tids) == 1 or max_workers == 1:
            return {tid: self.run_task(tid) for tid in tids}

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"hive-{self.name}") as pool:
            futures = {pool.submit(self.run_task, tid): tid for tid in tids}
            for fut in as_completed(futures):
                tid = futures[fut]
                try:
                    results[tid] = fut.result()
                except Exception as e:  # noqa: BLE001  错误隔离：只算本卡失败
                    log(f"{tid} 并发执行异常（不拖垮其他任务）: {e!r}")
                    try:
                        card = self.h.card(tid)
                    except Exception:  # noqa: BLE001  取不到卡也要能落一条失败
                        card = {}
                    results[tid] = self._fail(
                        tid, card, None, f"并发执行异常: {e!r}")
        return results

    def run_task(self, tid: str) -> dict:
        card = self.h.card(tid)
        repo_name = card.get("repo") or ""
        repo_path = (self.cfg.get("repos") or {}).get(repo_name)
        if not repo_path:
            return self._fail(tid, card, None, f"agent {self.name} 没有配置仓库 {repo_name!r} 的本地路径")

        repo = Path(repo_path).expanduser()
        if not (repo / ".git").exists():
            return self._fail(tid, card, None, f"本地仓库不存在: {repo}")

        started = iso()
        t0 = time.time()
        git_net(["fetch", "--prune", "origin"], repo)
        wt_root = Path(self.cfg.get("worktrees") or (self.state / "wt")).expanduser()
        wt_root.mkdir(parents=True, exist_ok=True)
        wt = wt_root / tid

        if wt.exists():
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)

        verify_only = self.mode == "verify"
        if verify_only and not (card.get("verify") or []):
            return self._fail(tid, card, None, "verify 模式的任务卡必须至少有一条 verify 命令")

        base = f"origin/{card.get('base') or 'main'}"
        branch = f"task/{tid}"
        if verify_only:
            branch = f"verify/{tid}"  # 只读检出，不产出代码分支
        elif not card.get("fresh") and rev_parse(repo, f"origin/task/{tid}"):
            base = f"origin/task/{tid}"  # 续跑上次的活；retry --fresh 时从 main 重来
        try:
            git(["worktree", "add", "-B", branch, str(wt), base], repo)
        except HiveError as e:
            return self._fail(tid, card, None, f"创建 worktree 失败: {e}")

        # .hive/ 只给 agent 和 worker 用，不进 git
        ex = git(["rev-parse", "--git-path", "info/exclude"], wt).stdout.strip()
        ex_path = (wt / ex).resolve() if not os.path.isabs(ex) else Path(ex)
        try:
            old = ex_path.read_text(encoding="utf-8") if ex_path.exists() else ""
            if ".hive/" not in old:
                ex_path.write_text(old + "\n.hive/\n", encoding="utf-8")
        except OSError:
            pass

        (wt / ".hive").mkdir(exist_ok=True)
        prompt = PROMPT_HEADER.format(tid=tid) + self._render_card(card, wt)
        (wt / ".hive" / "task.md").write_text(prompt, encoding="utf-8")
        summary_file = wt / ".hive" / "summary.md"

        logfile = self.state / "logs" / f"{tid}.log"
        out, timed_out, code = "", False, 0

        with self.lease_heartbeat(tid):
            if verify_only:
                log(f"{tid} 验证模式：只跑 verify，不调用 agent")
            elif self.manual:
                if os.name == "nt" and self.cfg.get("copy_prompt", True):
                    try:
                        subprocess.run("clip", input=prompt, text=True, shell=True,
                                       check=False, capture_output=True)
                        log("任务提示词已复制到剪贴板")
                    except OSError:
                        pass
                if self.launch:
                    cmdline = (self.launch.replace("{worktree}", str(wt))
                               .replace("{task}", tid)
                               .replace("{prompt}", str(wt / ".hive" / "task.md")))
                    try:
                        subprocess.Popen(cmdline, shell=True, cwd=str(wt))
                        log(f"已启动 GUI：{cmdline}")
                    except OSError as e:
                        log(f"启动 GUI 失败（继续手动）: {e}")
                log(f"{tid} 手动模式：请在 {wt} 里让 GUI agent 完成，然后写 {summary_file}")
                print(f"\n=== 任务 {tid} 已就绪 ===\n工作目录: {wt}\n任务卡:   {wt/'.hive'/'task.md'}"
                      f"\n完成后写: {summary_file}（或在此按 Ctrl-C 放弃）\n", flush=True)
                deadline = time.time() + int(card.get("timeout_minutes") or 120) * 60
                while time.time() < deadline:
                    if summary_file.exists() and summary_file.stat().st_size > 0:
                        break
                    time.sleep(5)
                else:
                    timed_out = True
            else:
                code, out, timed_out = self._exec(self.cmd, wt, prompt, logfile,
                                                  int(card.get("timeout_minutes") or 60) * 60, tid)

        # 越界检查：自举时代码仓和协调仓库是同一个，agent 能顺手改掉自己的任务卡
        changed: list[str] = changed_files(wt)
        oop: list[str] = []
        if not verify_only:
            oop = out_of_paths(card.get("paths"), changed)
            if oop:
                log(f"{tid} 越界改动 {len(oop)} 个文件（paths 声明的是 "
                    f"{', '.join(card.get('paths') or []) or '未声明'}）："
                    f"{', '.join(oop[:8])}")

        # 提交 + push（agent 自己 commit 过也没关系）；验证模式不产出代码
        head = None
        blocked = oop and strict_paths(self.cfg)
        if blocked:
            log(f"{tid} 严格模式：有越界改动，不提交不验收，直接判失败")
        elif verify_only:
            head = rev_parse(wt, "HEAD")
        else:
            try:
                git(["add", "-A"], wt)
                if git(["diff", "--cached", "--quiet"], wt, check=False).returncode != 0:
                    git([*IDENT, "commit", "-m", f"hive({tid}): {card.get('title', '')}"], wt)
                head = rev_parse(wt, "HEAD")
                git_net(["push", "--force", "origin",
                         f"HEAD:refs/heads/task/{tid}"], wt)
            except HiveError as e:
                log(f"{tid} 推送代码分支失败: {e}")

        verify = []
        if not blocked:
            for cmd in (card.get("verify") or []):
                p = run(cmd, cwd=wt, check=False, shell=True, timeout=1800)
                verify.append({"cmd": cmd, "code": p.returncode,
                               "tail": (p.stdout + p.stderr).strip().splitlines()[-5:]})
                log(f"{tid} 验证 {p.returncode}: {cmd}")

        ok = (not blocked and code == 0 and not timed_out
              and all(v["code"] == 0 for v in verify))
        if blocked:
            error = "越界改动（严格模式）：" + ", ".join(oop[:8])
        elif not ok:
            # 顺序很重要：verify 挂掉时 agent 的退出码往往是 0，
            # 写「agent 退出码 0」既误导人，也让 digest 的失败归因全跑偏
            bad = [str(v.get("cmd")) for v in verify if v.get("code")]
            if timed_out:
                error = "超时"
            elif bad:
                error = "verify 未通过: " + " | ".join(bad[:2])
            else:
                error = f"agent 退出码 {code}"
        else:
            error = ""
        if summary_file.exists() and summary_file.stat().st_size > 0:
            summary = summary_file.read_text(encoding="utf-8").strip()
        elif verify_only:
            summary = "verify-only 模式（无 agent 参与）\n" + "\n".join(
                f"$ {v['cmd']}  ->  {v['code']}\n" + "\n".join(v["tail"]) for v in verify)
        else:
            summary = "\n".join(out.strip().splitlines()[-30:])

        result = {
            "task": tid, "agent": self.name, "status": "done" if ok else "failed",
            "branch": branch, "base": card.get("base") or "main", "mode": self.mode,
            "head": head, "exit_code": code, "timed_out": timed_out,
            "started_at": started, "finished_at": iso(),
            "duration_s": round(time.time() - t0, 1),
            "verify": verify, "summary": summary, "error": error,
            "declared_paths": card.get("paths") or [],
            "changed_files": changed, "out_of_paths": oop,
        }
        self._write_result(tid, result)

        if not self.keep:
            git(["worktree", "remove", "--force", str(wt)], repo, check=False)
            shutil.rmtree(wt, ignore_errors=True)

        (self.state / "done" / tid).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        log(f"{tid} 完成: {result['status']}（{result['duration_s']}s）")
        return result

    def _render_card(self, card: dict, wt) -> str:
        lines = [f"- id: {card['id']}",
                 f"- 工作目录（宿主机和 chroot 内是同一个路径）: {wt}",
                 f"- 标题: {card.get('title', '')}",
                 f"- 仓库: {card.get('repo')}（基线 {card.get('base', 'main')}）",
                 f"- 允许修改: {', '.join(card.get('paths') or []) or '（未声明，尽量最小改动）'}"]
        if card.get("depends_on"):
            lines.append(f"- 依赖: {', '.join(card['depends_on'])}")
        if card.get("verify"):
            lines.append("- 验收命令（worker 会跑，必须全绿）:")
            lines += [f"    {c}" for c in card["verify"]]
        return "\n".join(lines) + "\n\n## 任务说明\n\n" + card.get("_body", "").strip() + "\n"

    def _exec(self, cmd: str, cwd: Path, prompt: str, logfile: Path, timeout_s: int, tid: str):
        env = os.environ.copy()
        env.update({
            "HIVE_TASK": tid, "HIVE_AGENT": self.name,
            "HIVE_PROMPT": str(cwd / ".hive" / "task.md"),
            "HIVE_WORKTREE": str(cwd),
        })
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        p = subprocess.Popen(
            cmd, shell=True, cwd=str(cwd), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, **kwargs,
        )
        timed_out = False
        try:
            out, _ = p.communicate(prompt, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
            else:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            out, _ = p.communicate()
        logfile.write_text(out or "", encoding="utf-8")
        return p.returncode or 0, out or "", timed_out

    def _fail(self, tid: str, card: dict, wt, error: str) -> dict:
        result = {
            "task": tid, "agent": self.name, "status": "failed", "branch": f"task/{tid}",
            "base": card.get("base") or "main", "head": None, "exit_code": 1,
            "timed_out": False, "started_at": iso(), "finished_at": iso(),
            "duration_s": 0, "verify": [], "summary": error, "error": error,
        }
        self._write_result(tid, result)
        (self.state / "done" / tid).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        log(f"{tid} 失败: {error}")
        return result

    def _write_result(self, tid: str, result: dict) -> None:
        commit_file_to_ref(
            self.dir, f"results/{tid}.json",
            json.dumps(result, ensure_ascii=False, indent=2),
            f"res/{tid}", "origin/main",
            f"result {tid}: {result['status']} by {self.name}", force=True,
        )

    # -- 主循环 ------------------------------------------------------------ #
    def loop(self, once=False, interval=30) -> None:
        log(f"worker {self.name} 启动（{self.dir}），轮询间隔 {interval}s")
        while True:
            try:
                # 同 __init__：轮询也不能裸 reset，否则常驻 worker 每隔 interval 秒
                # 就把主工作区里未提交的改动抹一次。落后+脏时 sync_main 抛 HiveError，
                # 下面本来就 catch 住并继续下一轮，不会崩。
                self.h.sync_main()
                pending = self.pending()
                claimed: list[str] = []
                for tid, _card in pending:
                    if self.claim(tid):
                        claimed.append(tid)
                if claimed:
                    # 一轮里可领的卡一次性丢进池子，按 max_concurrency 并发跑；
                    # 上限为 1 时 run_tasks 退化回逐张串行，行为与旧循环一致。
                    self.run_tasks(claimed)
                if once:
                    if not pending:
                        log("没有待办任务")
                    return
            except KeyboardInterrupt:
                log("收到中断，退出")
                return
            except HiveError as e:
                log(f"循环出错（继续）: {e}")
            time.sleep(interval)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_init(args) -> None:
    d = Path(args.dir).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").exists():
        git(["init", "-b", "main"], d)
    for sub in ("tasks", "inbox", "results", "agents"):
        (d / sub).mkdir(exist_ok=True)
        (d / sub / ".gitkeep").touch()
    # 工具随仓库走：每台机器 clone 下来就能 python3 hive.py
    tool = d / "hive.py"
    if not tool.exists():
        shutil.copy2(Path(__file__).resolve(), tool)
        os.chmod(tool, 0o755)
    readme = d / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Hive 协同仓库\n\n"
            "这个仓库是任务总线，不放代码。\n\n"
            "- `tasks/T-*.md`：任务卡（总控写）\n"
            "- `inbox/<agent>/`：分派信号（总控写）\n"
            "- `results/T-*.json`：结果（worker 写，在 `res/<task>` 分支）\n"
            "- `claims/<task>.json`：租约（worker 写，在 `claim/<task>` 分支）\n"
            "- `agents/<agent>.json`：机器/agent 配置\n\n"
            "看板：`python3 hive.py status`\n",
            encoding="utf-8",
        )
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*.log\n.hive/\n__pycache__/\n", encoding="utf-8")
    git(["add", "-A"], d)
    if git(["diff", "--cached", "--quiet"], d, check=False).returncode != 0:
        git([*IDENT, "commit", "-m", "hive: init"], d)
    if args.remote:
        if not git_ok(["remote", "get-url", "origin"], d):
            git(["remote", "add", "origin", args.remote], d)
        git_net(["push", "-u", "origin", "main"], d)
    log(f"协调仓库就绪: {d}")


def cmd_agent(args) -> None:
    h = Hive(args.dir)
    if args.action == "list":
        for p in sorted((h.dir / "agents").glob("*.json")):
            cfg = json.loads(p.read_text(encoding="utf-8"))
            print(f"{cfg.get('name'):<16} host={cfg.get('host') or '-':<8} "
                  f"abilities={cfg.get('abilities', [])} "
                  f"caps={cfg.get('capabilities', [])} "
                  f"{'manual' if cfg.get('manual') else cfg.get('command', '')[:60]}")
        return
    if not args.name:
        raise HiveError("agent add 需要 --name")
    repos = {}
    for item in args.repo or []:
        if "=" not in item:
            raise HiveError(f"--repo 需要 name=path 形式: {item}")
        k, v = item.split("=", 1)
        repos[k] = v
    mode = args.mode or ("manual" if args.manual else "exec")
    cfg = {
        "name": args.name,
        "host": (args.host or current_host()).strip().lower(),
        "command": args.command or "",
        "mode": mode,
        "manual": mode == "manual",
        "abilities": args.abilities or [],
        "capabilities": args.capabilities or [],
        "repos": repos,
        "launch": args.launch or "",
        "worktrees": args.worktrees or "",
        "max_concurrency": args.max_concurrency or 1,
        "created": iso(),
    }
    p = h.dir / "agents" / f"{args.name}.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    h.commit_and_push(f"agent: register {args.name}",
                      [f"agents/{args.name}.json"])
    log(f"已登记 {args.name}")


def cmd_task(args) -> None:
    h = Hive(args.dir)
    if args.action == "list":
        for tid, c in h.cards().items():
            print(f"{tid} {c.get('state'):<10} {c.get('owner') or '-':<12} {c.get('title')}")
        return
    if not args.title or not args.repo:
        raise HiveError("task new 需要 --title 和 --repo")
    ids = [int(p.stem.split("-")[1]) for p in (h.dir / "tasks").glob("T-*.md")
           if p.stem.split("-")[1].isdigit()]
    tid = args.id or f"T-{max(ids, default=0) + 1:04d}"
    if (h.dir / "tasks" / f"{tid}.md").exists():
        raise HiveError(f"{tid} 已存在")
    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body:
        body = args.body
    card = {
        "id": tid, "title": args.title, "repo": args.repo, "base": args.base,
        "state": "ready", "owner": None, "agent": None, "priority": args.priority,
        "needs": args.needs or [], "paths": args.paths or [], "verify": args.verify or [],
        "depends_on": args.depends or [], "fresh": False, "lease_minutes": args.lease,
        "timeout_minutes": args.timeout, "created": iso(),
        "max_retries": args.max_retries,
    }
    write_card(h.dir / "tasks" / f"{tid}.md", card, body or f"# {args.title}\n")
    h.commit_and_push(f"task: add {tid} {args.title}", [f"tasks/{tid}.md"])
    log(f"已创建 {tid}")


DRAFT_TEMPLATE = """---
id: {tid}
title: （一句话说清要做什么）
repo: {repo}
base: main
state: draft
owner: null
agent: null
priority: P1
needs: []
paths: []
verify: []
depends_on: []
lease_minutes: 180
timeout_minutes: 120
created: {now}
---

> 第一次接 hive？先读 `docs/AGENT快速上手.md`（agent 视角的接入指南）。

## 背景

（为什么需要做这件事？现在卡在哪？引用具体现象，别写「为了更好」这种空话。）

## 目标

（要改哪些文件、加什么能力、验收标准是什么。能列点就列点。）

## 红线

（不许动什么、不许引入什么、不许改什么行为。）

## 验收

（`git show --stat HEAD` 应该覆盖哪些路径？verify 全过之外还有什么？）
"""


def draft_lint(card: dict, tid: str, agents: dict, cards: dict) -> tuple[list, list]:
    """校验一张草稿卡。返回 (errors, warnings)。

    error 会挡住转正；warning 只是提醒。判定规则写在这里，
    让 `hive doctor` 和 `hive draft adopt` 共用同一套。
    """
    errors: list[str] = []
    warns: list[str] = []
    repo = card.get("repo") or ""
    if not repo:
        errors.append("没填 repo")
    elif not any(repo in (a.get("repos") or {}) for a in agents.values()):
        errors.append(f"repo {repo!r} 没有任何 agent 登记 —— 这张卡永远派不出去")
    if not (card.get("verify") or []):
        errors.append("一条 verify 都没有（没有验收机的卡不能转正）")
    else:
        v = _verify_verdict(card.get("verify") or [], with_codes=False)
        if not v["has_strong"]:
            errors.append(
                "没有强验收：全是 grep/test -f 这类只查痕迹的命令，"
                "agent 靠改个字符串就能糊弄过去（加一条 pytest 之类的）")
    if not (card.get("paths") or []):
        warns.append("没声明 paths，改动范围不受约束")
    needs = card.get("needs") or []
    have = set()
    for a in agents.values():
        have |= set(a.get("abilities") or []) | set(a.get("capabilities") or [])
    lack = [n for n in needs if n not in have]
    if lack:
        errors.append(f"能力 {', '.join(lack)} 没有任何 agent 满足 —— 会变成僵尸卡")
    for d in (card.get("depends_on") or []):
        if d not in cards:
            errors.append(f"依赖了不存在的卡 {d}")
    if not (card.get("title") or "").strip():
        errors.append("没填 title")
    return errors, warns


def _draft_name(file: str | None) -> str | None:
    """用户给 'T-0100' 或 'T-0100.md' 都认，统一补上 .md。"""
    if not file:
        return None
    return file if file.endswith(".md") else file + ".md"


def cmd_draft(args) -> None:
    h = Hive(args.dir)
    h.sync_main()
    ddir = h.dir / "drafts"
    args.file = _draft_name(args.file)
    if args.action == "list":
        files = sorted(ddir.glob("*.md"))
        if not files:
            print("（没有草稿）")
            return
        for p in files:
            data, _ = parse_card(p)
            print(f"{p.name:<28} {data.get('title') or '(未填标题)'}")
        return
    if args.action == "lint":
        agents = h.agents()
        cards = h.cards()
        files = [ddir / args.file] if args.file else sorted(ddir.glob("*.md"))
        bad = 0
        for p in files:
            if not p.exists():
                raise HiveError(f"草稿不存在：{p}")
            data, _ = parse_card(p)
            errors, warns = draft_lint(data, data.get("id") or p.stem, agents, cards)
            for w in warns:
                print(f"[WARN] {p.name}: {w}")
            for e in errors:
                print(f"[ERR ] {p.name}: {e}")
            if errors:
                bad += 1
            else:
                print(f"[ OK ] {p.name}")
        if bad:
            sys.exit(1)
        return
    if args.action == "adopt":
        src = ddir / args.file
        if not src.exists():
            raise HiveError(f"草稿不存在：{src}")
        data, body = parse_card(src)
        errors, warns = draft_lint(data, data.get("id") or src.stem,
                                   h.agents(), h.cards())
        if errors:
            for e in errors:
                print(f"[ERR ] {args.file}: {e}", file=sys.stderr)
            raise HiveError("草稿有问题，不转正（先改 drafts/ 里的内容再试）")
        for w in warns:
            log(f"提醒：{w}")
        ids = [int(p.stem.split("-")[1]) for p in (h.dir / "tasks").glob("T-*.md")
               if p.stem.split("-")[1].isdigit()]
        tid = args.id or f"T-{max(ids, default=0) + 1:04d}"
        if (h.dir / "tasks" / f"{tid}.md").exists():
            raise HiveError(f"{tid} 已存在")
        card = {
            "id": tid, "title": data.get("title"), "repo": data.get("repo"),
            "base": data.get("base") or "main", "state": "ready", "owner": None,
            "agent": None, "priority": data.get("priority") or "P1",
            "needs": data.get("needs") or [], "paths": data.get("paths") or [],
            "verify": data.get("verify") or [], "depends_on": data.get("depends_on") or [],
            "fresh": False, "lease_minutes": int(data.get("lease_minutes") or 180),
            "timeout_minutes": int(data.get("timeout_minutes") or 120),
            "created": iso(),
            "max_retries": int(data.get("max_retries") or DEFAULT_MAX_RETRIES),
        }
        write_card(h.dir / "tasks" / f"{tid}.md", card, body)
        src.unlink()  # 草稿转正后撤掉，别在 drafts/ 里留两份
        h.commit_and_push(f"adopt draft {args.file} -> {tid} {card['title']}",
                          [f"tasks/{tid}.md", f"drafts/{args.file}"])
        log(f"草稿 {args.file} 已转为 {tid}")
        print(f"\n=== {tid} 已入队 ===\n下一步：hive dispatch 或 "
              f"hive assign {tid} --agent <name>\n", flush=True)
        return
    # new
    repo = args.repo or "hive"
    ids = [int(p.stem.split("-")[1]) for p in (h.dir / "tasks").glob("T-*.md")
           if p.stem.split("-")[1].isdigit()]
    tid = f"T-{max(ids, default=0) + 1:04d}"
    name = args.file or f"{tid}.md"
    if (ddir / name).exists():
        raise HiveError(f"草稿 {name} 已存在")
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / name).write_text(
        DRAFT_TEMPLATE.format(tid=tid, repo=repo, now=iso()), encoding="utf-8")
    h.commit_and_push(f"draft: add {name}", [f"drafts/{name}"])
    log(f"草稿已生成：drafts/{name}")


def cmd_assign(args) -> None:
    Hive(args.dir).cmd_assign(args.task, args.agent)


def cmd_take(args) -> None:
    Hive(args.dir).cmd_take(args.task, args.agent)


def cmd_submit(args) -> None:
    Hive(args.dir).cmd_submit(args.task, args.keep, args.note or "")


def cmd_dispatch(args) -> None:
    Hive(args.dir).cmd_dispatch(
        apply=args.apply, limit=args.limit, only_agent=args.agent,
        as_json=args.json, max_per_repo=args.max_per_repo,
        dry_run=args.dry_run, only_host=args.host)


def cmd_mcp(args) -> int:
    """启动 MCP server（stdio）。转发给 mcp_server.py，保持 CLI 一个入口。"""
    import mcp_server
    argv = ["--agent", args.agent, "--role", args.role]
    if args.mcp_dir:
        argv += ["--dir", args.mcp_dir]
    if args.source:
        argv += ["--source", args.source]
    return mcp_server.main(argv)


def cmd_doctor(args) -> None:
    if not Hive(args.dir).cmd_doctor():
        sys.exit(1)


def cmd_digest(args) -> None:
    Hive(args.dir).cmd_digest(days=args.days, out=args.out or "",
                              as_json=args.json)


def cmd_stats(args) -> None:
    Hive(args.dir).cmd_stats(args.json, args.days)


def cmd_block(args) -> None:
    Hive(args.dir).cmd_block(args.task, args.reason or "", who="controller")


def cmd_unblock(args) -> None:
    Hive(args.dir).cmd_unblock(args.task, args.reason or "", who="controller")


def cmd_close(args) -> None:
    Hive(args.dir).cmd_close(args.task, args.note or "")


def cmd_land(args) -> None:
    Hive(args.dir).cmd_land(
        tid=getattr(args, "task", None), apply=args.apply,
        note=args.note or "", all_tasks=args.all, as_json=args.json)


def cmd_status(args) -> None:
    Hive(args.dir).cmd_status(args.json)


def cmd_gc(args) -> None:
    Hive(args.dir).cmd_gc(args.minutes)


def cmd_watchdog(args) -> None:
    Hive(args.dir).cmd_watchdog(minutes=args.minutes, apply=args.apply,
                                as_json=args.json, orphan_grace=args.orphan_grace)


def cmd_orchestrate(args) -> None:
    Hive(args.dir).cmd_orchestrate(
        interval=args.interval, once=args.once,
        iterations=args.iterations,
        dispatch=not args.no_dispatch, watchdog=not args.no_watchdog,
        land=not args.no_land, doctor=not args.no_doctor,
        dispatch_limit=args.dispatch_limit, max_land=args.max_land,
        watchdog_minutes=args.watchdog_minutes, orphan_grace=args.orphan_grace,
        stop_file=args.stop_file, as_json=args.json)


def cmd_llm(args) -> None:
    from llm_controller import run_llm
    run_llm(args)


def cmd_audit(args) -> None:
    Hive(args.dir).cmd_audit(tid=args.task, all_cards=args.all, as_json=args.json)


def cmd_prune(args) -> None:
    Hive(args.dir).cmd_prune(args.repo, args.apply)


def cmd_retry(args) -> None:
    Hive(args.dir).cmd_retry(args.task, args.agent, args.fresh)


def cmd_result(args) -> None:
    h = Hive(args.dir)
    h.fetch()
    res = h.result_info(args.task)
    if not res:
        raise HiveError(f"{args.task} 还没有结果")
    verdict = _verify_verdict(res.get("verify") or [])
    res["verify_strength"] = verdict["verdict"]
    res["verify_entries"] = verdict["verify"]
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"{res['task']}  {res['status']}  by {res['agent']}  ({res['duration_s']}s)")
        print(f"branch: {res.get('branch')}  head: {res.get('head')}")
        print(f"验收强度: {verdict['verdict']}")
        for e in verdict["verify"]:
            print(f"  [{e['code']}] ({e['strength']}) {e['cmd']}")
        if verdict["verdict"] == "weak-pass":
            print("  ! 只有弱验收（grep/test -f 之类），成果没有被真正执行验证")
        print("-" * 60)
        print(res.get("summary", ""))


def cmd_claim(args) -> None:
    w = Worker(args.dir, args.agent)
    okc = w.claim(args.task)
    if okc:
        # CLI 的 claim 只抢租约、不写卡面 owner；而 submit 要求 owner 存在。
        # MCP 的 hive_claim 已在 T-0147 改成「先抢锁后 assign」，CLI 这条路还没跟上，
        # 先给出明确提示，免得领完卡提交时报「没有 owner」一头雾水（T-0160 实测踩到）。
        log(f"{args.task} 租约已到手。注意：CLI claim 只抢锁不写 owner，"
            f"提交前需 `hive assign {args.task} --agent {args.agent}`；"
            f"或直接用 `hive take {args.task} --agent {args.agent}`（它会顺带备好 worktree）")
    sys.exit(0 if okc else 1)


def cmd_work(args) -> None:
    w = Worker(args.dir, args.agent, keep_worktree=args.keep, hive_state=args.state_dir,
               max_concurrency=args.max_concurrency)
    w.loop(once=args.once, interval=args.interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hive", description="多机 AI Agent 协同任务总线")
    p.add_argument("--dir", default=os.environ.get("HIVE_DIR", "."),
                   help="协调仓库路径（默认 $HIVE_DIR 或当前目录）")
    p.add_argument("-V", "--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="初始化协调仓库")
    s.add_argument("--remote", help="git 远端 URL")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("agent", help="登记 / 查看 agent")
    s.add_argument("action", choices=["add", "list"])
    s.add_argument("--name")
    s.add_argument("--command", help="执行命令（prompt 从 stdin 传入）")
    s.add_argument("--manual", action="store_true", help="GUI 半自动模式（等价 --mode manual）")
    s.add_argument("--mode", choices=["exec", "verify", "manual"],
                   help="exec=调用 agent；verify=只跑验收；manual=等人")
    s.add_argument("--abilities", nargs="*",
                   help="能力注册表，如 code ci docs verify")
    s.add_argument("--capabilities", nargs="*")
    s.add_argument("--repo", action="append", metavar="NAME=PATH")
    s.add_argument("--worktrees", help="worktree 根目录")
    s.add_argument("--launch", help="manual 模式下打开 GUI 的命令，支持 {worktree}/{task}/{prompt}")
    s.add_argument("--max-concurrency", type=int, default=1)
    s.add_argument("--host",
                   help="agent 所在机器名（多机协作用来路由；默认取当前 hostname）")
    s.set_defaults(func=cmd_agent)

    s = sub.add_parser("draft", help="任务卡草稿：agent 出题、人确认后转正")
    s.add_argument("action", choices=["new", "list", "lint", "adopt"])
    s.add_argument("file", nargs="?", help="草稿文件名（drafts/<file>）")
    s.add_argument("--repo", default="hive", help="草稿针对哪个代码仓（默认 hive）")
    s.add_argument("--id", help="转正时指定任务号（默认取当前最大号 +1）")
    s.set_defaults(func=cmd_draft)

    s = sub.add_parser("task", help="任务卡")
    s.add_argument("action", choices=["new", "list"])
    s.add_argument("--id")
    s.add_argument("--title")
    s.add_argument("--repo", required=False)
    s.add_argument("--base", default="main")
    s.add_argument("--priority", default="P1")
    s.add_argument("--needs", nargs="*", help="能力标签，如 windows exe")
    s.add_argument("--paths", nargs="*")
    s.add_argument("--verify", action="append")
    s.add_argument("--depends", nargs="*")
    s.add_argument("--lease", type=int, default=180)
    s.add_argument("--timeout", type=int, default=60)
    s.add_argument("--max-retries", dest="max_retries", type=int,
                   default=DEFAULT_MAX_RETRIES,
                   help="看门狗最多自动回收几次；用尽转 blocked 不再无限重派"
                        f"（默认 {DEFAULT_MAX_RETRIES}，0=一次都不自动重派）")
    s.add_argument("--body")
    s.add_argument("--body-file")
    s.set_defaults(func=cmd_task)

    s = sub.add_parser("take", help="手工领取并准备 worktree（不启动 worker）")
    s.add_argument("task")
    s.add_argument("--agent", required=True)
    s.set_defaults(func=cmd_take)

    s = sub.add_parser("submit", help="手工提交：commit/push/verify/result")
    s.add_argument("task")
    s.add_argument("--keep", action="store_true", help="保留 worktree")
    s.add_argument("--note", help="没有 summary.md 时的说明")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("dispatch", help="自动分派（能力/负载/依赖/paths 冲突感知）")
    s.add_argument("--apply", action="store_true", help="真正写入；默认 dry-run")
    s.add_argument("--dry-run", action="store_true",
                   help="只输出派/不派原因（默认行为）")
    s.add_argument("--limit", type=int, help="最多分派几张")
    s.add_argument("--agent", help="只分派给这个 agent")
    s.add_argument("--host", help="只分派给这台机器上的 agent（按 agent 配置的 host 字段）")
    s.add_argument("--max-per-repo", "--same-repo-limit", dest="max_per_repo",
                   type=int, default=None,
                   help="同一 repo 里 paths 互相冲突的卡同时在飞的上限"
                        "（默认 1，也可用 HIVE_MAX_PER_REPO；paths 不冲突的卡不受此限）")
                   
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_dispatch)

    s = sub.add_parser("doctor", help="检查协调仓库/任务/租约/结果")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("digest", help="复盘摘要：失败归因 + 慢卡 + 反复重跑的卡（只读）")
    s.add_argument("--days", type=int, default=7, help="只看最近 N 天（默认 7，0=全部）")
    s.add_argument("--out", help="把 markdown 摘要写到这个文件（比如 docs/lessons.md）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("stats", help="吞吐与成功率统计")
    s.add_argument("--json", action="store_true")
    s.add_argument("--days", type=int, default=0, help="只看最近 N 天完成的结果")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("assign", help="分派任务")
    s.add_argument("task")
    s.add_argument("--agent", required=True)
    s.set_defaults(func=cmd_assign)

    s = sub.add_parser("close", help="关闭任务")
    s.add_argument("task")
    s.add_argument("--note")
    s.set_defaults(func=cmd_close)

    s = sub.add_parser("land", help="把已完成的任务分支合入代码仓 main 并 close")
    s.add_argument("task", nargs="?", help="任务号；配合下面 --all 时可不给")
    s.add_argument("--all", action="store_true", help="合入所有满足条件（依赖在前）的卡")
    s.add_argument("--apply", action="store_true",
                   help="真的推 main；默认只在临时 worktree 里预演")
    s.add_argument("--json", action="store_true")
    s.add_argument("--note", help="close 时的完成备注")
    s.set_defaults(func=cmd_land)

    s = sub.add_parser("audit", help="审计：比对 agent 的 summary 与真实 diff（合并前看一眼）")
    s.add_argument("task", nargs="?", help="任务号；不给就加 --all")
    s.add_argument("--all", action="store_true", help="审所有已有结果的卡")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("block", help="挂起一张卡（等资源/等外部条件，dispatch 与 worker 都会跳过）")
    s.add_argument("task")
    s.add_argument("--reason", default="", help="为什么挂起（写进完成备注）")
    s.set_defaults(func=cmd_block)

    s = sub.add_parser("unblock", help="解除挂起，回到 ready 或 assigned")
    s.add_argument("task")
    s.add_argument("--reason", default="")
    s.set_defaults(func=cmd_unblock)

    s = sub.add_parser("status", help="看板")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("retry", help="重试任务（清掉结果与租约）")
    s.add_argument("task")
    s.add_argument("--agent")
    s.add_argument("--fresh", action="store_true", help="从 main 重来，不续用 task 分支")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("result", help="查看任务结果")
    s.add_argument("task")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_result)

    s = sub.add_parser("gc", help="回收过期租约")
    s.add_argument("--minutes", type=int, default=180)
    s.set_defaults(func=cmd_gc)

    s = sub.add_parser("watchdog",
                       help="看门狗：回收死掉/孤儿/僵尸的 in-flight 卡（默认 dry-run）")
    s.add_argument("--minutes", type=int, default=0,
                   help="租约过期超过这么久（分钟）才回收，默认 0（立刻）")
    s.add_argument("--orphan-grace", dest="orphan_grace", type=int, default=30,
                   help="assigned 但一直没 claim 超过这么久（分钟）才回收，默认 30（刚分派的卡在 grace 内不回收）")
    s.add_argument("--apply", action="store_true", help="真回收；默认只报告")
    s.add_argument("--json", action="store_true", help="输出结构化 JSON")
    s.set_defaults(func=cmd_watchdog)

    s = sub.add_parser("orchestrate",
                       help="总控进程：循环跑 看门狗→分派→合入→体检，替代人工 drive（默认常驻）")
    s.add_argument("--interval", type=int, default=60, help="每轮间隔秒数，默认 60")
    s.add_argument("--once", action="store_true", help="只跑一轮就退")
    s.add_argument("--iterations", type=int, default=None, help="跑满 N 轮就退")
    s.add_argument("--no-dispatch", action="store_true", help="关闭自动分派阶段")
    s.add_argument("--no-land", action="store_true", help="关闭自动合入阶段")
    s.add_argument("--no-watchdog", action="store_true", help="关闭看门狗回收阶段")
    s.add_argument("--no-doctor", action="store_true", help="关闭体检阶段")
    s.add_argument("--dispatch-limit", dest="dispatch_limit", type=int, default=None,
                   help="每轮最多分派几张（默认不限制）")
    s.add_argument("--max-land", dest="max_land", type=int, default=None,
                   help="每轮最多合入几张（默认不限制）")
    s.add_argument("--watchdog-minutes", dest="watchdog_minutes", type=int, default=180,
                   help="看门狗判定 stale 的租约过期阈值（分钟），默认 180")
    s.add_argument("--orphan-grace", dest="orphan_grace", type=int, default=45,
                   help="看门狗回收孤儿的宽限（分钟），默认 45（刚分派的卡不会被误杀）")
    s.add_argument("--stop-file", dest="stop_file", default=None,
                   help="每轮开始检查该文件，存在就优雅退出")
    s.add_argument("--json", action="store_true", help="每轮打印 tick 摘要 JSON")
    s.set_defaults(func=cmd_orchestrate)

    s = sub.add_parser("llm", help="LLM 总控：用 controller MCP 工具面让 LLM 逐轮决策")
    s.add_argument("--agent", default="controller", help="总控 agent 名（默认 controller）")
    s.add_argument("--mcp", action="store_true",
                   help="走真正的 MCP stdio 客户端（spawn mcp_server.py），默认 in-process 复用")
    s.add_argument("--llm", choices=["openai", "heuristic"], default="heuristic",
                   help="决策后端：openai=真 LLM；heuristic=无 key 回退（默认）")
    s.add_argument("--model", default=None, help="覆盖 ${OPENAI_MODEL}")
    s.add_argument("--base-url", default=None, help="覆盖 ${OPENAI_BASE_URL}")
    s.add_argument("--api-key", default=None, help="覆盖 ${OPENAI_API_KEY}")
    s.add_argument("--apply", action="store_true", help="真改 main（默认只预演）")
    s.add_argument("--max-iter", type=int, default=12, help="最多决策几轮")
    s.add_argument("--stop-file", dest="stop_file", default=None,
                   help="出现该文件就停止循环")
    s.add_argument("--once", action="store_true", help="只跑一轮决策就退")
    s.set_defaults(func=cmd_llm)

    s = sub.add_parser("prune", help="清理代码仓里已合并的 task/verify 分支（默认 dry-run）")
    s.add_argument("--repo", help="只清这个代码仓（默认：agent 登记的所有）")
    s.add_argument("--apply", action="store_true", help="真删；默认只列计划")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("claim", help="手工领取")
    s.add_argument("task")
    s.add_argument("--agent", required=True)
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("mcp", help="启动 MCP server（stdio），让 agent 通过 MCP 查/领/交/终止任务")
    s.add_argument("--agent", default=os.environ.get("HIVE_AGENT", ""),
                   help="以哪个 agent 的身份运行（默认 $HIVE_AGENT）")
    s.add_argument("--dir", dest="mcp_dir", default=None,
                   help="协调仓库路径；不给就用 ~/.hive/clones/mcp-<agent>（独占 clone）")
    s.add_argument("--source", help="自动建 clone 时从哪拷（默认 hive 工具仓本体）")
    s.add_argument("--role", default=os.environ.get("HIVE_ROLE", "worker"),
                   choices=["worker", "controller"],
                   help="worker=只能操作自己的卡；controller=总控，会改动 main")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("work", help="worker 主循环")
    s.add_argument("--agent", required=True)
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=int, default=30)
    s.add_argument("--keep", action="store_true", help="保留 worktree 便于排查")
    s.add_argument("--state-dir")
    s.add_argument("--max-concurrency", type=int, default=None,
                   help="worker 内并行跑卡的上限（默认取 agent 配置 max_concurrency）")
    s.set_defaults(func=cmd_work)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc = args.func(args)
        # mcp 这类长驻子命令会自己返回退出码，其余子命令返回 None 当 0
        return rc if isinstance(rc, int) else 0
    except HiveError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # 例如 `hive status | head`：安静退出，别打 traceback
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
