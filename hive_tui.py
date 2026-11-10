#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive top —— 纯标准库的常驻终端视图（只读）。

为什么不用 curses
----------------
Windows 上的 `curses` 不在标准库里（要额外装 windows-curses），而这个视图恰恰
是给 Windows 那几台机器用的。ANSI 转义 + `msvcrt` 就够了，零依赖也就零装包。

三条设计约束（来自 T-0218）
--------------------------
1. **只读**：不领卡、不改卡、不写任何文件；`q`/`Esc`/`Ctrl-C` 干净退出。
2. **可断言**：`--once` 打一帧就退出，退出码恒 0（数据源全挂也不能抛栈），
   这样测试不用模拟终端、不用起线程。
3. **宽度自己算**：中文按双宽，超宽用 `…` 截断；每行都必须 ≤ `--width`。
   `len()` 在中文上会把一行 80 列显示成 40 字，靠它做裁剪必然溢出。

数据源
------
优先看板 `/api/status`（默认 `http://127.0.0.1:8787`），不可达时回落到本地协调
仓的内存快照（等价于 `hive status --json` 的行，只是不落盘、不打印）；两个都
拿不到就显示「无数据」——看板挂了不该让这个视图跟着崩。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.request

# 一屏最少 60 列：再窄连卡号和标题都摆不下，截断只会剩下省略号
WIDTH_MIN = 60
REFRESH_S = 2.0
API_TIMEOUT_S = 1.5
DASH_PORT_DEFAULT = 8787

GROUP_READY = "待领取"
GROUP_RUNNING = "进行中"
GROUP_MERGE = "待合入"
GROUP_BAD = "异常"
GROUP_SHELVED = "搁置"
# 搁置放在最后一组：不抢前四组的注意力，但计数常驻 —— 别忘了还有几张卡封着
GROUPS = (GROUP_READY, GROUP_RUNNING, GROUP_MERGE, GROUP_BAD, GROUP_SHELVED)

# 结果状态里算「出了事」的取值：失败/超时/被 fence 掉都进异常组
BAD_RESULTS = ("failed", "error", "timeout", "conflict")
BAD_STATES = ("failed", "error", "blocked", "conflict")
RUNNING_STATES = ("assigned", "running", "claimed", "taken", "dispatched")
READY_STATES = ("ready", "pending", "")


# --------------------------------------------------------------------------- #
# 宽度：中文双宽
# --------------------------------------------------------------------------- #
def char_width(ch: str) -> int:
    """单个字符的终端列宽。宽/全角算 2，组合符算 0，其余算 1。

    歧义宽度（A，如 `…`、`─`）按 1 算 —— 这是 Windows Terminal / xterm 的
    默认行为，也是本视图所有边框线的宽度前提。
    """
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    """整串的可见列宽（换行/制表符按 1 列算，渲染前会先压平）。"""
    return sum(char_width(ch) for ch in text)


def flat(text: object) -> str:
    """把任意值压成单行：标题里的换行会把一帧的布局整个打乱。"""
    return " ".join(str(text if text is not None else "").split())


def truncate(text: str, width: int) -> str:
    """按可见列宽截断，超宽补 `…`。返回值列宽 ≤ width。

    只压平换行、**不 strip 首尾空格** —— 卡片行靠行首缩进分组，这里一 strip
    缩进就没了（`flat()` 是给标题用的，不是给整行用的）。
    """
    width = int(width)
    if width <= 0:
        return ""
    text = str(text if text is not None else "").replace("\r", " ")
    text = text.replace("\n", " ").replace("\t", " ")
    if text_width(text) <= width:
        return text
    budget = width - 1                       # 留一列给省略号
    out: list[str] = []
    used = 0
    for ch in text:
        cw = char_width(ch)
        if used + cw > budget:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def fit(text: str, width: int) -> str:
    """一行的最终出口：只有经过这里，才能保证「每行 ≤ width」。"""
    return truncate(text, width)


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #
def _pick(row: dict) -> dict:
    """把看板行 / 本地行压成同一种结构，后面渲染只认这几个字段。"""
    return {
        "id": str(row.get("id") or ""),
        "title": flat(row.get("title") or ""),
        "state": str(row.get("state") or "").lower(),
        "owner": str(row.get("owner") or row.get("claim_agent") or ""),
        "result": str(row.get("result_status") or row.get("result") or "").lower(),
        "stale": bool(row.get("stale")),
        "fenced": bool(row.get("fenced")),
    }


def snapshot(rows, *, source: str | None, cluster: dict | None = None,
             age_s: float | None = None, error: str = "") -> dict:
    tasks = [_pick(r) for r in rows or [] if isinstance(r, dict) and r.get("id")]
    return {"source": source, "cluster": cluster, "age_s": age_s,
            "tasks": tasks, "error": error}


def classify(task: dict) -> str | None:
    """把一张卡放进四组之一；已完成/作废的卡不进任何组（返回 None）。

    顺序即优先级：一张「结果 failed 但还挂在 assigned」的卡，先算异常，
    而不是「进行中」——它已经不需要人等，而是需要人看。
    """
    state = task.get("state") or ""
    result = task.get("result") or ""
    # 搁置优先判：旧快照里可能还带着 failed 结果或过期租约，但搁置卡不该
    # 被算进「异常」——它不是出了事，是情况已知、暂时封存。
    if state == "shelved":
        return GROUP_SHELVED
    if task.get("stale") or task.get("fenced") or result in BAD_RESULTS:
        return GROUP_BAD
    if state in BAD_STATES:
        return GROUP_BAD
    if result == "done" and state not in ("done", "closed"):
        return GROUP_MERGE
    if state in RUNNING_STATES:
        return GROUP_RUNNING
    if state in READY_STATES:
        return GROUP_READY
    return None


def group_tasks(tasks) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {name: [] for name in GROUPS}
    for task in tasks or []:
        bucket = classify(task)
        if bucket:
            out[bucket].append(task)
    return out


# --------------------------------------------------------------------------- #
# 取数：HTTP 优先，本地回落
# --------------------------------------------------------------------------- #
def _get_json(url: str, timeout: float = API_TIMEOUT_S) -> tuple[dict | list, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "hive-top/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (本机/内网)
        head = {k.lower(): v for k, v in resp.headers.items()}
        raw = resp.read()
    return json.loads(raw.decode("utf-8-sig")), head


def _cluster_from_state(state: dict | None) -> dict | None:
    """把 /api/cluster 的 state 压成头部要的几个数。

    `nodes` 在旧版本里是 dict（id→记录），新版本是 list；`alive`/`known` 是
    服务端已经算好的计数。三个来源都认，认不出来就显示 0/0，不猜。
    """
    if not isinstance(state, dict):
        return None
    nodes = state.get("nodes")
    if isinstance(nodes, dict):
        recs = list(nodes.values())
    elif isinstance(nodes, list):
        recs = nodes
    else:
        recs = []
    alive = state.get("alive")
    if not isinstance(alive, int):
        alive = sum(1 for rec in recs if isinstance(rec, dict)
                    and (rec.get("_status") or rec.get("status")) == "alive")
    total = state.get("known")
    if not isinstance(total, int):
        total = len(recs)
    return {
        "cluster": str(state.get("cluster_id") or ""),
        "role": str(state.get("role") or "?"),
        "term": state.get("term"),
        "online": alive,
        "total": total,
    }


def load_http(api: str, timeout: float = API_TIMEOUT_S) -> dict | None:
    """从看板取一帧；任何异常都当成「不可达」，交给本地回落。"""
    base = str(api or "").rstrip("/")
    if not base:
        return None
    try:
        data, head = _get_json(base + "/api/status", timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError):
        return None
    rows = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    age = None
    try:
        age = max(0.0, time.time() - float(head.get("x-snapshot-mtime") or 0))
    except (TypeError, ValueError):
        age = None
    cluster = None
    try:
        cdata, _ = _get_json(base + "/api/cluster", timeout)
        if isinstance(cdata, dict):
            cluster = _cluster_from_state(cdata.get("state") or cdata)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError):
        cluster = None                          # 集群信息缺失不影响主视图
    return snapshot(rows, source="http", cluster=cluster, age_s=age)


def load_local(hive_dir: str) -> dict | None:
    """本地协调仓的内存快照（等价 `hive status --json`，但不打印、不 fetch）。

    刻意不 fetch：这个回落存在的意义就是「看板够不着时还能看一眼」，再去做一次
    网络 fetch 只会把它也拖进同样的失败里。读到什么就显示什么。
    """
    try:
        import hive as hive_mod
    except ImportError:
        return None
    try:
        repo = hive_mod.Hive(os.path.abspath(hive_dir or "."))
    except Exception:                          # noqa: BLE001 —— 不是仓库就当没数据
        return None
    cards_fn = getattr(repo, "cards", None)
    if not callable(cards_fn):
        return None
    try:
        cards = cards_fn()
    except Exception:                          # noqa: BLE001 —— 读盘失败也只算没数据
        return None
    if not cards:
        return None
    rows = []
    for tid, card in cards.items():
        try:
            claim = repo.claim_info(tid) or {}
        except Exception:                      # noqa: BLE001
            claim = {}
        try:
            res = repo.result_info(tid) or {}
        except Exception:                      # noqa: BLE001
            res = {}
        state = card.get("state")
        if claim and not res and state not in ("done", "blocked"):
            state = "running"                  # 与 cmd_status 的看板推导一致
        state = str(state or "").lower()
        stale = False
        lease = claim.get("lease_until")
        if lease and not res:
            try:
                stale = hive_mod.parse_iso(lease) < hive_mod.now()
            except (ValueError, TypeError):
                stale = False
        rows.append({
            "id": tid, "title": card.get("title") or "",
            "state": state, "owner": card.get("owner") or "",
            "result_status": (res or {}).get("status") or "",
            "stale": stale,
        })
    age = None
    try:
        newest = max((p.stat().st_mtime for p in (repo.dir / "tasks").glob("T-*.md")),
                     default=0)
        age = max(0.0, time.time() - newest) if newest else None
    except OSError:
        age = None
    return snapshot(rows, source="local", cluster=None, age_s=age)


def load_snapshot(api: str, hive_dir: str) -> dict:
    snap = load_http(api)
    if snap is not None:
        return snap
    snap = load_local(hive_dir)
    if snap is not None:
        return snap
    return snapshot([], source=None, error=f"看板 {api} 不可达，本地也没有可读的任务卡")


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def human_age(age_s: float | None, source: str | None = None) -> str:
    if age_s is None:
        return "未知" if source else "—"
    age_s = max(0.0, float(age_s))
    if age_s < 60:
        return f"{int(age_s)}s 前"
    if age_s < 3600:
        return f"{int(age_s // 60)}m 前"
    return f"{int(age_s // 3600)}h 前"


def _clock(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now else time.time()))


def _header(snap: dict, width: int) -> str:
    cluster = snap.get("cluster") or {}
    if cluster:
        nodes = f"{cluster.get('online')}/{cluster.get('total')}"
        head = (f"hive top │ {cluster.get('cluster') or 'hive'} │ "
                f"{cluster.get('role') or '?'} │ term {cluster.get('term')} │ "
                f"节点 {nodes} │ 数据 {human_age(snap.get('age_s'), snap.get('source'))}")
    else:
        src = "本机" if snap.get("source") == "local" else "—"
        head = (f"hive top │ 集群 ? │ 角色 ? │ term ? │ 节点 ?/? │ "
                f"数据 {human_age(snap.get('age_s'), snap.get('source'))}（{src}）")
    return fit(head, width)


def _group_head(name: str, count: int, width: int, shown: int | None = None) -> str:
    """组标题行：` 待领取 12 · 显示 5 ─────`。

    被高度裁掉的行数直接写在标题里，而不是在组尾补一行「还有 N 张」——
    后者要多占一行的预算，小终端上很容易把整帧顶出屏幕。
    """
    head = f" {name} {count} "
    if shown is not None and shown < count:
        head = f" {name} {count} · 显示 {shown} "
    if text_width(head) >= width:
        return fit(head, width)
    return head + "─" * (width - text_width(head))


def layout_counts(buckets: dict[str, list[dict]], height: int | None) -> dict[str, int] | None:
    """按可用高度决定每组显示几张；height 为 None 时不做纵向裁剪。"""
    if not height:
        return None
    height = max(int(height), 8)
    empty = [name for name in GROUPS if not buckets[name]]
    # 头部 2 行 + 每组一个标题 + 尾部 2 行；空组的「（无）」各占 1 行。
    # 用 len(GROUPS) 现算，以后再加组不用回来改这个魔法数字。
    room = max(0, height - 4 - len(GROUPS) - len(empty))
    pending = [name for name in GROUPS if buckets[name]]
    counts = {name: 0 for name in GROUPS}
    if room >= len(pending):
        for name in pending:                   # 先保证每组都露一张
            counts[name] = 1
            room -= 1
    else:
        for name in pending[:room]:            # 连每组一张都放不下：按顺序给
            counts[name] = 1
            room -= 1
        return counts
    while room > 0:                            # 还有余量就轮转补齐
        progressed = False
        for name in pending:
            if room <= 0:
                break
            if counts[name] < len(buckets[name]):
                counts[name] += 1
                room -= 1
                progressed = True
        if not progressed:
            break
    return counts


def render_frame(snap: dict, width: int = 80, height: int | None = None,
                 now: float | None = None, interval: float = REFRESH_S) -> list[str]:
    """渲染一帧，返回行列表。**每行列宽都 ≤ width**（这是本模块唯一的硬承诺）。"""
    width = max(int(width or 0), WIDTH_MIN)
    lines: list[str] = [_header(snap, width), "─" * width]

    if not snap.get("source"):
        reason = snap.get("error") or "数据源不可达"
        lines.append(fit(f" 无数据（{reason}）", width))
        lines.append(fit(" 看板：hive dash start │ 本地：hive --dir <协调仓> top", width))
    else:
        buckets = group_tasks(snap.get("tasks"))
        counts = layout_counts(buckets, height)
        for name in GROUPS:
            group = buckets[name]
            shown = counts[name] if counts is not None else len(group)
            lines.append(_group_head(name, len(group), width,
                                     None if counts is None else shown))
            if not group:
                lines.append(fit("   （无）", width))
                continue
            for task in group[:shown]:
                owner = f"  @{task['owner']}" if task.get("owner") else ""
                body = f"  {task['id']}  {task['title']}{owner}"
                lines.append(fit(body, width))
    lines.append("─" * width)
    tail = f" {_clock(now)} │ {int(interval)}s 刷新 │ q 退出 "
    lines.append(fit(tail, width))
    return [fit(line, width) for line in lines]


# --------------------------------------------------------------------------- #
# 键盘（POSIX: select+cbreak / Windows: msvcrt）
# --------------------------------------------------------------------------- #
QUIT_KEYS = ("q", "Q", "\x1b", "\x03")


class Keyboard:
    """非阻塞读一个键；`q`/`Esc`/`Ctrl-C` 由调用方判定。"""

    def __init__(self) -> None:
        self._fd = None
        self._saved = None
        self._win = sys.platform.startswith("win")

    def __enter__(self) -> "Keyboard":
        if not self._win:
            try:
                import termios
                import tty
                self._fd = sys.stdin.fileno()
                if os.isatty(self._fd):
                    self._saved = termios.tcgetattr(self._fd)
                    tty.setcbreak(self._fd)
            except (ImportError, OSError, ValueError):
                self._fd = None
        return self

    def __exit__(self, *exc) -> None:
        if self._saved is not None and self._fd is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except (ImportError, OSError, ValueError):
                pass
            self._saved = None

    def wait(self, timeout: float) -> str | None:
        """最多等 timeout 秒；读到就返回那个字符，读不到返回 None。"""
        if self._win:
            import msvcrt
            end = time.time() + max(0.0, timeout)
            while time.time() < end:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    return "\x03" if ch == "\x03" else ch
                time.sleep(0.05)
            return None
        import select
        try:
            fd = self._fd if self._fd is not None else sys.stdin.fileno()
        except (OSError, ValueError):
            time.sleep(max(0.0, timeout))
            return None
        try:
            ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            time.sleep(max(0.0, timeout))
            return None
        if not ready:
            return None
        try:
            data = os.read(fd, 1)
        except OSError:
            return None
        return data.decode("utf-8", "replace") if data else "\x03"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def dashboard_api(args) -> str:
    explicit = getattr(args, "api", "") or ""
    if explicit:
        return explicit
    port = os.environ.get("HIVE_DASHBOARD_PORT") or DASH_PORT_DEFAULT
    return f"http://127.0.0.1:{port}"


def resolve_width(args) -> int:
    fixed = int(getattr(args, "width", 0) or 0)
    if fixed > 0:
        return max(fixed, WIDTH_MIN)
    try:
        return max(shutil.get_terminal_size((80, 24)).columns, WIDTH_MIN)
    except OSError:
        return 80


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def run(args) -> int:
    """`hive top` 的实现入口（hive.py 只做 argparse + 派发）。"""
    api = dashboard_api(args)
    hive_dir = getattr(args, "dir", ".") or "."
    interval = float(getattr(args, "interval", REFRESH_S) or REFRESH_S)
    interval = max(0.2, interval)
    clear = not bool(getattr(args, "no_clear", False))

    if getattr(args, "once", False):
        snap = load_snapshot(api, hive_dir)
        for line in render_frame(snap, resolve_width(args), None, interval=interval):
            print(line)
        return 0

    width = resolve_width(args)
    fixed_width = int(getattr(args, "width", 0) or 0) > 0
    try:
        with Keyboard() as kb:
            _write("\x1b[?25l")                # 藏光标：重绘时别闪
            first = True
            while True:
                if not fixed_width:
                    width = resolve_width(args)
                try:
                    height = shutil.get_terminal_size((80, 24)).lines
                except OSError:
                    height = 24
                snap = load_snapshot(api, hive_dir)
                frame = render_frame(snap, width, height, interval=interval)
                if clear and not first:
                    _write("\x1b[H\x1b[2J")    # 首帧不清屏：别把上一屏的命令擦掉
                _write("\n".join(frame) + "\n")
                first = False
                key = kb.wait(interval)
                if key is None:
                    continue
                if key in QUIT_KEYS:
                    break
    except KeyboardInterrupt:
        pass                                   # Ctrl-C 也是干净退出的一种
    finally:
        _write("\x1b[?25h\n")                  # 无论如何都把光标还回去
    return 0


def main(argv=None) -> int:
    """独立运行（调试用）：`python3 hive_tui.py --once`。"""
    import argparse
    p = argparse.ArgumentParser(prog="hive top", description="常驻终端视图（只读）")
    p.add_argument("--once", action="store_true", help="只打印一帧就退出")
    p.add_argument("--width", type=int, default=0, help="固定宽度（默认终端宽度）")
    p.add_argument("--interval", type=float, default=REFRESH_S, help="刷新间隔秒")
    p.add_argument("--no-clear", action="store_true", help="不清屏")
    p.add_argument("--api", default="", help="看板地址")
    p.add_argument("--dir", default=".", help="协调仓路径（本地回落用）")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
