"""hive_land 冲突返回值的措辞必须和实际 apply 一致（T-0128）。

背景：`hive_land` 传了 apply=true 仍撞冲突时，旧代码固定回「默认只预演，什么都没动」。
LLM 总控靠读这条 message 判断自己处在什么模式，这句话会把它误导成"我还在 dry-run"。
"""
from __future__ import annotations

from pathlib import Path

from conftest import sh

from mcp_server import HiveMcp


def _make_conflict(world, fake_cmd):
    """造一张 done 卡，再让代码仓 main 与它的 task 分支真实冲突。"""
    world.add_agent("a1", "exec", caps=["python"], command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "a1")
    world.run_worker("a1")
    code = world.code_clone("a1")
    sh(["git", "fetch", "-q", "origin"], cwd=code, env=world.git_env)
    sh(["git", "checkout", "-q", "main"], cwd=code, env=world.git_env)
    (code / "feature.txt").write_text("总控侧的内容\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=code, env=world.git_env)
    sh(["git", "commit", "-q", "-m", "conflict maker"], cwd=code, env=world.git_env)
    sh(["git", "push", "-q", "origin", "main"], cwd=code, env=world.git_env)


def _controller(world):
    world.add_agent("ctl", "exec", command="true")
    return HiveMcp(Path(world.hive2), "ctl", role="controller")


def _land(impl: HiveMcp, **args) -> dict:
    return HiveMcp.HANDLERS["hive_land"](impl, args)


def test_land_conflict_message_says_applied_when_apply_true(world, fake_cmd):
    """传了 apply=true 撞冲突：必须说清"申请了真合入但因冲突没动"，不能谎称在预演。"""
    _make_conflict(world, fake_cmd)
    impl = _controller(world)

    env = _land(impl, task_id="T-0001", apply=True)
    assert env["status"] == "error", env
    assert "apply=true" in env["message"], env
    assert "默认只预演" not in env["message"], (
        "传了 apply=true 却报告'默认只预演'，会误导 LLM 总控")
    assert env["details"]["applied"] is True, env
    assert "conflicts" in env["details"], env
    assert "feature.txt" in env["details"]["conflicts"], env


def test_land_conflict_message_still_says_dry_run_when_no_apply(world, fake_cmd):
    """没传 apply 撞冲突：仍要说"只预演"，别把 dry-run 语义也改坏。"""
    _make_conflict(world, fake_cmd)
    impl = _controller(world)

    env = _land(impl, task_id="T-0001")
    assert env["status"] == "error", env
    assert "只预演" in env["message"], env
    assert env["details"]["applied"] is False, env
    assert "feature.txt" in env["details"]["conflicts"], env


def test_land_success_message_unchanged(world, fake_cmd):
    """成功分支不能被动到：apply=true 仍报"已合入并关闭"，applied=true。"""
    world.add_agent("a1", "exec", caps=["python"], command=fake_cmd)
    world.add_task("T-0001", verify=["true"])
    world.hive("assign", "T-0001", "--agent", "a1")
    world.run_worker("a1")
    impl = _controller(world)

    env = _land(impl, task_id="T-0001", apply=True)
    assert env["status"] == "success", env
    assert "已合入并关闭" in env["message"], env
    assert env["details"]["applied"] is True, env
