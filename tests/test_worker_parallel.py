"""Worker 轻量线程池：按 max_concurrency 在独立 worktree 内并行跑多张 exec 卡（T-0162）。

覆盖两件事：

1. **端到端限流 + 结果收集**：3 张卡 + `max_concurrency=2`，任意时刻至多在跑 2 张、
   且 3 张最终都完成并各写一份 `results/<tid>.json`。
2. **池的边界行为**：空列表、单卡退化为串行、某个任务线程崩溃不拖垮池里其他任务。

并发是靠一个「假 agent」量出来的：每次进程进入/退出时往共享事件文件各追一行
（带时间戳），事后按时间轴还原峰值并发数。这样不依赖实现细节，只看「同时在跑的
agent 进程数」，正是 `max_concurrency` 想约束的东西。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import HIVE
from hive import Worker

PROBE_AGENT = r'''
import json, os, pathlib, time

LOG = os.environ["PROBE_LOG"]
TID = os.environ.get("HIVE_TASK", "?")
SLEEP = float(os.environ.get("PROBE_SLEEP", "1.0"))


def emit(kind):
    line = json.dumps({"kind": kind, "task": TID, "t": time.time()}) + "\n"
    # O_APPEND 下短行的单次 write 是原子的：多进程各追一行不会互相截断。
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line)


emit("start")
time.sleep(SLEEP)
pathlib.Path("feature.txt").write_text("made by " + TID + "\n", encoding="utf-8")
pathlib.Path(".hive").mkdir(exist_ok=True)
pathlib.Path(".hive/summary.md").write_text("# " + TID + " 完成\n", encoding="utf-8")
emit("end")
'''


def _git(git_dir: Path, *args: str) -> str:
    """本地 git 读取，钉死 utf-8 解码（结果 JSON / 分支名里有中文）。"""
    p = subprocess.run(["git", "--git-dir", str(git_dir), *args], text=True,
                       encoding="utf-8", errors="replace", capture_output=True,
                       env={**os.environ})
    return p.stdout


def _probe_command(tmp_path: Path) -> str:
    """把假 agent 落到 tmp（仓库外，不留 tracked 改动），返回 agent command 串。"""
    script = tmp_path / "probe_agent.py"
    script.write_text(PROBE_AGENT, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _run_worker(world, agent: str, max_concurrency: int, tmp_path: Path, extra_env: dict):
    """直接调 CLI：`work --once --max-concurrency N`，用 utf-8 收输出。"""
    cmd = [sys.executable, HIVE, "--dir", str(world.hive1), "work",
           "--agent", agent, "--once",
           "--state-dir", str(tmp_path / f"state-{agent}"),
           "--max-concurrency", str(max_concurrency)]
    return subprocess.run(cmd, text=True, encoding="utf-8", errors="replace",
                          capture_output=True, env={**os.environ, **extra_env})


def _peak_concurrency(events: Path) -> int:
    """按时间轴扫 start/end，还原同一时刻最多几张小卡在跑。"""
    rows = [json.loads(x) for x in
            events.read_text(encoding="utf-8").splitlines() if x.strip()]
    # 同一时间戳先算 end 再算 start：对「并发上限」的判定取保守方向。
    rows.sort(key=lambda r: (r["t"], r["kind"] != "end"))
    cur = peak = 0
    for r in rows:
        cur += 1 if r["kind"] == "start" else -1
        peak = max(peak, cur)
    return peak


def _result_of(world, tid: str) -> dict:
    out = _git(world.hive_origin, "show", f"refs/heads/res/{tid}:results/{tid}.json")
    assert out.strip(), f"{tid} 没有结果（res/{tid} 分支缺失？）"
    return json.loads(out)


# --------------------------------------------------------------------------- #
# 端到端：限流 + 结果收集
# --------------------------------------------------------------------------- #

def test_max_concurrency_throttles_and_collects_all(world, tmp_path):
    events = tmp_path / "events.log"
    world.add_agent("w1", "exec", command=_probe_command(tmp_path))

    tids = ["T-9001", "T-9002", "T-9003"]
    for tid in tids:
        world.add_task(tid, repo="light", paths=["feature.txt"],
                       verify=[f"{sys.executable} -c pass"])
        world.hive("assign", tid, "--agent", "w1")

    proc = _run_worker(world, "w1", max_concurrency=2, tmp_path=tmp_path,
                       extra_env={"PROBE_LOG": str(events), "PROBE_SLEEP": "1.0"})
    assert proc.returncode == 0, proc.stderr

    assert events.exists(), "假 agent 没被调用，并发根本没跑起来"
    peak = _peak_concurrency(events)
    assert peak == 2, (
        f"max_concurrency=2 时并发峰值应为 2，实测 {peak}\n{events.read_text(encoding='utf-8')}")

    # 结果收集：3 张都完成，各自落一份 status=done 的结果。
    refs = _git(world.hive_origin, "for-each-ref", "--format=%(refname)",
                "refs/heads/res/*")
    for tid in tids:
        assert f"refs/heads/res/{tid}" in refs, f"{tid} 没有结果分支"
        res = _result_of(world, tid)
        assert res["status"] == "done", (tid, res)


def test_max_concurrency_one_keeps_single_card_serial(world, tmp_path):
    """上限为 1：3 张仍然都完成，但任意时刻至多 1 张在跑（退化为旧串行行为）。"""
    events = tmp_path / "events.log"
    world.add_agent("w1", "exec", command=_probe_command(tmp_path))

    tids = ["T-9101", "T-9102", "T-9103"]
    for tid in tids:
        world.add_task(tid, repo="light", paths=["feature.txt"],
                       verify=[f"{sys.executable} -c pass"])
        world.hive("assign", tid, "--agent", "w1")

    proc = _run_worker(world, "w1", max_concurrency=1, tmp_path=tmp_path,
                       extra_env={"PROBE_LOG": str(events), "PROBE_SLEEP": "0.3"})
    assert proc.returncode == 0, proc.stderr

    assert _peak_concurrency(events) == 1, "max_concurrency=1 竟然并行跑了"
    for tid in tids:
        res = _result_of(world, tid)
        assert res["status"] == "done", (tid, res)


# --------------------------------------------------------------------------- #
# 池的边界行为（在进程内直接调 run_tasks，不起 CLI）
# --------------------------------------------------------------------------- #

def test_run_tasks_empty_returns_empty(world):
    world.add_agent("w1", "exec", command="x")
    w = Worker(str(world.hive1), "w1")
    assert w.run_tasks([]) == {}


def test_constructor_override_sets_max_workers(world):
    world.add_agent("w1", "exec", command="x")
    w = Worker(str(world.hive1), "w1", max_concurrency=3)
    assert w.max_workers == 3
    # 非法/缺省值都要落回 >=1，绝不当成 0 并发死锁。
    w0 = Worker(str(world.hive1), "w1", max_concurrency=0)
    assert w0.max_workers == 1


def test_run_tasks_single_card_is_serial(world, monkeypatch):
    """只有一张卡时不必开池，逐字走 run_task，返回同构的 {tid: result}。"""
    world.add_agent("w1", "exec", command="x")
    w = Worker(str(world.hive1), "w1", max_concurrency=4)

    seen: list[str] = []
    monkeypatch.setattr(
        w, "run_task",
        lambda tid: seen.append(tid) or {"task": tid, "status": "done"})

    out = w.run_tasks(["T-0001"])
    assert out == {"T-0001": {"task": "T-0001", "status": "done"}}
    assert seen == ["T-0001"]


def test_run_tasks_isolates_failing_task(world, monkeypatch):
    """一个任务线程抛异常，只判它自己失败，池里其他任务照常完成。"""
    world.add_agent("w1", "exec", command="x")
    w = Worker(str(world.hive1), "w1")

    def flaky(tid):
        if tid == "T-BAD":
            raise RuntimeError("boom")
        return {"task": tid, "status": "done"}

    monkeypatch.setattr(w, "run_task", flaky)
    monkeypatch.setattr(
        w, "_fail",
        lambda tid, card, wt, err: {"task": tid, "status": "failed", "error": err})

    out = w.run_tasks(["T-A", "T-BAD", "T-C"], max_workers=2)
    assert set(out) == {"T-A", "T-BAD", "T-C"}
    assert out["T-A"]["status"] == "done"
    assert out["T-C"]["status"] == "done"
    assert out["T-BAD"]["status"] == "failed"
    assert "boom" in out["T-BAD"]["error"]