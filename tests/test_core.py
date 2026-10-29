"""hive 核心行为测试：分派、体检、统计、领取、验收、归档。"""
from __future__ import annotations

import json
import os
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
    """默认只派 hive 仓；其它仓要被明确拒绝（点名白名单），而不是静默跳过。"""
    world.add_agent("fb", "exec", command=fake_cmd,
                    repos={"light": world.code_clone("fb")})
    world.add_task("T-0001", repo="light")

    out = world.hive_json("dispatch", "--dry-run")
    assert out["plan"] == [], out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    assert "不在派发白名单" in reasons["T-0001"], out
    assert "HIVE_DISPATCH_REPOS" in reasons["T-0001"], out
    assert world.card("T-0001")["owner"] is None


def test_dispatch_other_repo_allowed_by_whitelist(world, fake_cmd):
    """HIVE_DISPATCH_REPOS 显式开白名单后，登记过该仓的 agent 可以领该仓的卡。

    这是「给 light 仓出语言缺陷修复卡」的唯一入口：默认行为不变，
    只有显式开了白名单才跨仓派发。
    """
    world.add_agent("fb", "exec", command=fake_cmd,
                    repos={"light": world.code_clone("fb")})
    world.add_task("T-0001", repo="light")

    p = world.hive("dispatch", "--dry-run", "--json",
                   env={**os.environ, "HIVE_DISPATCH_REPOS": "hive,light"})
    out = json.loads(p.stdout)
    assert len(out["plan"]) == 1, out
    assert out["plan"][0]["task"] == "T-0001", out


def test_dispatch_whitelist_does_not_bypass_repo_registration(world, fake_cmd):
    """开了白名单但 agent 没登记该仓 → 仍然不派（仓不匹配兜底）。"""
    world.add_agent("fb", "exec", command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    world.add_task("T-0001", repo="light")

    p = world.hive("dispatch", "--dry-run", "--json",
                   env={**os.environ, "HIVE_DISPATCH_REPOS": "hive,light"})
    out = json.loads(p.stdout)
    assert out["plan"] == [], out
    reasons = {s["task"]: s["reason"] for s in out["skipped"]}
    assert "仓不匹配" in reasons["T-0001"], out


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
        "paths": ["src/"], "verify": ["python3 -m pytest tests -q"], "depends_on": [],
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


def _push_foreign_commit_to_hive(world, filename: str, content: str) -> None:
    """从 hive2 往协调仓 origin/main 推一个"别人"的提交，让 hive1 落后。"""
    sh(["git", "fetch", "-q", "origin"], cwd=world.hive2, env=world.git_env)
    sh(["git", "checkout", "-q", "main"], cwd=world.hive2, env=world.git_env)
    # hive2 是初始化时 clone 的，之后 hive1 推了不少提交；只 fetch 不 reset 的话
    # 本地 main 还停在旧基线上，commit 完推送必然非快进被拒
    sh(["git", "reset", "--hard", "-q", "origin/main"], cwd=world.hive2,
       env=world.git_env)
    target = world.hive2 / filename
    if target.exists():
        target.write_text(target.read_text(encoding="utf-8") + content,
                          encoding="utf-8")
    else:
        target.write_text(content, encoding="utf-8")
    sh(["git", "add", "-A"], cwd=world.hive2, env=world.git_env)
    sh(["git", "commit", "-q", "-m", f"foreign: {filename}"], cwd=world.hive2,
       env=world.git_env)
    sh(["git", "push", "-q", "origin", "main"], cwd=world.hive2, env=world.git_env)


def test_land_survives_dirty_coordination_tree(world, fake_cmd):
    """协调仓主工作区落后且有未提交改动时，land --apply 必须一次成功（T-0136）。

    复现 T-0133/T-0135：合并在临时 worktree 里能推成，老代码却在 close 的
    sync_main 上抛「不敢自动 reset」——代码合了、卡片没关。未提交改动还必须
    原样保留（ff 只碰真正变化的文件）。
    """
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    _fake_done_card(world, "fb", "T-0001")

    # 1) 别人往协调仓推了一个提交 → hive1 落后 origin/main
    _push_foreign_commit_to_hive(world, "FOREIGN.md", "别人推的\n")
    # 2) 主仓工作区有未提交改动（与传入改动、与 close 的路径都不重叠）
    dirty = world.hive1 / "agents" / "fb.json"
    dirty.write_text(dirty.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    world.hive("land", "T-0001", "--apply")

    # 合并 + 关卡都完成
    assert world.card("T-0001")["state"] == "done"
    assert (world.hive1 / "results" / "T-0001.json").exists()
    blob = sh(["git", "--git-dir", world.code_origin, "show", "main:feature.txt"]).stdout
    assert "made by fb for T-0001" in blob
    # 别人的提交被 ff 进来；未提交改动原样还在
    assert (world.hive1 / "FOREIGN.md").exists()
    head_version = sh(["git", "-C", str(world.hive1), "show",
                       "HEAD:agents/fb.json"]).stdout
    assert dirty.read_text(encoding="utf-8") == head_version + "\n"
    # 注意：porcelain 前导空格是语义位（未暂存），只能 rstrip，不能 strip
    status = sh(["git", "-C", str(world.hive1), "status", "--porcelain",
                 "--untracked-files=no"], env=world.git_env).stdout.rstrip("\n")
    assert status == " M agents/fb.json", status


def test_land_reports_merged_not_closed_when_sync_impossible(world, fake_cmd):
    """未提交改动与传入改动重叠（ff 被 git 拒绝）：不崩、报清楚、不丢改动。"""
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    _fake_done_card(world, "fb", "T-0001")

    # 别人的提交改的正是 agents/fb.json（必须保持 JSON 合法，否则 land 读 agent
    # 配置时直接跳过，根本走不到合并那步）
    sh(["git", "fetch", "-q", "origin"], cwd=world.hive2, env=world.git_env)
    sh(["git", "checkout", "-q", "main"], cwd=world.hive2, env=world.git_env)
    sh(["git", "reset", "--hard", "-q", "origin/main"], cwd=world.hive2,
       env=world.git_env)
    foreign_cfg = json.loads((world.hive2 / "agents" / "fb.json").read_text(encoding="utf-8"))
    foreign_cfg["note"] = "别人加的"
    (world.hive2 / "agents" / "fb.json").write_text(
        json.dumps(foreign_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=world.hive2, env=world.git_env)
    sh(["git", "commit", "-q", "-m", "foreign: agents/fb.json"], cwd=world.hive2,
       env=world.git_env)
    sh(["git", "push", "-q", "origin", "main"], cwd=world.hive2, env=world.git_env)
    # hive1 也改了同一个文件且未提交（同样保持合法）→ ff 必然被 git 拒绝
    dirty = world.hive1 / "agents" / "fb.json"
    my_cfg = json.loads(dirty.read_text(encoding="utf-8"))
    my_cfg["my_uncommitted"] = True
    dirty.write_text(json.dumps(my_cfg, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")

    out = world.hive_json("land", "T-0001", "--apply")

    # 不崩（JSON 正常返回），状态说清"合入了但没关成"
    assert out["status"] == "merged-not-closed", out
    assert out["merged"] is True and out["closed"] is False
    assert any("合并已推入" in p and "hive close T-0001" in p
               for p in out["problems"]), out["problems"]
    assert any("agents/fb.json" in p for p in out["problems"]), out["problems"]
    # 卡片确实没关；代码合并确实已在主线
    assert world.card("T-0001")["state"] != "done"
    blob = sh(["git", "--git-dir", world.code_origin, "show", "main:feature.txt"]).stdout
    assert "made by fb for T-0001" in blob
    # 我的未提交改动没被吞：还是我改的版本，别人的 note 没混进来
    text = dirty.read_text(encoding="utf-8")
    assert '"my_uncommitted"' in text and '"note"' not in text, text
    status = sh(["git", "-C", str(world.hive1), "status", "--porcelain",
                 "--untracked-files=no"], env=world.git_env).stdout.rstrip("\n")
    assert status == " M agents/fb.json", status


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


def test_audit_catches_real_confession_wording(world, fake_cmd):
    """T-0101 的真实措辞是「未能启动…未得到实际测试结果」—— 固定词表追不上，用正则。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", paths=["docs/"], verify=["true"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")

    from hive import commit_file_to_ref
    for summary in ("本机 sandbox 后端不可用，pytest 未能启动，未得到实际测试结果",
                    "环境有问题，没跑测试就交了",
                    "无法验证，只能靠静态核对"):
        res = json.loads(world.hive("result", "T-0001", "--json").stdout)
        res["summary"] = summary
        commit_file_to_ref(world.hive1, "results/T-0001.json",
                           json.dumps(res, ensure_ascii=False),
                           "res/T-0001", "origin/main", "forge", force=True)
        rep = json.loads(world.hive("audit", "T-0001", "--json").stdout)
        assert rep["risk"] == "warn", (summary, rep)
        assert any("自称" in w for w in rep["warnings"]), (summary, rep)

    # 正常 summary 不该被误报
    res = json.loads(world.hive("result", "T-0001", "--json").stdout)
    res["summary"] = "改完了，pytest 全绿，17 个用例通过"
    commit_file_to_ref(world.hive1, "results/T-0001.json",
                       json.dumps(res, ensure_ascii=False),
                       "res/T-0001", "origin/main", "forge", force=True)
    rep = json.loads(world.hive("audit", "T-0001", "--json").stdout)
    assert not any("自称" in w for w in rep["warnings"]), rep


# --------------------------------------------------------------------------- #
# 总控写命令的提交范围收口
# --------------------------------------------------------------------------- #

def _head_stat(world) -> str:
    return sh(["git", "-C", str(world.hive1), "show", "--stat", "--format=",
               "HEAD"]).stdout


def _porcelain(world) -> str:
    return sh(["git", "-C", str(world.hive1), "status", "--porcelain"]).stdout


def test_task_new_does_not_sweep_unrelated_changes(world):
    """建卡只提交那张卡 —— 总控工作区里别的改动不许被卷进来。

    真实事故：mcp_server.py（705 行）等一批改动还没提交时跑了一次 `task new`，
    全被塞进「task: add T-0110」提交推了上去。
    """
    (world.hive1 / "notes.tmp").write_text("总控手头的活\n", encoding="utf-8")

    world.add_task("T-0001")

    stat = _head_stat(world)
    assert "T-0001" in stat, stat
    assert "notes.tmp" not in stat, f"无关的改动被卷进来了：{stat}"
    assert "notes.tmp" in _porcelain(world), "无关改动应该原样留在工作区"


def test_close_commits_only_card_and_result(world, fake_cmd):
    """close 只提交任务卡和归档结果。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")

    (world.hive1 / "scratch.txt").write_text("别卷我\n", encoding="utf-8")
    world.hive("close", "T-0001", "--note", "ok")

    stat = _head_stat(world)
    assert "tasks/T-0001.md" in stat, stat
    assert "results/T-0001.json" in stat, stat
    assert "scratch.txt" not in stat, f"scratch.txt 被卷进来了：{stat}"
    assert "scratch.txt" in _porcelain(world)


def test_assign_and_take_only_touch_card_and_inbox(world, fake_cmd):
    """assign / take 只动任务卡和对应的 inbox 信号。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001")
    (world.hive1 / "wip.md").write_text("写到一半的文档\n", encoding="utf-8")

    world.hive("assign", "T-0001", "--agent", "fb")
    stat = _head_stat(world)
    assert "tasks/T-0001.md" in stat and "inbox/fb/T-0001.md" in stat, stat
    assert "wip.md" not in stat, stat
    assert "wip.md" in _porcelain(world), "wip.md 应该还在工作区里"


def test_commit_and_push_without_paths_keeps_old_behaviour(world):
    """向后兼容：不传 paths 时仍然是全量提交（但会打告警）。"""
    (world.hive1 / "legacy.txt").write_text("老行为\n", encoding="utf-8")

    h = hive.Hive(world.hive1)
    assert h.commit_and_push("legacy: 全量提交") is True

    stat = _head_stat(world)
    assert "legacy.txt" in stat, stat
    assert "legacy.txt" not in _porcelain(world)


def test_sync_main_keeps_untracked_files(world):
    """同步时未跟踪的文件不该被当成「脏」—— reset --hard 不会动它们。

    总控工作区里有个草稿或编辑器 lock 文件是常态，不能因此让 land → close 卡死。
    """
    (world.hive1 / "scratch.txt").write_text("总控的草稿\n", encoding="utf-8")

    # 从另一个 clone 推一版，让 hive1 落后于 origin/main
    (world.hive2 / "pushed-elsewhere.md").write_text("别的机器推的\n", encoding="utf-8")
    for args in (["add", "-A"],
                 ["commit", "-q", "-m", "from elsewhere"],
                 ["push", "-q", "origin", "main"]):
        sh(["git", "-C", str(world.hive2)] + args, env=world.git_env)

    h = hive.Hive(world.hive1)
    h.sync_main()  # 不该抛错

    assert (world.hive1 / "scratch.txt").exists(), "未跟踪的文件不该被 reset 掉"
    assert (world.hive1 / "pushed-elsewhere.md").exists(), "应该已经跟上了 origin/main"


# --------------------------------------------------------------------------- #
# 状态机：blocked / conflict / needs_review
# --------------------------------------------------------------------------- #

def test_block_skips_dispatch_and_worker(world, fake_cmd):
    """blocked 的卡：dispatch 不派、worker 不接。"""
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd,
                    repos={"hive": world.code_clone("fb")})
    world.add_task("T-0001", repo="hive", needs=["freebsd"], paths=["docs/"])

    out = world.hive_json("dispatch")
    assert [p["task"] for p in out["plan"]] == ["T-0001"], out
    world.hive("dispatch", "--apply")
    world.hive("block", "T-0001", "--reason", "等 Windows 真机")
    assert world.card("T-0001")["state"] == "blocked"
    assert world.card("T-0001")["owner"] is None, "挂起要收回 owner"

    # 重新入队也派不出去（blocked 不在 ready 集合里）
    out2 = world.hive_json("dispatch", "--agent", "fb")
    assert out2["plan"] == [], out2
    reasons = {s["task"]: s["reason"] for s in out2["skipped"]}
    assert "T-0001" not in reasons or "blocked" in reasons["T-0001"], reasons

    # worker 也不接它（inbox 里有也不跑）
    row = next(r for r in world.hive_json("status") if r["id"] == "T-0001")
    assert row["state"] == "blocked", row

    world.hive("unblock", "T-0001", "--reason", "环境好了")
    assert world.card("T-0001")["state"] == "ready", "没 owner 就该回 ready"
    out3 = world.hive_json("dispatch", "--agent", "fb")
    assert [p["task"] for p in out3["plan"]] == ["T-0001"], out3


def test_doctor_flags_conflict_as_error(world, fake_cmd):
    """conflict 是必须尽快解决的状态，doctor 要当错误报。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001")
    world.hive("assign", "T-0001", "--agent", "fb")
    world.hive("close", "T-0001")  # 先混个 close 免得空状态干扰
    # 手工把一张卡改成 conflict
    world.hive("retry", "T-0001", "--agent", "fb")
    p = world.hive1 / "tasks" / "T-0001.md"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "state: assigned", "state: conflict", 1), encoding="utf-8")
    sh(["git", "-C", str(world.hive1), "add", "-A"], env=world.git_env)
    sh(["git", "-C", str(world.hive1), "commit", "-q", "-m", "make conflict"],
       env=world.git_env)
    sh(["git", "-C", str(world.hive1), "push", "-q", "origin", "main"],
       env=world.git_env)

    out = world.hive("doctor", check=False)
    assert out.returncode != 0, "有 conflict 卡时 doctor 必须失败"
    assert "T-0001" in (out.stdout + out.stderr)


def test_states_are_centralised():
    """状态机要有集中的常量，别散落在各处字符串比较。"""
    assert "ready" in hive.STATES and "done" in hive.STATES
    assert set(hive.HANDOFF_STATES) == {"needs_review", "conflict", "blocked"}
    assert set(hive.IN_FLIGHT_STATES) == {"assigned", "running"}
    assert hive.ERROR_STATES == ("conflict",)


# --------------------------------------------------------------------------- #
# 任务卡草稿：agent 出题、人确认后转正
# --------------------------------------------------------------------------- #

def test_draft_lint_catches_unworkable_cards(world):
    """草稿校验要拦住「永远派不出去」和「没有验收」的卡。"""
    world.add_agent("fb", "exec", command="true",
                    repos={"hive": world.code_clone("fb")})
    h = hive.Hive(world.hive1)
    cards = h.cards()

    ok_card = {"title": "写文档", "repo": "hive", "verify": ["python3 -m pytest tests -q"],
               "needs": [], "paths": ["docs/"]}
    errors, warns = hive.draft_lint(ok_card, "T-0001", h.agents(), cards)
    assert errors == [] and warns == [], (errors, warns)

    no_verify = {"title": "x", "repo": "hive", "verify": []}
    errors, _ = hive.draft_lint(no_verify, "T-0002", h.agents(), cards)
    assert any("verify" in e for e in errors), errors

    unknown_repo = {"title": "x", "repo": "light", "verify": ["python3 -m pytest tests -q"]}
    errors, _ = hive.draft_lint(unknown_repo, "T-0003", h.agents(), cards)
    assert any("永远派不出去" in e for e in errors), errors

    nobody_can = {"title": "x", "repo": "hive", "verify": ["python3 -m pytest tests -q"],
                  "needs": ["windows"]}
    errors, _ = hive.draft_lint(nobody_can, "T-0004", h.agents(), cards)
    assert any("僵尸卡" in e for e in errors), errors

    bad_dep = {"title": "x", "repo": "hive", "verify": ["python3 -m pytest tests -q"],
               "depends_on": ["T-9999"]}
    errors, _ = hive.draft_lint(bad_dep, "T-0005", h.agents(), cards)
    assert any("T-9999" in e for e in errors), errors

    no_paths = {"title": "x", "repo": "hive", "verify": ["python3 -m pytest tests -q"]}
    errors, warns = hive.draft_lint(no_paths, "T-0006", h.agents(), cards)
    assert errors == [] and any("paths" in w for w in warns), (errors, warns)


def test_draft_adopt_validates_then_queues(world):
    """草稿要过 lint 才能转正；转正后进 tasks/，草稿本身撤掉。"""
    world.add_agent("fb", "exec", command="true",
                    repos={"hive": world.code_clone("fb")})
    ddir = world.hive1 / "drafts"
    ddir.mkdir(parents=True, exist_ok=True)

    # 不合格的草稿：没有 verify
    bad = ddir / "bad.md"
    bad.write_text("""---
id: T-0001
title: 没写验收的卡
repo: hive
state: draft
needs: []
paths:
verify:
---
## 背景
测试
""", encoding="utf-8")
    sh(["git", "-C", str(world.hive1), "add", "-A"], env=world.git_env)
    sh(["git", "-C", str(world.hive1), "commit", "-q", "-m", "bad draft"],
       env=world.git_env)
    sh(["git", "-C", str(world.hive1), "push", "-q", "origin", "main"],
       env=world.git_env)

    p = world.hive("draft", "adopt", "bad.md", check=False)
    assert p.returncode != 0, "没有 verify 的草稿不该转正"
    assert not (world.hive1 / "tasks" / "T-0001.md").exists()

    # 合格的草稿
    good = ddir / "good.md"
    good.write_text("""---
id: T-0001
title: 草稿转正的卡
repo: hive
state: draft
needs: []
paths:
  - docs/
verify:
  - python3 -m pytest tests -q --tb=short
depends_on: []
---
## 背景

agent 出的题。
""", encoding="utf-8")
    sh(["git", "-C", str(world.hive1), "add", "-A"], env=world.git_env)
    sh(["git", "-C", str(world.hive1), "commit", "-q", "-m", "good draft"],
       env=world.git_env)
    sh(["git", "-C", str(world.hive1), "push", "-q", "origin", "main"],
       env=world.git_env)

    world.hive("draft", "lint", "good.md")
    world.hive("draft", "adopt", "good.md")

    card = world.card("T-0001")
    assert card["state"] == "ready", card
    assert card["title"] == "草稿转正的卡"
    assert card["verify"] == ["python3 -m pytest tests -q --tb=short"]
    assert not good.exists(), "转正后草稿要撤掉"
    # 转正后立刻能被 dispatch 看见
    out = world.hive_json("dispatch", "--agent", "fb")
    assert [x["task"] for x in out["plan"]] == ["T-0001"], out


def test_draft_new_generates_template(world):
    """`draft new` 生成的模板要能直接被 agent 填写。"""
    world.add_agent("fb", "exec", command="true",
                    repos={"hive": world.code_clone("fb")})
    world.hive("draft", "new", "T-0001", "--repo", "hive")
    ddir = world.hive1 / "drafts"
    files = sorted(ddir.glob("*.md"))
    assert len(files) == 1, files
    data, body = hive.parse_card(files[0])
    assert data["repo"] == "hive" and data["state"] == "draft"
    for sec in ("背景", "目标", "红线", "验收"):
        assert sec in body, f"模板缺「{sec}」段"
    # 空模板过不了 lint —— 逼着填写
    errors, _ = hive.draft_lint(data, "T-0001", hive.Hive(world.hive1).agents(), {})
    assert errors, "空模板应该有 error"


# --------------------------------------------------------------------------- #
# 复盘摘要（digest）
# --------------------------------------------------------------------------- #

def test_digest_groups_failures_and_finds_slow_cards(world, fake_cmd):
    """digest 要把失败归因、慢卡、反复重跑的卡都点出来。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    # 一张成功的
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")
    # 一张 verify 挂掉的
    world.add_task("T-0002", verify=["false"])
    world.hive("assign", "T-0002", "--agent", "fb")
    world.run_worker("fb")

    data = hive.Hive(world.hive1).digest_data(days=0)
    assert data["summary"]["done"] == 1, data
    assert data["summary"]["failed"] == 1, data
    assert data["summary"]["pass_rate"] == 50.0, data
    assert "T-0002" in data["failure_reasons"].get("verify 未通过", []), data
    assert data["slowest"], data
    assert data["by_agent"]["fb"]["done"] == 1, data
    assert data["by_agent"]["fb"]["failed"] == 1, data


def test_digest_flags_repeated_retries(world, fake_cmd):
    """同一张卡出现多次结果 = 第一版验收就没过。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", verify=["false"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")
    world.hive("retry", "T-0001", "--agent", "fb")
    world.run_worker("fb")

    data = hive.Hive(world.hive1).digest_data(days=0)
    assert "T-0001" in data["retried"], data


def test_digest_is_read_only_and_writes_out(world, fake_cmd):
    """digest 是只读的；--out 只写普通文件，不推分支不动卡。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")

    out_file = world.root / "digest.md"
    h = hive.Hive(world.hive1)
    before = sh(["git", "-C", str(world.hive1), "rev-parse", "HEAD"]).stdout.strip()
    h.cmd_digest(days=0, out=str(out_file))
    assert sh(["git", "-C", str(world.hive1), "rev-parse",
               "HEAD"]).stdout.strip() == before, "digest 自己不该推提交"
    text = out_file.read_text(encoding="utf-8")
    assert "复盘摘要" in text, text          # 总在
    assert "失败归因" not in text, text      # 没有失败就不该出现这一节
    # 有失败时才出现失败归因
    world.add_task("T-0002", verify=["false"])
    world.hive("assign", "T-0002", "--agent", "fb")
    world.run_worker("fb")
    hive.Hive(world.hive1).cmd_digest(days=0, out=str(out_file))
    assert "失败归因" in out_file.read_text(encoding="utf-8")
    # 上面建卡/分派确实推了提交，但那不是 digest 干的


def test_changed_files_handles_non_ascii_names(world):
    """中文文件名不能被 git 的八进制转义误判成越界（core.quotePath）。

    T-0121 提交时自己被这条咬过一次：改动只有 docs/操作手册.md（就在 paths 里），
    却报「越界改动 1 个文件」—— git 把中文路径转义成了 \346\223\245...。
    """
    repo = world.code_clone("fb")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "操作手册.md").write_text("改了一行\n", encoding="utf-8")

    files = hive.changed_files(repo)
    assert "docs/操作手册.md" in files, files
    assert hive.out_of_paths(["docs/操作手册.md"], files) == [], files
    # 白名单外的一样能抓出来
    (repo / "越界文件.txt").write_text("x\n", encoding="utf-8")
    files2 = hive.changed_files(repo)
    assert hive.out_of_paths(["docs/"], files2) == ["越界文件.txt"], files2


# --------------------------------------------------------------------------- #
# verify 强度分级
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd,expected", [
    ("python3 -m pytest tests -q --tb=short", "strong"),
    ("python -m pytest -q", "strong"),
    ("npm test", "strong"),
    ("go test ./...", "strong"),
    ("cargo test", "strong"),
    ("make test", "strong"),
    ("make check", "strong"),
    ("python3 -m unittest discover", "strong"),
    # 弱：只查痕迹
    ("test -f docs/lessons.md", "weak"),
    ("grep -q abilities agents/fb88-dsh.json", "weak"),
    ("which python3", "weak"),
    ("echo ok", "weak"),
    ("", "weak"),
    # 混合管线按最弱的一环算 —— T-0101 那条就是这形状
    ("grep -q abilities agents/fb88-dsh.json && grep -q 同仓 hive.py "
     "&& python3 -m pytest tests -q", "weak"),
    ("cat foo.txt && python3 -m pytest", "weak"),
    # 不是测试框架的命令一律算弱（保守）
    ("python3 -c \"import sys; sys.exit(0)\"", "weak"),
])
def test_verify_strength(cmd, expected):
    assert hive._verify_strength(cmd) == expected, cmd


def test_verify_verdict_levels():
    """三种结论：全绿且有 strong / 全绿但只有 weak / 有红。"""
    ok = [{"cmd": "python3 -m pytest tests -q", "code": 0}]
    assert hive._verify_verdict(ok)["verdict"] == "strong-pass"

    only_weak = [{"cmd": "test -f docs/lessons.md", "code": 0},
                 {"cmd": "grep -q foo hive.py", "code": 0}]
    v = hive._verify_verdict(only_weak)
    assert v["verdict"] == "weak-pass"
    assert v["has_strong"] is False

    mixed = [{"cmd": "test -f x", "code": 0},
             {"cmd": "python3 -m pytest tests -q", "code": 1}]
    assert hive._verify_verdict(mixed)["verdict"] == "fail"


def test_result_shows_strength(world, fake_cmd):
    """`hive result` 要把强弱标出来；弱验收要有显式提醒。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    # 只有弱验收的卡
    world.add_task("T-0001", verify=["test -f feature.txt"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")

    res = json.loads(world.hive("result", "T-0001", "--json").stdout)
    assert res["verify_strength"] == "weak-pass", res
    assert res["verify_entries"][0]["strength"] == "weak", res

    # 人类可读输出里也要提醒
    out = world.hive("result", "T-0001")
    assert "weak-pass" in out.stdout
    assert "成果没有被真正执行验证" in out.stdout, out.stdout


def test_doctor_flags_weak_only_cards(world, fake_cmd):
    """只有弱验收的卡要被 doctor 点名。"""
    world.add_agent("fb", "exec", command=fake_cmd)
    world.add_task("T-0001", verify=["test -f feature.txt"])
    world.hive("assign", "T-0001", "--agent", "fb")
    world.run_worker("fb")

    out = world.hive("doctor", check=False)
    text = out.stdout + out.stderr
    assert "T-0001" in text and "弱验收" in text, text


def test_draft_lint_requires_strong_verify(world):
    """草稿没有强验收就报 error，挡转正。"""
    from hive import draft_lint
    agents = {"fb": {"repos": {"hive": "/tmp"}, "abilities": ["code"]}}
    weak = {"title": "t", "repo": "hive", "paths": ["hive.py"],
            "verify": ["test -f hive.py"], "needs": [], "depends_on": []}
    errors, _ = draft_lint(weak, "T-0001", agents, {})
    assert any("强验收" in e for e in errors), errors

    strong = dict(weak, verify=["python3 -m pytest tests -q"])
    errors2, _ = draft_lint(strong, "T-0001", agents, {})
    assert not any("强验收" in e for e in errors2), errors2
