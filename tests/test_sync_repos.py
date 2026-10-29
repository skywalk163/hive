"""T-0193：`hive sync-repos` 代码仓自动同步 + orchestrate 加 sync 阶段。

覆盖：
- 已最新不报错（up-to-date）；
- 能快进（fast-forwarded，落后仓被拉到 origin 主线）；
- 分叉时跳过、本地提交不丢（diverged，绝不 reset --hard）；
- 未提交改动在成功快进后原样保留（复用 _sync_for_commit 安全边界）；
- --repo 只同步指定仓（其余仓不动）；
- --json 输出结构化结果；
- --repo 未知名字明确报错；
- orchestrate 一轮内会调用 sync 阶段；
- orchestrate 的 sync 失败不中断整轮循环。
"""
from __future__ import annotations

import hive

from conftest import sh


def _push_commit(world, clone, filename: str, content: str, msg: str) -> str:
    """从 clone 往代码仓 origin/main 推一个提交，返回新 HEAD。"""
    (clone / filename).write_text(content, encoding="utf-8")
    sh(["git", "add", "-A"], cwd=clone, env=world.git_env)
    sh(["git", "commit", "-q", "-m", msg], cwd=clone, env=world.git_env)
    sh(["git", "push", "-q", "origin", "main"], cwd=clone, env=world.git_env)
    return sh(["git", "-C", str(clone), "rev-parse", "HEAD"]).stdout.strip()


def _head(world, clone) -> str:
    return sh(["git", "-C", str(clone), "rev-parse", "HEAD"]).stdout.strip()


# --------------------------------------------------------------------------- #
# 基本同步
# --------------------------------------------------------------------------- #
def test_sync_repos_up_to_date(world):
    """已最新：跑 sync-repos 不报错，输出「已最新」。"""
    world.add_agent("fb", mode="manual")
    p = world.hive("sync-repos")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "已最新" in p.stdout


def test_sync_repos_fast_forwards(world):
    """落后仓：fetch + merge --ff-only 把 main 快进到 origin 主线。"""
    world.add_agent("fb", mode="manual")
    repo = world.code_clone("fb")
    before = _head(world, repo)

    other = world.code_clone("other")
    _push_commit(world, other, "feature.txt", "sync\n", "sync commit")

    p = world.hive("sync-repos")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "快进到" in p.stdout

    after = _head(world, repo)
    assert after != before, "main 应该前进了"
    assert (repo / "feature.txt").exists()


def test_sync_repos_diverged_skips_and_preserves_local(world):
    """分叉仓被跳过、绝不 reset --hard：本地提交原样保留，主线不动。"""
    world.add_agent("fb", mode="manual")
    repo = world.code_clone("fb")

    # 远端前进
    other = world.code_clone("other")
    foreign_head = _push_commit(world, other, "foreign.txt", "foreign\n",
                                "foreign commit")

    # 本地再提交一个（不推送）→ 与远端分叉
    (repo / "local.txt").write_text("local\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=repo, env=world.git_env)
    sh(["git", "commit", "-q", "-m", "local commit"], cwd=repo, env=world.git_env)
    local_head = _head(world, repo)
    assert local_head != foreign_head

    p = world.hive("sync-repos")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "落后但无法快进" in p.stdout

    # 本地提交没被吞，主线也没被强推（分叉仓不该被快进到远端提交）
    after = _head(world, repo)
    assert after == local_head
    assert (repo / "local.txt").exists()
    assert not (repo / "foreign.txt").exists(), "分叉时不该被快进到远端提交"


def test_sync_repos_preserves_uncommitted_changes(world):
    """未提交改动与传入提交不重叠：成功快进后，未提交改动原样保留。"""
    world.add_agent("fb", mode="manual")
    repo = world.code_clone("fb")

    # 未提交改动（git 没 add）——只改 src/main.py
    (repo / "src" / "main.py").write_text("print('local edit')\n", encoding="utf-8")

    # 远端提交只碰 README.md，与未提交改动不重叠
    other = world.code_clone("other")
    _push_commit(world, other, "README.md", "# light v2\n", "foreign readme")

    p = world.hive("sync-repos")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "快进到" in p.stdout

    # 未提交改动还在，远端提交也进来了
    assert (repo / "src" / "main.py").read_text(encoding="utf-8") == "print('local edit')\n"
    assert "v2" in (repo / "README.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# --repo 过滤 / --json
# --------------------------------------------------------------------------- #
def test_sync_repos_repo_filter(world):
    """--repo 只同步指定仓，其余仓不被碰。"""
    world.add_agent("fb", mode="manual", repos={"light": world.code_clone("fb")})
    world.add_agent("fb2", mode="manual", repos={"light2": world.code_clone("fb2")})

    other = world.code_clone("other")
    _push_commit(world, other, "feature.txt", "sync\n", "sync commit")

    p = world.hive("sync-repos", "--repo", "light")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "light" in p.stdout
    assert "light2" not in p.stdout, "未指定仓不该出现在输出里"

    # 指定仓被快进；未指定仓保持落后（两个 clone 同源，只有被同步的才前进）
    assert (world.code_clone("fb") / "feature.txt").exists()
    assert not (world.code_clone("fb2") / "feature.txt").exists()


def test_sync_repos_json_structure(world):
    """--json 输出结构化结果，字段齐全。"""
    world.add_agent("fb", mode="manual")
    out = world.hive_json("sync-repos")
    assert isinstance(out, dict) and "results" in out, out
    assert len(out["results"]) >= 1
    r = out["results"][0]
    assert r["repo"] == "light"
    assert r["status"] == "up-to-date"
    assert "path" in r


def test_sync_repos_unknown_repo(world):
    """--repo 未知名字明确报错（退出码非 0）。"""
    world.add_agent("fb", mode="manual")
    p = world.hive("sync-repos", "--repo", "nonexistent", check=False)
    assert p.returncode != 0
    assert "没有登记代码仓" in p.stdout + p.stderr


# --------------------------------------------------------------------------- #
# orchestrate 集成
# --------------------------------------------------------------------------- #
def test_orchestrate_calls_sync(world, monkeypatch):
    """orchestrate 每轮在 doctor 之前调用 sync 阶段（quiet 模式）。"""
    world.add_agent("fb", mode="manual")
    calls = []

    def fake_sync(self, repo=None, as_json=False, quiet=False):
        calls.append({"repo": repo, "as_json": as_json, "quiet": quiet})
        return {"results": []}

    monkeypatch.setattr(hive.Hive, "cmd_sync_repos", fake_sync)

    h = hive.Hive(world.hive1)
    # 只留 sync 阶段，其余全关
    h.cmd_orchestrate(once=True, dispatch=False, land=False, watchdog=False,
                      doctor=False, sync=True)

    assert len(calls) == 1, calls
    assert calls[0]["quiet"] is True


def test_orchestrate_sync_failure_does_not_interrupt(world, monkeypatch):
    """sync 阶段抛 HiveError 不中断整轮循环，orchestrate 仍正常返回。"""
    world.add_agent("fb", mode="manual")

    def boom(self, repo=None, as_json=False, quiet=False):
        raise hive.HiveError("sync boom")

    monkeypatch.setattr(hive.Hive, "cmd_sync_repos", boom)

    h = hive.Hive(world.hive1)
    # 不该向上抛：tick 内 catch HiveError 并继续
    h.cmd_orchestrate(once=True, dispatch=False, land=False, watchdog=False,
                      doctor=False, sync=True)


def test_orchestrate_no_sync_skips_stage(world, monkeypatch):
    """--no-sync（sync=False）时不调用 sync 阶段。"""
    world.add_agent("fb", mode="manual")
    calls = []

    def fake_sync(self, repo=None, as_json=False, quiet=False):
        calls.append(True)
        return {"results": []}

    monkeypatch.setattr(hive.Hive, "cmd_sync_repos", fake_sync)

    h = hive.Hive(world.hive1)
    h.cmd_orchestrate(once=True, dispatch=False, land=False, watchdog=False,
                      doctor=False, sync=False)
    assert calls == []
