"""T-0205 结果闸门：head 没推上来照样记 done 的假成功必须被拦下。

验收标准：
1. 本地有提交但没推 → 结果 failed 并点名「未推送」
2. changed_files=[] 的 done → failed
3. 正常卡零影响
4. 补 tests/test_result_head_guard.py
"""
import json
import subprocess
from pathlib import Path

import pytest


def git(repo, *args):
    """Helper for git commands."""
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r


class World:
    """临时仓库管理：协调仓 + 代码仓（light）。"""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.hive = tmp_path / "hive"
        self.light = tmp_path / "light"
        self._init_repos()

    def _init_repos(self):
        for p in [self.hive, self.light]:
            p.mkdir(parents=True, exist_ok=True)
            git(p, "init")
            git(p, "config", "user.name", "hive-test")
            git(p, "config", "user.email", "test@hive")
            (p / "README.md").write_text("# " + p.name + "\n")
            git(p, "add", "README.md")
            git(p, "commit", "-m", "init")
            # 用本地路径当 origin（避免网络依赖）
            git(p, "remote", "add", "origin", str(p))
            git(p, "push", "-u", "origin", "main")

    def write_task(self, tid: str, card: dict):
        """写任务卡到协调仓。"""
        task_dir = self.hive / "tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"id: {tid}", f"title: {card.get('title', tid)}"]
        if card.get("repo"):
            lines.append(f"repo: {card['repo']}")
        if card.get("base"):
            lines.append(f"base: {card['base']}")
        if card.get("paths"):
            lines.append("paths:")
            for p in card["paths"]:
                lines.append(f"  - {p}")
        if card.get("verify"):
            lines.append("verify:")
            for v in card["verify"]:
                lines.append(f"  - {v}")
        if card.get("allow_empty_diff"):
            lines.append("allow_empty_diff: true")
        body = card.get("body", "")
        content = "---\n" + "\n".join(lines) + "\n---\n" + body
        (task_dir / f"{tid}.md").write_text(content)
        git(self.hive, "add", "tasks")
        git(self.hive, "commit", "-m", f"task {tid}")

    def write_result(self, tid: str, result: dict):
        """写结果到协调仓。"""
        res_dir = self.hive / "results"
        res_dir.mkdir(parents=True, exist_ok=True)
        (res_dir / f"{tid}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2))
        git(self.hive, "add", "results")
        git(self.hive, "commit", "-m", f"result {tid}")

    def create_worktree(self, tid: str, head_sha: str):
        """在代码仓创建 task/<tid> 分支并推送。"""
        # 删除已存在的分支（本地 + 远程），忽略失败
        try:
            git(self.light, "checkout", "main")
        except RuntimeError:
            pass
        try:
            git(self.light, "branch", "-D", f"task/{tid}")
        except RuntimeError:
            pass
        try:
            git(self.light, "push", "origin", f":refs/heads/task/{tid}")
        except RuntimeError:
            pass
        git(self.light, "branch", f"task/{tid}", head_sha)
        git(self.light, "push", "origin", f"task/{tid}")

    def delete_remote_branch(self, tid: str):
        """删除远端 task/<tid> 分支。"""
        git(self.light, "push", "origin", f":refs/heads/task/{tid}")

    def rev_parse(self, repo, ref):
        r = git(repo, "rev-parse", "--verify", "-q", ref)
        return r.stdout.strip() if r.returncode == 0 else None


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_guard_passes_when_head_on_remote(world):
    """head 在远端、有改动 → 通过。"""
    # 写一个文件并提交
    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-001", head)

    from hive import guard_result_head
    ok, err = guard_result_head(
        world.light, "T-001", head, False, ["feature.txt"])
    assert ok
    assert err == ""


def test_guard_fails_when_head_not_pushed(world):
    """本地有提交但远端分支被删 → 失败。"""
    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-002", head)
    world.delete_remote_branch("T-002")

    from hive import guard_result_head
    ok, err = guard_result_head(
        world.light, "T-002", head, False, ["feature.txt"])
    assert not ok
    assert "未推送" in err


def test_guard_fails_when_remote_branch_deleted(world):
    """远端无此分支 → 失败。"""
    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    # 不推送，直接判

    from hive import guard_result_head
    ok, err = guard_result_head(
        world.light, "T-003", head, False, ["feature.txt"])
    assert not ok
    assert "未推送" in err


def test_guard_head_mismatch(world):
    """本地 head 与远端分支头不一致 → 失败。"""
    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-004", head)
    # 再改一个提交，让本地 head 与远端不一致
    (world.light / "extra.txt").write_text("extra\n")
    git(world.light, "add", "extra.txt")
    git(world.light, "commit", "-m", "add extra")
    new_head = world.rev_parse(world.light, "HEAD")

    from hive import guard_result_head
    ok, err = guard_result_head(
        world.light, "T-004", new_head, False, ["feature.txt", "extra.txt"])
    assert not ok
    assert "不一致" in err


def test_guard_fails_on_zero_changes(world):
    """零改动（非 allow_empty_diff）→ 失败。"""
    from hive import guard_result_head
    ok, err = guard_result_head(
        world.light, "T-005", None, False, [])
    assert not ok
    assert "零改动" in err


def test_guard_zero_changes_allowed_when_allow_empty_diff(world):
    """allow_empty_diff=True 时零改动通过。"""
    from hive import guard_result_head
    ok, err = guard_result_head(
        world.light, "T-006", None, True, [])
    assert ok
    assert err == ""


def test_guard_retries_ls_remote_once_on_network_failure(world):
    """ls-remote 失败一次后重试一次，仍失败按未推送处理。"""
    # 模拟：不推送分支，让 ls-remote 返回 None
    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")

    from hive import guard_result_head
    ok, err = guard_result_head(
        world.light, "T-007", head, False, ["feature.txt"])
    assert not ok
    assert "未推送" in err


def test_result_diff_files(world):
    """result_diff_files 正确列出改动文件。"""
    from hive import result_diff_files

    # 先在 main 上有一个提交
    (world.light / "base.txt").write_text("base\n")
    git(world.light, "add", "base.txt")
    git(world.light, "commit", "-m", "add base")

    # 创建 task 分支并添加新文件
    git(world.light, "branch", "task/T-007")
    git(world.light, "checkout", "task/T-007")
    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    base = world.rev_parse(world.light, "main")

    files = result_diff_files(world.light, base, head)
    assert "feature.txt" in files


def test_land_self_consistency_head_mismatch(world):
    """land 前置校验：结果 head 与远端分支头不一致 → 拒绝。"""
    from hive import guard_land_self_consistency

    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-013", head)

    # 模拟结果记录了另一个 head
    result = {"head": "deadbeef" * 5, "changed_files": ["feature.txt"]}
    ok, err = guard_land_self_consistency(
        world.light, "T-013", "main", result, False)
    assert not ok
    assert "不一致" in err


def test_land_self_consistency_zero_changes(world):
    """land 前置校验：changed_files 为空 → 拒绝。"""
    from hive import guard_land_self_consistency

    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-014", head)

    result = {"head": head, "changed_files": []}
    ok, err = guard_land_self_consistency(
        world.light, "T-014", "main", result, False)
    assert not ok
    assert "零改动" in err


def test_land_self_consistency_zero_changes_allowed(world):
    """land 前置校验：allow_empty_diff=True 时零改动通过。"""
    from hive import guard_land_self_consistency

    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-015", head)

    result = {"head": head, "changed_files": []}
    ok, err = guard_land_self_consistency(
        world.light, "T-015", "main", result, True)
    assert ok


def test_land_self_consistency_ok(world):
    """land 前置校验：正常卡通过。"""
    from hive import guard_land_self_consistency

    # 先在 main 上有一个提交
    (world.light / "base.txt").write_text("base\n")
    git(world.light, "add", "base.txt")
    git(world.light, "commit", "-m", "add base")

    # 创建 task 分支并添加新文件
    git(world.light, "branch", "task/T-016")
    git(world.light, "checkout", "task/T-016")
    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-016", head)

    result = {"head": head, "changed_files": ["feature.txt"]}
    ok, err = guard_land_self_consistency(
        world.light, "T-016", "main", result, False)
    assert ok


def test_land_self_consistency_count_mismatch(world):
    """land 前置校验：changed_files 数量与实际不符 → 拒绝。"""
    from hive import guard_land_self_consistency

    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-017", head)

    # 结果声称改了 2 个文件，实际只有 1 个
    result = {"head": head, "changed_files": ["feature.txt", "extra.txt"]}
    ok, err = guard_land_self_consistency(
        world.light, "T-017", "main", result, False)
    assert not ok
    assert "不符" in err


def test_land_rejects_when_result_head_mismatch(world):
    """land 拒绝：结果 head 与远端分支头不一致。"""
    from hive import guard_land_self_consistency

    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-018", head)

    # 结果记录了另一个 head
    result = {"head": "0000000" * 10, "changed_files": ["feature.txt"]}
    ok, err = guard_land_self_consistency(
        world.light, "T-018", "main", result, False)
    assert not ok
    assert "另一棵树" in err


def test_land_rejects_when_zero_changes_result(world):
    """land 拒绝：结果 changed_files 为空。"""
    from hive import guard_land_self_consistency

    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-019", head)

    result = {"head": head, "changed_files": []}
    ok, err = guard_land_self_consistency(
        world.light, "T-019", "main", result, False)
    assert not ok
    assert "零改动" in err


def test_land_normal_card_still_lands(world):
    """land 通过：正常卡。"""
    from hive import guard_land_self_consistency

    # 先在 main 上有一个提交
    (world.light / "base.txt").write_text("base\n")
    git(world.light, "add", "base.txt")
    git(world.light, "commit", "-m", "add base")

    # 创建 task 分支并添加新文件
    git(world.light, "branch", "task/T-020")
    git(world.light, "checkout", "task/T-020")
    (world.light / "feature.txt").write_text("hello\n")
    git(world.light, "add", "feature.txt")
    git(world.light, "commit", "-m", "add feature")
    head = world.rev_parse(world.light, "HEAD")
    world.create_worktree("T-020", head)

    result = {"head": head, "changed_files": ["feature.txt"]}
    ok, err = guard_land_self_consistency(
        world.light, "T-020", "main", result, False)
    assert ok
