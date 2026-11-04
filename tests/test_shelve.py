"""搁置（shelved）：当前做不了、但不想删的卡。

和三条既有路径划清界限，这几条是本文件要钉住的语义：

    unassign  放回 ready，谁都能再领 —— 搁置卡放回去只会占位置、干扰领取
    blocked   等资源、还得盯着，看板进异常列、doctor 会报 —— 搁置不算挂账
    close     这张卡不做了 —— 搁置是「别忘，以后再说」，卡面和现场都要留

所以搁置必须同时做到：① 释放租约/结果/进度/收件箱信号；② 不被 take / land /
dispatch 捞走；③ 卡面记清「什么时候、从什么状态、因为什么」；④ 留好三个
出口：放回待领取 / 另立新卡 / 废弃。
"""
from __future__ import annotations

import json
from pathlib import Path

import hive
from conftest import sh


def _card_text(world, tid: str) -> str:
    return (world.hive1 / "tasks" / f"{tid}.md").read_text(encoding="utf-8")


def _state(world, tid: str) -> str:
    for line in _card_text(world, tid).splitlines():
        if line.startswith("state:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{tid} 卡面没有 state 字段")


def _front(world, tid: str, key: str) -> str:
    for line in _card_text(world, tid).splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def _fake_runtime(world, tid: str, owner: str = "a1") -> None:
    """造出一张「正在被 agent 做着」的卡该有的全部派生物。"""
    h = hive.Hive(str(world.hive1))
    h.fetch()
    hive.commit_file_to_ref(
        h.dir, f"claims/{tid}.json",
        json.dumps({"agent": owner, "lease_id": "L1", "fence": 1,
                    "lease_until": "2099-01-01T00:00:00+00:00"}),
        f"claim/{tid}", "origin/main", "fake claim")
    hive.commit_file_to_ref(
        h.dir, f"results/{tid}.json",
        json.dumps({"status": "failed", "summary": "编译不过，等上游修好"}),
        f"res/{tid}", "origin/main", "fake result", force=True)
    hive.commit_file_to_ref(
        h.dir, f"status/{tid}.json",
        json.dumps({"progress": 40, "step": "编译", "message": "卡住了"}),
        f"status/{tid}", "origin/main", "fake progress")
    # 归档结果 + 收件箱分派信号（工作区文件）
    (world.hive1 / "results").mkdir(exist_ok=True)
    (world.hive1 / "results" / f"{tid}.json").write_text(
        json.dumps({"status": "failed", "summary": "编译不过"}), encoding="utf-8")
    inbox = world.hive1 / "inbox" / owner
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{tid}.md").write_text(f"# {tid} -> {owner}\n", encoding="utf-8")
    sh(["git", "-C", str(world.hive1), "add", "-A", "--",
        f"results/{tid}.json", f"inbox/{owner}/{tid}.md"])
    sh(["git", "-C", str(world.hive1), "commit", "-q", "-m", "fake runtime files"],
       check=False)


def test_shelve_writes_state_and_full_context(world):
    world.add_task("T-0001", title="等上游接口，暂时做不了")
    world.hive("shelve", "T-0001", "--reason", "上游 SDK 还没发版，等 12 月")

    assert _state(world, "T-0001") == "shelved"
    assert _front(world, "T-0001", "shelved_from") == "ready"
    assert "12 月" in _front(world, "T-0001", "shelve_reason")
    assert _front(world, "T-0001", "shelved_at"), "搁置时刻要落卡面"
    body = _card_text(world, "T-0001")
    assert "搁置" in body and "现场：" in body, "卡面必须留下当时为什么停"
    assert "日后处置" in body, "三个出口要写在卡上，免得以后不知道怎么捡起来"
    assert _front(world, "T-0001", "owner") in ("", "null"), "搁置卡不该还挂着 owner"


def test_shelve_releases_all_runtime_files(world):
    world.add_task("T-0001", title="做了一半，卡在外部依赖")
    _fake_runtime(world, "T-0001")

    world.hive("shelve", "T-0001", "--reason", "等外部依赖")

    for ref in ("claim", "res", "status"):
        assert world.refs(f"refs/heads/{ref}/T-0001") == [], \
            f"{ref}/T-0001 分支没释放，旧 agent 还会续跑 / 看板还显示进度"
    assert not (world.hive1 / "results" / "T-0001.json").exists()
    assert not (world.hive1 / "inbox" / "a1" / "T-0001.md").exists()
    # 摘要写进卡面：文件可以删，现场不能丢
    assert "编译不过，等上游修好" in _card_text(world, "T-0001")


def test_shelve_copies_full_result_summary_before_releasing_res(world):
    """搁置会删 res 分支；worker 的长结论必须全文留在卡面，不能只剩 200 字。"""
    world.add_task("T-0001", title="有重要定因结论的卡")
    long_summary = "".join(f"第 {i} 层已排除：证据见附录 {i}。\n" for i in range(200))
    h = hive.Hive(str(world.hive1))
    h.fetch()
    hive.commit_file_to_ref(
        h.dir, "results/T-0001.json",
        json.dumps({"status": "done", "summary": long_summary}, ensure_ascii=False),
        "res/T-0001", "origin/main", "long result", force=True)
    world.hive("shelve", "T-0001", "--reason", "等 arm64 环境")

    body = _card_text(world, "T-0001")
    assert "<details>" in body, "长结论要折叠，别把卡面撑爆"
    assert body.count("层已排除：证据见附录") == 200, \
        "完整摘要必须逐条留在卡面（res 分支已被释放，别处没有副本）"
    assert world.refs("refs/heads/res/T-0001") == []


def test_shelved_card_is_not_in_status_as_claimable(world):
    world.add_task("T-0001", title="搁置的卡")
    _fake_runtime(world, "T-0001")
    world.hive("shelve", "T-0001", "--reason", "等条件")

    row = world.hive_json("status", "--json")[0]
    assert row["state"] == "shelved", "残留 claim 绝不能把 shelved 推导回 running"
    assert row["shelve_reason"] == "等条件"
    assert row["shelved_from"] == "ready"
    assert row["stage"] == "shelved"
    assert row["claim"] == ""
    assert row["result_status"] == ""


def test_shelved_card_refuses_manual_take_and_unassign(world):
    world.add_agent("a1", "exec")
    world.add_task("T-0001", title="别被领走")
    world.hive("shelve", "T-0001", "--reason", "等条件")

    r = world.hive("take", "T-0001", "--agent", "a1", check=False)
    assert r.returncode != 0, "人工 take 不能绕过搁置"
    assert "搁置" in (r.stdout + r.stderr)

    for cmd in ("unassign", "retry", "block"):
        r = world.hive(cmd, "T-0001", check=False)
        assert r.returncode != 0, f"{cmd} 不该把搁置卡悄悄解锁"
    assert _state(world, "T-0001") == "shelved"


def test_shelved_card_is_not_landable(world):
    world.add_task("T-0001", title="有结果也要搁置")
    _fake_runtime(world, "T-0001")
    # 先造一份「可以合入」的结果，再搁置 —— 搁置必须把它从 land --all 里摘掉
    h = hive.Hive(str(world.hive1))
    h.fetch()
    hive.commit_file_to_ref(
        h.dir, "results/T-0001.json",
        json.dumps({"status": "done", "verify": [], "summary": "done"}),
        "res/T-0001", "origin/main", "done result", force=True)
    world.hive("shelve", "T-0001", "--reason", "结果先封存，暂不合并")

    assert h.landable() == [], "搁置卡不能被 land --all 顺手合入"
    plan = h.land_one("T-0001", apply=False)
    assert any("搁置" in p for p in plan["problems"]), plan


def test_unshelve_returns_card_to_ready(world):
    world.add_task("T-0001", title="条件成熟了")
    world.hive("shelve", "T-0001", "--reason", "等上游")
    world.hive("unshelve", "T-0001", "--reason", "上游已发版")

    assert _state(world, "T-0001") == "ready"
    assert _front(world, "T-0001", "shelved_at") == "", "恢复后元数据要清干净"
    body = _card_text(world, "T-0001")
    assert "取消搁置" in body and "上游已发版" in body
    # 历史留痕还在，不是把卡擦回原样
    assert "搁置" in body and "日后处置" in body


def test_spawn_new_keeps_original_shelved(world):
    world.add_task("T-0001", title="旧目标已经不成立")
    world.hive("shelve", "T-0001", "--reason", "需求变了")

    r = world.hive("unshelve", "T-0001", "--spawn-new",
                   "--title", "按新接口重做", check=True)
    assert "NEW_TASK=T-0002" in r.stdout

    assert _state(world, "T-0001") == "shelved", "原卡保持搁置，确认后再废弃"
    assert "已派生 T-0002" in _card_text(world, "T-0001")
    assert _state(world, "T-0002") == "ready"
    assert "按新接口重做" in _card_text(world, "T-0002")
    assert "源自搁置卡 T-0001" in _card_text(world, "T-0002")
    assert _front(world, "T-0002", "repo") == "light", "新卡要继承模板的 repo/base 等字段"
    assert _front(world, "T-0002", "retries") == "0", "别把旧卡的重试次数带过去"


def test_shelve_refuses_terminal_and_double_shelve(world):
    world.add_task("T-0001", title="正常卡")
    world.hive("close", "T-0001", "--note", "不做了")
    r = world.hive("shelve", "T-0001", check=False)
    assert r.returncode != 0 and "终态" in (r.stdout + r.stderr)

    world.add_task("T-0002", title="另一张")
    world.hive("shelve", "T-0002", "--reason", "等条件")
    r = world.hive("shelve", "T-0002", check=False)
    assert r.returncode != 0 and "已搁置" in (r.stdout + r.stderr)
    r = world.hive("unshelve", "T-0001", check=False)
    assert r.returncode != 0, "非搁置卡不能 unshelve"


def test_shelved_card_can_be_cancelled(world):
    """第三个出口：废弃。搁置时结果已释放，close 应记作 closed（作废）。"""
    world.add_task("T-0001", title="以后也不做了")
    world.hive("shelve", "T-0001", "--reason", "等条件")
    world.hive("close", "T-0001", "--note", "确认废弃")

    assert _state(world, "T-0001") == "closed"
    assert "原状态 shelved" in _card_text(world, "T-0001")
