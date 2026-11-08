"""T-0286 回归：agent 自己 commit 后 submit 不得再把有效交付判成零改动。

现场（T-0267 第二轮）：verify 3/3 code=0、成果齐全，但 agent 先自己 commit
过，`changed_files()` 只统计「相对 HEAD 的未提交改动」→ 空列表 →
`result_gate` 判「零改动」failed，最后靠主控手工 cherry-pick 收口。

修复口径：产结果与越界检查改用 `delivery_files()` =
`git diff base...HEAD` 与 `git status` 的并集；stage 仍只加未提交改动。
"""

from __future__ import annotations

import sys

from conftest import sh

VERIFY = f'{sys.executable} -c "print(1)"'


def _take(world, monkeypatch, tmp_path, tid, paths=("src/main.py",)):
    state = tmp_path / "state"
    monkeypatch.setenv("HIVE_STATE", str(state))
    world.add_agent("w1", mode="manual")
    world.add_task(tid, paths=paths, verify=(VERIFY,))
    world.hive("take", tid, "--agent", "w1")
    return state / "wt" / tid


def _commit(wt, world, *paths, msg="agent self commit"):
    sh(["git", "-C", str(wt), "add", *paths], env=world.git_env)
    sh(["git", "-C", str(wt), "commit", "-q", "-m", msg], env=world.git_env)


def _result(world, tid):
    return world.hive_json("result", tid)


def test_self_committed_file_counts_as_delivery(world, monkeypatch, tmp_path):
    """复现本 bug：agent 自己 commit → changed_files 含该文件、status=done。

    修前 changed_files() 返回 []，结果闸门判 failed。
    """
    tid = "T-1001"
    wt = _take(world, monkeypatch, tmp_path, tid)
    (wt / "src" / "main.py").write_text("print('T-1001')\n", encoding="utf-8")
    _commit(wt, world, "src/main.py")

    world.hive("submit", tid)
    res = _result(world, tid)
    assert res["status"] == "done", res.get("error")
    assert res["changed_files"] == ["src/main.py"]


def test_no_change_still_fails(world, monkeypatch, tmp_path):
    """一个文件都没改 → 仍判 failed（零改动保护不能丢）。"""
    tid = "T-1002"
    _take(world, monkeypatch, tmp_path, tid)

    world.hive("submit", tid)
    res = _result(world, tid)
    assert res["status"] == "failed"
    assert "零改动" in res.get("error", "")
    assert res["changed_files"] == []


def test_committed_and_uncommitted_union(world, monkeypatch, tmp_path):
    """已提交 + 未提交混合 → 并集正确、无重复项。"""
    tid = "T-1003"
    wt = _take(world, monkeypatch, tmp_path, tid,
                paths=("src/main.py", "src/extra.py"))
    (wt / "src" / "main.py").write_text("print('committed')\n", encoding="utf-8")
    _commit(wt, world, "src/main.py")
    (wt / "src" / "extra.py").write_text("print('uncommitted')\n", encoding="utf-8")

    world.hive("submit", tid)
    res = _result(world, tid)
    assert res["status"] == "done", res.get("error")
    assert set(res["changed_files"]) == {"src/main.py", "src/extra.py"}
    assert len(res["changed_files"]) == 2, res["changed_files"]


def test_allow_empty_diff_card_still_passes(world, monkeypatch, tmp_path):
    """allow_empty_diff: true 的验收卡仍放行（不产文件改动）。"""
    tid = "T-1004"
    state = tmp_path / "state"
    monkeypatch.setenv("HIVE_STATE", str(state))
    world.add_agent("w1", mode="manual")
    card = world.hive1 / "tasks" / f"{tid}.md"
    card.write_text(
        "---\n"
        f"id: {tid}\n"
        "title: 纯验收卡\n"
        "repo: light\n"
        "base: main\n"
        "paths:\n"
        "  - docs/audit.md\n"
        "allow_empty_diff: true\n"
        "verify:\n"
        f"  - {VERIFY}\n"
        "---\n\n只做验收，不改文件。\n",
        encoding="utf-8",
    )
    sh(["git", "-C", str(world.hive1), "add", f"tasks/{tid}.md"],
       env=world.git_env)
    sh(["git", "-C", str(world.hive1), "commit", "-q", "-m", f"task {tid}"],
       env=world.git_env)
    world.hive("take", tid, "--agent", "w1")

    world.hive("submit", tid)
    res = _result(world, tid)
    assert res["status"] == "done", res.get("error")
    assert res["changed_files"] == []


def test_committed_out_of_paths_is_detected(world, monkeypatch, tmp_path):
    """越界检查一并覆盖已提交文件：提交了 paths 外的文件要记进 out_of_paths。"""
    tid = "T-1005"
    wt = _take(world, monkeypatch, tmp_path, tid, paths=("src/main.py",))
    (wt / "outside.txt").write_text("out of paths\n", encoding="utf-8")
    _commit(wt, world, "outside.txt")

    world.hive("submit", tid)
    res = _result(world, tid)
    assert res["out_of_paths"] == ["outside.txt"], res["out_of_paths"]
