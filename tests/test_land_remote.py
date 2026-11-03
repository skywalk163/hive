"""land 的远端选择：分支不在 origin 上时也能合入（T-0238 现场）。

背景：不同机器上的「同一个仓」远端名不一样 —— 主控机的 origin 是内网 Gitea，
Windows agent 的克隆里 origin 可能就是 gitcode。land 早期把 origin 写死，于是
分支明明推上去了、却报「代码仓里没有 origin/task/<tid> 分支」，人只能手工 merge。

验收：
1. `hive land --remote <name>`：从指定远端取分支、往该远端推 main；
2. 不给 --remote 时自动探测（origin → gitcode → github → 其它），并报出用了哪个；
3. 远端名打错 → 明确报错并列出真实远端，绝不静默回落 origin；
4. `code_remotes()` 的顺序稳定（origin 优先）。
"""
import json

from conftest import sh


def _fake_done_card(world, agent, tid, verify=("true",)):
    """用假 agent 跑出一张 status=done 的卡（分支推到 origin）。"""
    world.add_task(tid, verify=list(verify))
    world.hive("assign", tid, "--agent", agent)
    world.run_worker(agent)
    row = next(r for r in world.hive_json("status") if r["id"] == tid)
    assert row["result"] == "done", row
    return world.code_clone(agent)


def _move_branch_to_other_remote(world, clone, tid, remote="gitcode"):
    """把 task 分支与 main 搬到第二个裸仓远端，并从 origin 删掉分支。

    模拟真实事故现场：agent 把 task/<tid> 推到了 gitcode，主控机的 origin 上
    压根没有这条 ref（连 fetch --prune 之后也没有）。
    """
    other = world.root / f"{remote}.git"
    sh(["git", "init", "--bare", "-b", "main", other])
    sh(["git", "-C", clone, "remote", "add", remote, other])
    sh(["git", "-C", clone, "push", "-q", remote,
        "refs/remotes/origin/main:refs/heads/main"], env=world.git_env)
    sh(["git", "-C", clone, "push", "-q", remote,
        f"refs/remotes/origin/task/{tid}:refs/heads/task/{tid}"], env=world.git_env)
    # origin 上的分支删掉：land 若还写死 origin，就会「找不到分支」
    sh(["git", "--git-dir", world.code_origin, "update-ref", "-d",
        f"refs/heads/task/{tid}"])
    return other


def _ref(bare, name):
    return sh(["git", "--git-dir", bare, "rev-parse", name]).stdout.strip()


def test_land_auto_detects_remote_holding_branch(world, fake_cmd):
    """不给 --remote 也要能合入：自动发现分支在 gitcode 上。"""
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    clone = _fake_done_card(world, "fb", "T-0001")
    other = _move_branch_to_other_remote(world, clone, "T-0001")

    origin_before = _ref(world.code_origin, "main")
    other_before = _ref(other, "main")

    dry = world.hive_json("land", "T-0001")
    assert dry["status"] == "would-merge", dry
    assert dry["remote"] == "gitcode", dry

    world.hive("land", "T-0001", "--apply")

    # 推的是分支所在的远端，不是 origin
    assert _ref(other, "main") != other_before, "gitcode 的 main 应该前进"
    assert _ref(world.code_origin, "main") == origin_before, \
        "origin 上没有这条分支，不该被推"
    blob = sh(["git", "--git-dir", other, "show", "main:feature.txt"]).stdout
    assert "made by fb for T-0001" in blob
    assert world.card("T-0001")["state"] == "done"


def test_land_explicit_remote_is_honored_and_typo_is_fatal(world, fake_cmd):
    """--remote 显式指定必须照做；写错的远端名要报错，不能悄悄回落 origin。"""
    world.add_agent("fb", "exec", caps=["freebsd"], command=fake_cmd)
    clone = _fake_done_card(world, "fb", "T-0001")
    other = _move_branch_to_other_remote(world, clone, "T-0001")

    # 远端名打错：明确失败，并列出真实远端
    p = world.hive("land", "T-0001", "--remote", "gtcode", "--json", check=False)
    assert p.returncode == 0 or p.returncode == 1  # CLI 不因计划失败改退出码
    plan = json.loads(p.stdout)
    assert plan["status"] == "blocked", plan
    assert any("gtcode" in x and "gitcode" in x for x in plan["problems"]), plan

    # 正确指定：从 gitcode 合入
    other_before = _ref(other, "main")
    out = world.hive_json("land", "T-0001", "--remote", "gitcode", "--apply")
    assert out["status"] == "landed" and out["remote"] == "gitcode", out
    assert _ref(other, "main") != other_before
    # 只推了一个远端，另一个要提示怎么对齐
    assert any("origin" in h for h in out.get("hints") or []), out


def test_code_remotes_prefers_origin_and_is_stable():
    """远端顺序固定：origin 先，其余字母序（探测与日志都靠这个顺序）。"""
    import subprocess
    import tempfile
    from pathlib import Path

    from hive import code_remotes

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        sh(["git", "init", "-q", str(repo)])
        for name, url in (("upstream", "u"), ("gitcode", "g"), ("origin", "o")):
            subprocess.run(["git", "-C", str(repo), "remote", "add", name, url],
                           check=True, capture_output=True)
        assert code_remotes(repo) == ["origin", "gitcode", "upstream"]
