"""hive 核心行为测试：分派、体检、统计、领取、验收、归档。"""
from __future__ import annotations

import json
import subprocess
import sys

import hive
import pytest

from conftest import HIVE, sh


def show(world, ref: str) -> str:
    return sh(["git", "--git-dir", world.hive_origin, "show", ref]).stdout


# --------------------------------------------------------------------------- #
# 分派
# --------------------------------------------------------------------------- #

def test_dispatch_auto_plans_and_applies(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    world.add_agent("win", "verify", caps=["windows"])
    world.add_task("T-0001", needs=["freebsd"], paths=["src/"])
    world.add_task("T-0002", needs=["windows"])
    world.add_task("T-0003", needs=["windows"], paths=["src/"])   # 验收机不改代码
    world.add_task("T-0004", depends=["T-0001"])                  # 依赖未完成

    out = world.hive_json("dispatch")
    plan = {p["task"]: p["agent"] for p in out["plan"]}
    assert plan == {"T-0001": "fb", "T-0002": "win"}, out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    assert "T-0003" in reasons and "T-0004" in reasons

    # 默认 dry-run，不写
    assert world.card("T-0001")["owner"] is None

    world.hive("dispatch", "--apply")
    assert world.card("T-0001")["owner"] == "fb"
    assert world.card("T-0002")["owner"] == "win"
    assert (world.hive1 / "inbox" / "fb" / "T-0001.md").exists()

    # 都分完了，第二轮没有新计划
    assert world.hive_json("dispatch")["plan"] == []
    # stats 把「已分派未领取」也算进行中
    stats = json.loads(world.hive("stats", "--json").stdout)
    assert stats["summary"]["running"] == 2, stats


def test_dispatch_respects_max_concurrency(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    for i in range(3):
        world.add_task(f"T-000{i + 1}")
    out = world.hive_json("dispatch")
    assert len(out["plan"]) == 1
    assert {s["task"] for s in out["skipped"]} == {"T-0002", "T-0003"}


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #

def test_doctor_ok(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    world.add_task("T-0001")
    p = world.hive("doctor")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "[ OK ]" in p.stdout


def test_doctor_detects_broken_card(world):
    (world.hive1 / "tasks" / "T-9999.md").write_text("不是任务卡\n", encoding="utf-8")
    p = world.hive("doctor", check=False)
    assert p.returncode != 0
    assert "[ERR ]" in p.stdout


# --------------------------------------------------------------------------- #
# 领取 / 验收 / 统计 / 归档 / 回收
# --------------------------------------------------------------------------- #

def test_claim_is_atomic(world, fake_cmd):
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_agent("a2", "exec", command=fake_cmd)
    world.add_task("T-0001")
    procs = [
        subprocess.Popen(
            [sys.executable, str(HIVE),
             "--dir", str(world.hive1 if name == "a1" else world.hive2),
             "claim", "T-0001", "--agent", name],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for name in ("a1", "a2")
    ]
    codes = [p.wait() for p in procs]
    assert sorted(codes) == [0, 1], codes
    claims = world.refs("refs/heads/claim/T-0001")
    assert len(claims) == 1
    data = json.loads(show(world, "claim/T-0001:claims/T-0001.json"))
    assert data["agent"] in ("a1", "a2")


def test_verify_mode_stats_and_close(world):
    world.add_agent("win", "verify", caps=["windows"])
    world.add_task("T-0001", needs=["windows"], verify=["true"])
    world.hive("assign", "T-0001", "--agent", "win")
    p = world.run_worker("win")
    assert p.returncode == 0, p.stdout + p.stderr

    res = json.loads(show(world, "res/T-0001:results/T-0001.json"))
    assert res["status"] == "done" and res["mode"] == "verify", res
    assert world.refs("refs/heads/task/T-0001") == []

    stats = json.loads(world.hive("stats", "--json").stdout)
    assert stats["summary"]["done"] == 1
    assert stats["summary"]["rate"] == 100.0

    world.hive("close", "T-0001", "--note", "ok")
    archived = json.loads(show(world, "main:results/T-0001.json"))
    assert archived["status"] == "done"
    assert world.refs("refs/heads/claim/T-0001") == []
    assert world.refs("refs/heads/res/T-0001") == []
    status = json.loads(world.hive("status", "--json").stdout)
    assert status[0]["result"] == "done"


def test_gc_reclaims_expired_claim(world, fake_cmd):
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_task("T-0001")
    world.hive("claim", "T-0001", "--agent", "a1")
    assert world.refs("refs/heads/claim/T-0001")
    world.hive("gc", "--minutes", "0")
    assert world.refs("refs/heads/claim/T-0001") == []


def test_retry_fresh_clears_archive(world):
    world.add_agent("win", "verify", caps=["windows"])
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "win")
    world.run_worker("win")
    world.hive("close", "T-0001")
    assert json.loads(show(world, "main:results/T-0001.json"))["status"] == "done"

    world.hive("retry", "T-0001", "--fresh")
    assert world.hive("status", "--json").returncode == 0
    # 归档已从 main 撤掉
    check = subprocess.run(
        ["git", "--git-dir", str(world.hive_origin), "show", "main:results/T-0001.json"],
        text=True, capture_output=True)
    assert check.returncode != 0
    assert world.card("T-0001")["fresh"] is True


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #

def test_parse_card_roundtrip(tmp_path):
    path = tmp_path / "T-0001.md"
    data = {
        "id": "T-0001", "title": "标题: 带冒号", "repo": "light", "base": "main",
        "state": "ready", "owner": None, "priority": "P1", "needs": ["windows"],
        "paths": ["src/"], "verify": ["true"], "depends_on": [],
        "lease_minutes": 30, "timeout_minutes": 10, "created": "2026-10-23T00:00:00+08:00",
    }
    hive.write_card(path, data, "# 正文\n")
    got, body = hive.parse_card(path)
    assert got["title"] == "标题: 带冒号"
    assert got["needs"] == ["windows"]
    assert got["lease_minutes"] == 30
    assert body == "# 正文\n"


def test_parse_card_rejects_bad_list(tmp_path):
    path = tmp_path / "T-0001.md"
    path.write_text("---\nid: T-0001\n  - 孤儿列表项\n---\n", encoding="utf-8")
    with pytest.raises(hive.HiveError):
        hive.parse_card(path)


def test_take_and_submit_manual(world):
    """手工领取（take）+ 手工提交（submit）：不依赖 worker 在跑。

    对应「人工在面板/CLI 领取、标记 agent、完成后回主控验收」的工作流。
    """
    wt = world.root / "wt-manual"
    world.add_agent("manual-agent", mode="manual", worktrees=str(wt))
    world.add_task("T-0001", paths=["docs/"], verify=["test -f docs/ok.md"])
    world.hive("take", "T-0001", "--agent", "manual-agent")
    wtask = wt / "T-0001"
    assert wtask.exists() and (wtask / ".hive" / "task.md").exists()

    # 重复 take：claim 已在自己名下，复用同一 worktree
    world.hive("take", "T-0001", "--agent", "manual-agent")

    # 其他 agent 领取会被拒
    world.add_agent("other", mode="manual", worktrees=str(world.root / "wt-other"))
    rejected = False
    try:
        world.hive("take", "T-0001", "--agent", "other")
    except AssertionError:
        rejected = True
    assert rejected

    (wtask / "docs").mkdir(exist_ok=True)
    (wtask / "docs" / "ok.md").write_text("x\n", encoding="utf-8")
    (wtask / ".hive" / "summary.md").write_text("完成\n", encoding="utf-8")
    world.hive("submit", "T-0001")
    res = json.loads(world.hive("result", "T-0001", "--json").stdout)
    assert res["status"] == "done"
    assert res["mode"] == "manual"
    assert res["agent"] == "manual-agent"
    assert res["verify"][0]["code"] == 0
    assert not wtask.exists()  # 默认清理 worktree
    # 结果已写，但和 worker 流程一致：state 到 close 才翻成 done
    assert world.card("T-0001")["state"] == "assigned"


def test_concurrent_claims_share_one_clone(world, fake_cmd):
    """两个 worker 共用一个 clone 抢卡：claim 分支必须带 claims/T-xxxx.json。

    这是对「独立 GIT_INDEX_FILE」修复的回归测试：旧实现会互相踩 index，
    可能推出一个没有 claims 文件的空分支。
    """
    world.add_agent("a1", "exec", command=fake_cmd)
    world.add_agent("a2", "exec", command=fake_cmd)
    for i in range(1, 4):
        tid = f"T-000{i}"
        world.add_task(tid)
        procs = [
            subprocess.Popen(
                [sys.executable, str(HIVE), "--dir", str(world.hive1),
                 "claim", tid, "--agent", name],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for name in ("a1", "a2")
        ]
        codes = [p.wait() for p in procs]
        assert sorted(codes) == [0, 1], (tid, codes)
        data = json.loads(show(world, f"claim/{tid}:claims/{tid}.json"))
        assert data["agent"] in ("a1", "a2"), data


# --------------------------------------------------------------------------- #
# 分支清理（prune）
# --------------------------------------------------------------------------- #

def _code_refs(world, pattern="refs/heads/task/*") -> set[str]:
    out = sh(["git", "--git-dir", world.code_origin, "for-each-ref",
              "--format=%(refname)", pattern]).stdout
    return {r.rsplit("/", 1)[-1] for r in out.split()}


def _local_branches(world, pattern="verify/*") -> set[str]:
    out = sh(["git", "-C", world.code_clone("fb"), "branch", "--list", pattern]).stdout
    return {l.strip().removeprefix("* ").strip() for l in out.splitlines() if l.strip()}


def test_prune_deletes_only_merged_branches(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    # 卡 1：跑通，然后由总控合并进代码仓 main
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")
    code = world.code_clone("fb")
    sh(["git", "fetch", "-q", "origin"], cwd=code, env=world.git_env)
    sh(["git", "checkout", "-q", "main"], cwd=code, env=world.git_env)
    sh(["git", "merge", "--no-ff", "-q", "origin/task/T-0001", "-m", "merge T-0001"],
       cwd=code, env=world.git_env)
    sh(["git", "push", "-q", "origin", "main"], cwd=code, env=world.git_env)
    row = next(r for r in world.hive_json("status") if r["id"] == "T-0001")
    assert row["result"] == "done", row

    # 卡 2：在飞（assigned、无结果）。分支内容哪怕已并入 main 也不许删
    world.add_task("T-0002")
    world.hive("assign", "T-0002", "--agent", "fb")
    sh(["git", "push", "-q", "origin", "main:refs/heads/task/T-0002"],
       cwd=code, env=world.git_env)
    # T-9999：没有卡、也没合并，保留
    sh(["git", "checkout", "-q", "-b", "task/T-9999", "origin/main"],
       cwd=code, env=world.git_env)
    (code / "junk.txt").write_text("x\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=code, env=world.git_env)
    sh(["git", "commit", "-q", "-m", "junk"], cwd=code, env=world.git_env)
    sh(["git", "push", "-q", "origin", "task/T-9999"], cwd=code, env=world.git_env)
    sh(["git", "checkout", "-q", "main"], cwd=code, env=world.git_env)
    # 本地 verify/* 遗留：verify 模式不推远端，worktree 删了分支还在，应被清掉
    sh(["git", "branch", "verify/T-0008", "origin/main"], cwd=code, env=world.git_env)

    before = _code_refs(world)
    assert {"T-0001", "T-0002", "T-9999"} <= before, before

    # dry-run：只列计划，一个分支都不动（hive 的日志走 stderr）
    dry = world.hive("prune")
    out = dry.stdout + dry.stderr
    assert "[dry-run]" in out and "T-0001" in out, out
    assert "verify/T-0008" in out, out
    assert _code_refs(world) == before
    assert _local_branches(world) == {"verify/T-0008"}

    # --apply：只删「已合并且不在飞」的 T-0001，外加本地 verify 遗留
    run = world.hive("prune", "--apply")
    out = run.stdout + run.stderr
    assert _code_refs(world) == {"T-0002", "T-9999"}, out
    assert _local_branches(world) == set(), out
    assert "T-0002" in out and "T-9999" in out  # 两条保留的原因都交代了
    assert "T-0001" in out and "已删除" in out


def test_prune_rejects_unknown_repo(world):
    world.add_agent("fb", "verify", caps=["windows"])
    p = world.hive("prune", "--repo", "nope", check=False)
    assert p.returncode != 0
    assert "nope" in (p.stderr + p.stdout)
