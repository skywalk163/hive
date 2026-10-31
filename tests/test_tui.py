"""T-0218 `hive top`：纯标准库常驻 TUI 的可断言出口（`--once`）。

为什么测试全走 `--once`
----------------------
常驻视图真正的循环没有稳定的判定点（刷屏、按键、终端大小都在变）。`--once`
把「一帧」变成一次确定的 stdout，于是三条判据都能写成断言：

1. 每行可见宽度 ≤ `--width`（中文按 2 列）；
2. 四组标题齐全，且卡片数与输入快照一致；
3. 数据源不可达时输出「无数据」且退出码 0。

每条判据都配了反证：把裁剪/分组拆掉，对应用例必然变红（见各用例注释）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hive_tui  # noqa: E402

from hive_compat import no_window_kwargs  # noqa: E402

HIVE = ROOT / "hive.py"
WIDTH = 80

TID_RE = re.compile(r"^\s+(T-\d{4,})\s")

# 9 张卡：3 待领取 / 2 进行中 / 2 待合并 / 2 异常
# 标题故意写长（中英混排），宽度判据才有东西可截
TASKS = [
    {"id": "T-9001", "title": "待领取示例一：一个足够长的中文标题，用来验证中文按双宽计算不会溢出 80 列",
     "state": "ready", "owner": ""},
    {"id": "T-9002", "title": "待领取示例二 mixed 中英文 title with ASCII words inside",
     "state": "pending", "owner": ""},
    {"id": "T-9003", "title": "待领取示例三：短标题", "state": "ready", "owner": ""},
    {"id": "T-9010", "title": "进行中示例一：worker 正在跑，标题很长很长很长很长很长很长很长很长很长",
     "state": "assigned", "owner": "workbuddy"},
    {"id": "T-9011", "title": "进行中示例二：租约还有效", "state": "running", "owner": "fb88-dsh"},
    {"id": "T-9020", "title": "待合并示例一：结果已提交但还没 land", "state": "assigned",
     "owner": "workbuddy", "result_status": "done"},
    {"id": "T-9021", "title": "待合并示例二：结果 done 等合入", "state": "running",
     "owner": "fb88-dsh", "result_status": "done"},
    {"id": "T-9030", "title": "异常示例一：verify 失败", "state": "assigned",
     "owner": "workbuddy", "result_status": "failed"},
    {"id": "T-9031", "title": "异常示例二：租约过期（stale）", "state": "ready", "stale": True},
]


# --------------------------------------------------------------------------- #
# 夹具：一个只会说 /api/status 的假看板
# --------------------------------------------------------------------------- #
class _Stub(BaseHTTPRequestHandler):
    rows: list = []

    def _json(self, payload, mtime=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if mtime is not None:
            self.send_header("X-Snapshot-Mtime", str(int(mtime)))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server 的接口名)
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._json(self.rows, mtime=time.time() - 5)
        elif path == "/api/cluster":
            self._json({"ok": True, "running": True, "state": {
                "cluster_id": "hive", "term": 7, "role": "leader",
                "alive": 2, "known": 2,
                "nodes": [{"name": "a", "_status": "alive"},
                          {"name": "b", "_status": "alive"}],
            }})
        else:
            self.send_error(404)

    def log_message(self, *args):  # 别把测试日志刷满
        pass


@pytest.fixture()
def stub_url():
    _Stub.rows = [dict(r) for r in TASKS]
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def run_top(*args, hive_dir: Path, api="http://127.0.0.1:9") -> subprocess.CompletedProcess:
    """跑一次 `hive --dir <空仓> top --once ...`，返回进程结果。

    默认 api 指向一个必然连不上的端口：需要看板的用例显式传 `--api <stub>`，
    这样「谁会不小心读到开发机真实看板」这件事在用例里一眼可见。
    """
    cmd = [sys.executable, str(HIVE), "--dir", str(hive_dir),
           "top", "--once", *args]
    if api:
        cmd += ["--api", api]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "HIVE_DASH_AUTOSTART": "0", "HIVE_TESTING": "1"}
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=120,
                          **no_window_kwargs())


def empty_repo(tmp_path: Path) -> Path:
    """一个没有任务卡的协调仓（本地回落也拿不到东西）。"""
    d = tmp_path / "hive-empty"
    d.mkdir()
    (d / ".git").mkdir()
    return d


def repo_with_card(tmp_path: Path) -> Path:
    """一个只有一张卡的最小协调仓：验证「看板挂了还能看见本机」。"""
    d = tmp_path / "hive-local"
    (d / "tasks").mkdir(parents=True)
    (d / ".git").mkdir()
    (d / "tasks" / "T-9001.md").write_text(
        "---\n"
        "id: T-9001\n"
        "title: 本地快照：看板挂了也要能看见这张卡\n"
        "repo: hive\n"
        "base: main\n"
        "state: ready\n"
        "owner: null\n"
        "agent: null\n"
        "fresh: false\n"
        "priority: P2\n"
        "needs:\n"
        "paths:\n"
        "verify:\n"
        "depends_on:\n"
        "lease_minutes: 150\n"
        "timeout_minutes: 120\n"
        "created: 2026-10-31T09:51:29+08:00\n"
        "max_retries: 3\n"
        "---\n## 背景\n本地回落用例\n",
        encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=False,
                   capture_output=True, **no_window_kwargs())
    return d


def parse_groups(lines) -> dict[str, list[str]]:
    """把一帧拆回 {组名: [卡号...]}；组头形如 ` 待领取 3 ───`。"""
    out: dict[str, list[str]] = {}
    current = None
    for line in lines:
        hit = None
        for name in hive_tui.GROUPS:
            if re.match(rf"^\s*{re.escape(name)}\s+\d+", line):
                hit = name
                break
        if hit:
            current = hit
            out[current] = []
            continue
        m = TID_RE.match(line)
        if m and current:
            out[current].append(m.group(1))
    return out


def group_counts(lines) -> dict[str, int]:
    """组头里那个数字（渲染时算好的）——用来和实际行数对账。"""
    out = {}
    for line in lines:
        for name in hive_tui.GROUPS:
            m = re.match(rf"^\s*{re.escape(name)}\s+(\d+)", line)
            if m:
                out[name] = int(m.group(1))
    return out


# --------------------------------------------------------------------------- #
# 1) 宽度：中文双宽 + 超宽截断
# --------------------------------------------------------------------------- #
def test_中文按双宽计算且截断带省略号():
    assert hive_tui.text_width("中文") == 4
    assert hive_tui.text_width("ab") == 2
    assert hive_tui.text_width("中a") == 3

    long = "很长的中文标题" * 10
    cut = hive_tui.truncate(long, 20)
    assert hive_tui.text_width(cut) <= 20
    assert cut.endswith("…")
    # 反证：如果按 len() 裁剪，这里会是 20 个汉字 = 40 列，必然溢出
    assert len(cut) <= 20 and hive_tui.text_width(cut) <= 20


def test_once_width_80_每行都不超宽(stub_url, tmp_path):
    p = run_top("--width", str(WIDTH), hive_dir=empty_repo(tmp_path), api=stub_url)
    assert p.returncode == 0, p.stderr
    assert "Traceback" not in p.stderr

    lines = p.stdout.rstrip("\n").split("\n")
    over = [(hive_tui.text_width(line), line) for line in lines
            if hive_tui.text_width(line) > WIDTH]
    assert not over, f"这些行超过 {WIDTH} 列: {over[:3]}"

    # 反证：同一份快照不做裁剪时（宽度给够）一定会超宽。
    # 也就是说把 truncate 拆掉，上面那条断言必然变红。
    natural = hive_tui.render_frame(
        hive_tui.snapshot(TASKS, source="http", cluster=None), 10000)
    assert max(hive_tui.text_width(line) for line in natural) > WIDTH


def test_width_下限是60(stub_url, tmp_path):
    """--width 40 会被抬到 60：再窄连卡号都放不下。"""
    p = run_top("--width", "40", hive_dir=empty_repo(tmp_path), api=stub_url)
    assert p.returncode == 0, p.stderr
    widths = [hive_tui.text_width(l) for l in p.stdout.rstrip("\n").split("\n")]
    assert max(widths) == hive_tui.WIDTH_MIN


# --------------------------------------------------------------------------- #
# 2) 四组齐全 + 卡片数与快照一致
# --------------------------------------------------------------------------- #
def test_once_四组齐全且卡片数与快照一致(stub_url, tmp_path):
    p = run_top("--width", str(WIDTH), hive_dir=empty_repo(tmp_path), api=stub_url)
    assert p.returncode == 0, p.stderr
    lines = p.stdout.rstrip("\n").split("\n")

    groups = parse_groups(lines)
    assert set(groups) == set(hive_tui.GROUPS), f"缺组: {groups.keys()}"

    expected = {
        "待领取": ["T-9001", "T-9002", "T-9003"],
        "进行中": ["T-9010", "T-9011"],
        "待合并": ["T-9020", "T-9021"],
        "异常": ["T-9030", "T-9031"],
    }
    assert groups == expected
    # 组头数字必须等于实际行数（两边都错成同一个数也算错，所以还要对账）
    assert group_counts(lines) == {k: len(v) for k, v in expected.items()}

    total = sum(len(v) for v in groups.values())
    assert total == len(TASKS)
    # 一张卡只出现在一个组里
    flat_ids = [tid for ids in groups.values() for tid in ids]
    assert len(flat_ids) == len(set(flat_ids)) == len(TASKS)


def test_少一张卡就少一行():
    """反证：卡片数不是写死的常量，而是跟着快照走的。"""
    full = hive_tui.render_frame(hive_tui.snapshot(TASKS, source="http"), WIDTH)
    fewer = hive_tui.render_frame(hive_tui.snapshot(TASKS[:-1], source="http"), WIDTH)
    assert sum(len(v) for v in parse_groups(full).values()) == len(TASKS)
    assert sum(len(v) for v in parse_groups(fewer).values()) == len(TASKS) - 1


def test_已完成的卡不进四组():
    rows = [{"id": "T-9100", "title": "已经 done 的卡", "state": "done",
             "result_status": "done"}]
    assert hive_tui.classify(hive_tui.snapshot(rows, source="http")["tasks"][0]) is None
    groups = parse_groups(hive_tui.render_frame(
        hive_tui.snapshot(rows, source="http"), WIDTH))
    assert sum(len(v) for v in groups.values()) == 0


def test_高度裁剪后不出屏且标明显示几张():
    """窗口只有 14 行：整帧必须 ≤ 14 行，被裁掉的组要在标题里说清楚。"""
    rows = [{"id": f"T-9{i:03d}", "title": "很长的中文标题" * 3, "state": "ready"}
            for i in range(10)]
    frame = hive_tui.render_frame(hive_tui.snapshot(rows, source="http"), WIDTH,
                                  height=14)
    assert len(frame) <= 14
    assert any("显示" in line for line in frame)
    assert sum(len(v) for v in parse_groups(frame).values()) < len(rows)


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="Windows 的按键走 msvcrt，管道输入读不到（真机手测）")
def test_q_键干净退出(tmp_path):
    """交互模式下按 q：退出码 0、只渲染一帧、光标还回去。"""
    p = subprocess.run(
        [sys.executable, str(HIVE), "--dir", str(empty_repo(tmp_path)),
         "top", "--width", "60", "--no-clear", "--api", "http://127.0.0.1:9"],
        input="q", capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
             "HIVE_DASH_AUTOSTART": "0", "HIVE_TESTING": "1"},
        **no_window_kwargs())
    assert p.returncode == 0, p.stderr
    assert p.stdout.count("hive top") == 1
    assert "\x1b[?25h" in p.stdout          # 退出时把光标还回去


# --------------------------------------------------------------------------- #
# 3) 数据源不可达：无数据 + 退出码 0
# --------------------------------------------------------------------------- #
def test_两个数据源都不可达时显示无数据且退出码0(tmp_path):
    p = run_top("--width", str(WIDTH), hive_dir=empty_repo(tmp_path),
                api="http://127.0.0.1:9")
    assert p.returncode == 0, f"退出码应为 0，实际 {p.returncode}: {p.stderr}"
    assert "无数据" in p.stdout
    assert "Traceback" not in p.stderr
    # 无数据也是一帧：头尾都要在，不能是半截输出
    lines = p.stdout.rstrip("\n").split("\n")
    assert lines[0].startswith("hive top")
    assert "q 退出" in lines[-1]


def test_看板挂了回落到本地快照(tmp_path):
    """看板不可达 ≠ 没数据：本地协调仓里的卡也要能看见。"""
    p = run_top("--width", str(WIDTH), hive_dir=repo_with_card(tmp_path),
                api="http://127.0.0.1:9")
    assert p.returncode == 0, p.stderr
    assert "无数据" not in p.stdout
    groups = parse_groups(p.stdout.rstrip("\n").split("\n"))
    assert groups["待领取"] == ["T-9001"]
    assert "本机" in p.stdout          # 头部的数据来源标记


# --------------------------------------------------------------------------- #
# 分类语义（顺序即优先级）
# --------------------------------------------------------------------------- #
def test_分类优先级():
    pick = lambda **kw: hive_tui.snapshot([{"id": "T-1", "title": "x", **kw}],
                                          source="http")["tasks"][0]
    assert hive_tui.classify(pick(state="ready")) == "待领取"
    assert hive_tui.classify(pick(state="assigned", owner="a")) == "进行中"
    assert hive_tui.classify(pick(state="assigned", result_status="done")) == "待合并"
    assert hive_tui.classify(pick(state="assigned",
                                  result_status="failed")) == "异常"
    assert hive_tui.classify(pick(state="ready", stale=True)) == "异常"
    assert hive_tui.classify(pick(state="blocked")) == "异常"
    assert hive_tui.classify(pick(state="done", result_status="done")) is None
