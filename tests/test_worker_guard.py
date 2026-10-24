"""Worker 不得静默抹掉协调仓里未提交的已跟踪改动（T-0129）。

回归守卫：修复前 `Worker.__init__` / `Worker.loop()` 是无条件 `fetch + reset --hard`，
在主工作区上跑 `hive take` / `work` / `submit` 会把人正写到一半的草稿等改动直接抹掉。
"""
from __future__ import annotations

import pytest

from conftest import sh
from hive import HiveError, Worker


MARKER = "\nDIRTY-MARKER-DO-NOT-REMOVE\n"


def _tracked(world, name="KEEP.md"):
    """建一个**已跟踪且已推送**的文件，返回它的路径。"""
    p = world.hive1 / name
    p.write_text("keep\n", encoding="utf-8")
    hs = ["git", "-C", str(world.hive1)]
    sh(hs + ["add", name], env=world.git_env)
    sh(hs + ["commit", "-q", "-m", f"add {name}"], env=world.git_env)
    sh(hs + ["push", "-q", "origin", "main"], env=world.git_env)
    return p


def _dirty(p):
    """把它改脏（未提交），返回标记文本。"""
    p.write_text(p.read_text(encoding="utf-8") + MARKER, encoding="utf-8")


def test_worker_init_preserves_dirty_tracked_file(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    p = _tracked(world)
    _dirty(p)

    Worker(str(world.hive1), "w1")  # 构造本身不该动工作区

    assert MARKER in p.read_text(encoding="utf-8"), (
        "构造 Worker 把未提交的已跟踪改动抹掉了（又回到裸 reset --hard 了？）")


def test_worker_loop_once_preserves_dirty_tracked_file(world, fake_cmd):
    world.add_agent("w1", "exec", command=fake_cmd)
    p = _tracked(world)
    _dirty(p)

    world.run_worker("w1", hive_dir=world.hive1)  # 走 --once 的轮询循环

    assert MARKER in p.read_text(encoding="utf-8"), (
        "worker 轮询把未提交的已跟踪改动抹掉了")


def test_sync_main_raises_instead_of_wiping_when_behind_and_dirty(world, fake_cmd):
    """落后于 origin/main 且工作区脏：必须报错，绝不静默丢弃。"""
    world.add_agent("w1", "exec", command=fake_cmd)
    p = _tracked(world)

    # 让 hive1 落后：从另一个 clone 往 origin/main 再推一个提交
    other = str(world.hive2)
    sh(["git", "-C", other, "fetch", "-q", "origin"], env=world.git_env)
    sh(["git", "-C", other, "reset", "--hard", "-q", "origin/main"],
       env=world.git_env)
    (world.hive2 / "BEHIND.md").write_text("behind maker\n", encoding="utf-8")
    sh(["git", "-C", other, "add", "-A"], env=world.git_env)
    sh(["git", "-C", other, "commit", "-q", "-m", "behind maker"],
       env=world.git_env)
    sh(["git", "-C", other, "push", "-q", "origin", "main"], env=world.git_env)

    _dirty(p)

    with pytest.raises(HiveError) as e:
        Worker(str(world.hive1), "w1")
    assert "不敢自动 reset" in str(e.value), e.value
