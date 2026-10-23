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
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    world.add_agent("win", "verify", caps=["windows"],
                    repos={"hive": world.code_clone("win")})
    world.add_task("T-0001", repo="hive", needs=["freebsd"], paths=["src/"])
    world.add_task("T-0002", repo="hive", needs=["windows"])
    world.add_task("T-0003", repo="hive", needs=["windows"],
                   paths=["src/"])                              # 验收机不改代码
    world.add_task("T-0004", repo="hive", depends=["T-0001"])   # 依赖未完成

    out = world.hive_json("dispatch", "--max-per-repo", "2")
    plan = {p["task"]: p["agent"] for p in out["plan"]}
    assert plan == {"T-0001": "fb", "T-0002": "win"}, out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    assert "T-0003" in reasons and "T-0004" in reasons

    # 默认 dry-run，不写
    assert world.card("T-0001")["owner"] is None

    world.hive("dispatch", "--apply", "--max-per-repo", "2")
    assert world.card("T-0001")["owner"] == "fb"
    assert world.card("T-0002")["owner"] == "win"
    assert (world.hive1 / "inbox" / "fb" / "T-0001.md").exists()

    # 都分完了，第二轮没有新计划
    assert world.hive_json("dispatch", "--max-per-repo", "2")["plan"] == []
    # stats 把「已分派未领取」也算进行中
    stats = json.loads(world.hive("stats", "--json").stdout)
    assert stats["summary"]["running"] == 2, stats


def test_dispatch_respects_max_concurrency(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    for i in range(3):
        world.add_task(f"T-000{i + 1}", repo="hive")
    out = world.hive_json("dispatch")
    assert len(out["plan"]) == 1
    assert {s["task"] for s in out["skipped"]} == {"T-0002", "T-0003"}


def test_dispatch_ability(world, fake_cmd):
    """abilities 是能力注册表；needs 缺能力时明确给出「能力缺失」。"""
    world.hive("agent", "add", "--name", "fb", "--mode", "exec",
               "--command", fake_cmd, "--abilities", "code", "docs",
               "--repo", f"hive={world.code_clone('fb')}")
    world.add_task("T-0001", repo="hive", needs=["code"])
    world.add_task("T-0002", repo="hive", needs=["verify"])

    out = world.hive_json("dispatch", "--max-per-repo", "2")
    plan = {p["task"]: p["agent"] for p in out["plan"]}
    assert plan == {"T-0001": "fb"}, out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    assert "能力缺失" in reasons["T-0002"], out


def test_dispatch_concurrency(world, fake_cmd):
    """并发锁的粒度是 paths：paths 重叠的卡默认只允许多 1 张在飞。

    这两张卡都没声明 paths，按「可能改任何地方」处理，所以第二张被拦下。
    """
    world.add_agent("a1", "exec", command=fake_cmd,
                    repos={"hive": world.code_clone("a1")})
    world.add_agent("a2", "exec", command=fake_cmd,
                    repos={"hive": world.code_clone("a2")})
    world.add_task("T-0001", repo="hive")
    world.add_task("T-0002", repo="hive")

    out = world.hive_json("dispatch", "--dry-run")
    assert len(out["plan"]) == 1, out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    # 跳过原因要点名跟谁抢，而不是笼统说「同仓满了」
    assert "T-0001" in reasons["T-0002"] and "paths" in reasons["T-0002"], out

    out = world.hive_json("dispatch", "--max-per-repo", "2")
    assert len(out["plan"]) == 2, out


def test_dispatch_non_hive_rejected(world, fake_cmd):
    """现阶段只派 hive 仓任务，其它仓明确拒绝而不是静默跳过。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", repo="light")

    out = world.hive_json("dispatch", "--dry-run")
    assert out["plan"] == [], out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    assert "非 hive 仓任务暂不派发" in reasons["T-0001"], out
    assert world.card("T-0001")["owner"] is None


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


# --------------------------------------------------------------------------- #
# 合入（land）
# --------------------------------------------------------------------------- #

def _fake_done_card(world, agent, tid, verify=("true",)):
    """用假 agent 跑出一张 status=done 的卡，返回它所在的代码仓 clone。"""
    world.add_task(tid, verify=list(verify))
    world.hive("assign", tid, "--agent", agent)
    world.run_worker(agent)
    row = next(r for r in world.hive_json("status") if r["id"] == tid)
    assert row["result"] == "done", row
    return world.code_clone(agent)


def test_land_merges_and_closes(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    _fake_done_card(world, "fb", "T-0001")

    # dry-run：只预演，不能动 main
    dry = world.hive_json("land", "T-0001")
    assert dry["status"] == "would-merge", dry
    assert dry["strategy"] in ("ff", "merge"), dry
    assert dry["merged"] is False and dry["closed"] is False

    before = sh(["git", "--git-dir", world.code_origin, "rev-parse", "main"]).stdout.strip()

    world.hive("land", "T-0001", "--apply")
    after = sh(["git", "--git-dir", world.code_origin, "rev-parse", "main"]).stdout.strip()
    assert after != before, "main 应该前进了"

    # 真合并之后：卡 close、结果归档、claim/res 分支清掉
    assert world.card("T-0001")["state"] == "done"
    assert (world.hive1 / "results" / "T-0001.json").exists()
    assert world.refs("refs/heads/res/T-0001") == []
    assert world.refs("refs/heads/claim/T-0001") == []
    # 产物真的在 main 里
    blob = sh(["git", "--git-dir", world.code_origin, "show", "main:feature.txt"]).stdout
    assert "made by fb for T-0001" in blob


def test_land_rejects_unqualified_cards(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)

    # 没有结果的卡
    world.add_task("T-0001")
    out = world.hive_json("land", "T-0001")
    assert out["status"] == "blocked" and any("还没有结果" in p for p in out["problems"])

    # 跑完了但 verify 没过的卡：worker 会写 failed 结果，land 必须拦下
    world.add_task("T-0002", verify=["false"])
    world.hive("assign", "T-0002", "--agent", "fb")
    world.run_worker("fb")
    out = world.hive_json("land", "T-0002")
    assert out["status"] == "blocked", out
    assert any(("verify" in p) or ("failed" in p) for p in out["problems"]), out
    assert out["merged"] is False


def test_land_reports_conflicts_without_force(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    _fake_done_card(world, "fb", "T-0001")
    code = world.code_clone("fb")

    # 在 main 上改同一个文件，制造真冲突
    sh(["git", "fetch", "-q", "origin"], cwd=code, env=world.git_env)
    sh(["git", "checkout", "-q", "main"], cwd=code, env=world.git_env)
    (code / "feature.txt").write_text("总分行的内容\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=code, env=world.git_env)
    sh(["git", "commit", "-q", "-m", "conflict maker"], cwd=code, env=world.git_env)
    sh(["git", "push", "-q", "origin", "main"], cwd=code, env=world.git_env)

    out = world.hive_json("land", "T-0001", "--apply")
    assert out["status"] == "conflict", out
    assert "feature.txt" in out["conflicts"]
    # 冲突时绝不 force push：main 仍然是冲突制造者那个 commit
    head = sh(["git", "--git-dir", world.code_origin, "rev-parse", "main"]).stdout.strip()
    msg = sh(["git", "--git-dir", world.code_origin, "log", "-1",
              "--format=%s", head]).stdout.strip()
    assert msg == "conflict maker", msg
    assert world.card("T-0001")["state"] != "done", "冲突的卡不能被 close"


def test_land_all_picks_only_landable(world, fake_cmd):
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    _fake_done_card(world, "fb", "T-0001")
    world.add_task("T-0002")                       # 没有结果
    world.add_task("T-0003", verify=["false"])     # 结果是 failed
    world.hive("assign", "T-0003", "--agent", "fb")
    world.run_worker("fb")

    before = sh(["git", "--git-dir", world.code_origin, "rev-parse", "main"]).stdout.strip()
    plan = world.hive_json("land", "--all")
    assert [p["task"] for p in plan] == ["T-0001"], plan
    assert plan[0]["status"] == "would-merge", plan
    assert sh(["git", "--git-dir", world.code_origin,
               "rev-parse", "main"]).stdout.strip() == before, "dry-run 不能动 main"

    res = world.hive_json("land", "--all", "--apply")
    assert [p["task"] for p in res] == ["T-0001"], res
    assert res[0]["status"] == "landed" and res[0]["closed"], res
    assert world.card("T-0001")["state"] == "done"
    assert world.card("T-0002")["state"] == "ready", "没结果的卡不该被碰"
    assert world.card("T-0003")["state"] == "assigned", "失败的卡不该被碰"


# --------------------------------------------------------------------------- #
# 并发锁的粒度：paths 而不是 repo
# --------------------------------------------------------------------------- #

def test_dispatch_paths_disjoint_parallel(world, fake_cmd):
    """两张改互不相干目录的卡，应该能同时派给两个 agent。"""
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    world.add_agent("fb2", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb2")})
    world.add_task("T-0001", repo="hive", paths=["hive.py"])
    world.add_task("T-0002", repo="hive", paths=["docs/"])

    out = world.hive_json("dispatch")
    plan = {p["task"]: p["agent"] for p in out["plan"]}
    assert set(plan) == {"T-0001", "T-0002"}, out
    assert plan["T-0001"] != plan["T-0002"], "两张无冲突的卡该分给不同 agent"

    # 默认 max-per-repo=1 —— 老逻辑下这里只能派一张，现在两张都派得出去
    assert out["max_per_repo"] == 1, out


def test_dispatch_paths_overlap_blocked(world, fake_cmd):
    """paths 重叠（含父子目录）的第二张卡要被拦下，且说清是跟谁冲突。"""
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    world.add_agent("fb2", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb2")})
    world.add_task("T-0001", repo="hive", paths=["docs/"])
    world.add_task("T-0002", repo="hive", paths=["docs/操作手册.md"])  # 是 docs/ 的子路径

    out = world.hive_json("dispatch")
    assert [p["task"] for p in out["plan"]] == ["T-0001"], out
    reason = {s["task"]: s["reason"] for s in out["skipped"]}["T-0002"]
    assert "T-0001" in reason and "paths" in reason, reason

    # 放宽上限后两张都能派
    out2 = world.hive_json("dispatch", "--max-per-repo", "2")
    assert {p["task"] for p in out2["plan"]} == {"T-0001", "T-0002"}, out2


def test_dispatch_empty_paths_is_conservative(world, fake_cmd):
    """没声明 paths 的卡按「可能改任何地方」处理，与在飞的卡一律冲突。"""
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    world.add_agent("fb2", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb2")})
    world.add_task("T-0001", repo="hive", paths=["docs/"])
    world.add_task("T-0002", repo="hive")  # paths 为空

    out = world.hive_json("dispatch")
    assert [p["task"] for p in out["plan"]] == ["T-0001"], out
    reason = {s["task"]: s["reason"] for s in out["skipped"]}["T-0002"]
    assert "T-0001" in reason, reason


def test_paths_conflict_rules(world):
    """_paths_conflict 的判定规则：空=冲突、父子=冲突、不相干=不冲突。"""
    h = hive.Hive(world.hive1)
    assert h._paths_conflict(["hive.py"], ["hive.py"]) is True
    assert h._paths_conflict(["docs/"], ["docs/操作手册.md"]) is True
    assert h._paths_conflict(["docs/操作手册.md"], ["docs/"]) is True
    assert h._paths_conflict(["hive.py"], ["docs/"]) is False
    assert h._paths_conflict(["docs/"], ["tests/"]) is False
    assert h._paths_conflict([], ["hive.py"]) is True      # 未声明 = 可能改任何地方
    assert h._paths_conflict(["hive.py"], []) is True
    assert h._paths_conflict(["tests/"], ["tests/unit/"]) is True
    assert h._paths_conflict(["tests/"], ["testing/"]) is False  # 前缀不是目录边界


# --------------------------------------------------------------------------- #
# paths 越界检测 + audit
# --------------------------------------------------------------------------- #

def test_out_of_paths_helper():
    """越界判定：白名单内（含子路径）放行，白名单外拦下；没声明 paths 就不约束。"""
    assert hive.out_of_paths(["hive.py"], ["hive.py"]) == []
    assert hive.out_of_paths(["docs/"], ["docs/操作手册.md"]) == []
    assert hive.out_of_paths(["hive.py", "docs/"],
                             ["hive.py", "docs/a.md", "tests/x.py"]) == ["tests/x.py"]
    assert hive.out_of_paths([], ["anything.py"]) == []      # 未声明 = 不约束
    assert hive.out_of_paths(["tests/"], ["testing/a.py"]) == ["testing/a.py"]


def test_worker_records_out_of_paths_but_still_finishes(world, fake_cmd):
    """默认只警告不阻断：越界改动记进结果，任务照常完成。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", paths=["docs/"], verify=["true"])  # 假 agent 会写 feature.txt
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")

    res = json.loads(world.hive("result", "T-0001", "--json").stdout)
    assert res["status"] == "done", res
    assert "feature.txt" in res["out_of_paths"], res
    assert "feature.txt" in res["changed_files"], res
    assert res["declared_paths"] == ["docs/"]


def test_worker_strict_paths_fails_the_task(world, fake_cmd):
    """HIVE_STRICT_PATHS=1 时越界直接判失败，且不跑 verify、不提交。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", paths=["docs/"], verify=["true"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb", env={"HIVE_STRICT_PATHS": "1"})

    res = json.loads(world.hive("result", "T-0001", "--json").stdout)
    assert res["status"] == "failed", res
    assert "越界" in res["error"], res
    assert res["verify"] == [], "严格模式下不该跑验收"
    assert res["out_of_paths"] == ["feature.txt"], res


def test_audit_reports_risk_when_summary_confesses(world, fake_cmd):
    """audit 要能把「summary 自称没跑测试」这类风险点出来。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", paths=["docs/"], verify=["true"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")

    # 把 summary 换成「没能跑测试」的口供
    res_ref = world.hive("result", "T-0001", "--json")
    res = json.loads(res_ref.stdout)
    res["summary"] = "写完了，但本机环境没能跑测试"
    wt = world.root / "light-fb"
    from hive import commit_file_to_ref
    commit_file_to_ref(
        world.hive1, "results/T-0001.json", json.dumps(res, ensure_ascii=False),
        "res/T-0001", "origin/main", "forge", force=True)

    rep = json.loads(world.hive("audit", "T-0001", "--json").stdout)
    assert rep["risk"] == "warn", rep
    assert any("没能跑" in w for w in rep["warnings"]), rep
    assert rep["out_of_paths"] == ["feature.txt"], rep
