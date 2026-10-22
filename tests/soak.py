#!/usr/bin/env python3
"""hive 浸泡测试：多 worker 并发 + 随机杀进程 + 任务重派 + 最终一致性检查。

用法:
  python3 tests/soak.py --total 240 --workers 3 --minutes 20 --sleep 5 \
      --workdir /tmp/hive-soak

它会：
1. 建一个临时世界（代码仓 + 协调仓 + 每 worker 独立 clone）；
2. 造 N 张任务卡（verify 为 true，fake agent 睡几秒）；
3. 起 W 个 worker，持续 dispatch；
4. 每 30–45 秒随机 kill 一个 worker，把它名下的 claim/已分派任务重派给别人，再拉起；
5. 到点后等收尾，检查每张卡是否恰好一份 done 结果。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from conftest import FAKE, HIVE, World, sh  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=240)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--wait", type=float, default=5, help="到点后等待收尾的分钟数")
    ap.add_argument("--sleep", type=float, default=5, help="fake agent 每次干活的秒数")
    ap.add_argument("--workdir")
    ap.add_argument("--seed", type=int, default=20261023)
    args = ap.parse_args()

    random.seed(args.seed)
    started = time.time()
    root = Path(args.workdir or f"/tmp/hive-soak-{int(started)}").expanduser()
    root.mkdir(parents=True, exist_ok=True)
    print(f"[soak] 世界目录: {root}", flush=True)

    world = World(root)
    names = [f"w{i}" for i in range(1, args.workers + 1)]
    for name in names:
        world.add_agent(name, "exec", caps=["soak"], command=f"{sys.executable} {FAKE}")
    print(f"[soak] 已登记 {len(names)} 个 agent", flush=True)

    tids = [f"T-{i:04d}" for i in range(1, args.total + 1)]
    for tid in tids:
        world.add_task(tid, title=f"浸泡任务 {tid}", verify=["true"])
    print(f"[soak] 已创建 {len(tids)} 张任务卡", flush=True)

    clones: dict[str, Path] = {}
    procs: dict[str, subprocess.Popen] = {}
    logs: dict[str, object] = {}

    def start(name: str) -> subprocess.Popen:
        clone = clones.get(name)
        if clone is None:
            clone = root / f"hive-{name}"
            sh(["git", "clone", "-q", world.hive_origin, clone])
            sh(["git", "-C", clone, "config", "gc.auto", "0"], check=False)
            clones[name] = clone
        lock = clone / ".git" / "index.lock"
        if lock.exists():
            lock.unlink()  # 上次被 SIGKILL 留下的锁，重启时清掉
        log = open(root / f"{name}.log", "a", encoding="utf-8")
        logs[name] = log
        return subprocess.Popen(
            [sys.executable, str(HIVE), "--dir", str(clone), "work",
             "--agent", name, "--interval", "1",
             "--state-dir", str(root / f"state-{name}")],
            stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "FAKE_SLEEP": str(args.sleep)},
            start_new_session=True,
        )

    for name in names:
        procs[name] = start(name)
    print("[soak] worker 已启动", flush=True)

    def fetch() -> None:
        sh(["git", "-C", world.hive1, "fetch", "--prune", "origin",
            "+refs/heads/*:refs/remotes/origin/*"], check=False)

    res_cache: set[str] = set()

    def refresh_results() -> set[str]:
        nonlocal res_cache
        res_cache = {r.rsplit("/", 1)[-1]
                     for r in world.refs("refs/heads/res/")}
        return res_cache

    def has_result(tid: str) -> bool:
        return tid in res_cache

    kills = retries = reassigned = 0
    deadline = started + args.minutes * 60
    next_chaos = started + 30
    last_report = 0.0

    while time.time() < deadline:
        fetch()
        refresh_results()
        world.hive("dispatch", "--apply", check=False)
        done = sum(1 for t in tids if has_result(t))
        now_ts = time.time()
        if now_ts - last_report >= 30:
            print(f"[soak] {int(now_ts - started):>4}s  done={done}/{len(tids)} "
                  f"kills={kills} retries={retries} reassigned={reassigned}", flush=True)
            last_report = now_ts

        if now_ts >= next_chaos:
            next_chaos = now_ts + random.uniform(30, 45)
            victim = random.choice(names)
            proc = procs.get(victim)
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                kills += 1
                live = [n for n in names if n != victim]
                if live:
                    target = random.choice(live)
                    fetch()
                    refresh_results()
                    for tid in tids:
                        if has_result(tid):
                            continue
                        claim = None
                        ref = world.hive1
                        p = sh(["git", "-C", ref, "show", f"origin/claim/{tid}:claims/{tid}.json"],
                               check=False)
                        if p.returncode == 0:
                            claim = json.loads(p.stdout)
                        if claim and claim.get("agent") == victim:
                            world.hive("retry", tid, "--agent", target, check=False)
                            retries += 1
                        else:
                            card = world.card(tid)
                            if card.get("owner") == victim and card.get("state") == "assigned":
                                world.hive("assign", tid, "--agent", target, check=False)
                                reassigned += 1
                print(f"[soak] kill {victim} -> 重派给 {target if live else '-'} "
                      f"(kills={kills} retries={retries})", flush=True)
                time.sleep(1.5)
                procs[victim] = start(victim)

        time.sleep(2)

    # 收尾：停止制造新任务，等剩下的人干完
    print("[soak] 到点，等待收尾…", flush=True)
    end_wait = time.time() + args.wait * 60
    while time.time() < end_wait:
        fetch()
        refresh_results()
        world.hive("dispatch", "--apply", check=False)
        done = sum(1 for t in tids if has_result(t))
        if done == len(tids):
            break
        time.sleep(3)

    for name, proc in procs.items():
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
    for log in logs.values():
        log.close()

    fetch()
    refresh_results()
    done, failed, missing, bad = [], [], [], []
    for tid in tids:
        if not has_result(tid):
            missing.append(tid)
            continue
        data = json.loads(sh(["git", "--git-dir", world.hive_origin,
                              "show", f"res/{tid}:results/{tid}.json"]).stdout)
        if data.get("status") == "done":
            done.append(tid)
        elif data.get("status") == "failed":
            failed.append(tid)
        else:
            bad.append(tid)
        if data.get("verify") and any(v.get("code") for v in data["verify"]):
            bad.append(tid)

    elapsed = round(time.time() - started, 1)
    report = {
        "ok": not missing and not failed and not bad,
        "total": len(tids), "done": len(done), "failed": len(failed),
        "missing": missing, "bad": bad,
        "kills": kills, "retries": retries, "reassigned": reassigned,
        "elapsed_s": elapsed, "workers": args.workers, "sleep_s": args.sleep,
        "workdir": str(root),
    }
    (root / "soak-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "missing"}, ensure_ascii=False, indent=2))
    if report["missing"]:
        print(f"[soak] 缺结果 {len(missing)}: {missing[:10]}", flush=True)
    print("[soak] PASS" if report["ok"] else "[soak] FAIL", flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
