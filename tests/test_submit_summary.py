"""T-0246 回归：worker 提交不得把 `.hive/` 运行时文件当交付物带进 task 分支。

真实现场：hive 仓自己也跟踪 `.hive/summary.md`（内容是上一张卡的小结），
worker 用 `git add -A` 提交时把它一起带上，导致 `land` 反复撞同一个文件。
"""

from __future__ import annotations

import sys

from conftest import sh
from hive import delivery_paths, lease_until_iso


def test_lease_until_iso_normalizes_store_epoch():
    """store 的 lease_until 是 epoch 秒；submit 前必须收敛成 ISO，否则崩。"""
    assert lease_until_iso(None) == ""
    assert lease_until_iso(0) == ""
    assert lease_until_iso("2026-11-03T23:05:37+08:00") == "2026-11-03T23:05:37+08:00"
    assert lease_until_iso(1793718937.0).startswith("2026-")


def test_delivery_paths_drops_hive_runtime_files():
    """默认剔除 `.hive/`；卡面显式点名时保留（历史兼容）。"""
    assert delivery_paths(
        ["src/main.py"], ["src/main.py", ".hive/summary.md"]) == ["src/main.py"]
    assert ".hive/summary.md" in delivery_paths(
        [".hive/summary.md"], ["src/main.py", ".hive/summary.md"])


def test_submit_does_not_branch_hive_summary(world, monkeypatch, tmp_path):
    """写 `.hive/summary.md` → submit → 分支树上必须没有它，代码改动正常带上。"""
    state = tmp_path / "state"
    monkeypatch.setenv("HIVE_STATE", str(state))

    world.add_agent("w1", mode="manual")
    repo = world.code_clone("w1")
    # 模拟现场：基底仓（light.git）已经跟踪了 `.hive/summary.md`
    (repo / ".hive").mkdir(exist_ok=True)
    (repo / ".hive" / "summary.md").write_text(
        "上一张卡 T-0237 的残留小结\n", encoding="utf-8")
    sh(["git", "-C", str(repo), "add", ".hive/summary.md"], env=world.git_env)
    sh(["git", "-C", str(repo), "commit", "-q", "-m", "seed .hive/summary.md"],
       env=world.git_env)
    sh(["git", "-C", str(repo), "push", "-q", "origin", "main"], env=world.git_env)

    world.add_task(
        "T-1001", title="提交不带 .hive 运行时文件", paths=("src/main.py",),
        verify=(f'{sys.executable} -c "print(1)"',))
    world.hive("take", "T-1001", "--agent", "w1")

    wt = state / "wt" / "T-1001"
    assert (wt / ".hive" / "summary.md").exists(), \
        "基底应跟踪 .hive/summary.md（模拟现场）"

    # agent 干活：改代码 + 写本卡小结
    (wt / "src" / "main.py").write_text(
        "print('hi')\nprint('T-1001')\n", encoding="utf-8")
    (wt / ".hive" / "summary.md").write_text(
        "T-1001 的真实小结（只进 results/）\n", encoding="utf-8")

    world.hive("submit", "T-1001")

    tree = sh(["git", "--git-dir", str(world.code_origin),
               "ls-tree", "-r", "--name-only", "task/T-1001"]).stdout.split()
    assert ".hive/summary.md" not in tree, \
        "提交把 .hive/summary.md 当交付物带进 task 分支了"
    assert "src/main.py" in tree, "真正的代码改动应该被提交"
