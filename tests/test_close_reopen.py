"""终止 / 重开的语义（T-0181 的教训）。

以前 `hive close` 无条件把卡写成 `done`，于是「中途掐掉」和「真的做完」在看班上长得
一模一样，卡面还只留一句「看板操作」。这里把新语义钉住：

    close 且没有 res/<tid> 结果  ->  state=closed（作废），卡面记现场
    close 且 --as-done           ->  state=done（人工认定完成）
    reopen                       ->  done/closed 拉回 ready，清 owner，留痕
    对非终态卡 reopen            ->  报错，别让人用错命令
"""
from __future__ import annotations

from pathlib import Path


def _card_text(world, tid: str) -> str:
    return (world.hive1 / "tasks" / f"{tid}.md").read_text(encoding="utf-8")


def _state(world, tid: str) -> str:
    for line in _card_text(world, tid).splitlines():
        if line.startswith("state:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{tid} 卡面没有 state 字段")


def test_close_without_result_is_cancelled_not_done(world):
    world.add_task("T-0001", title="没跑完的卡")
    world.hive("close", "T-0001", "--note", "手工终止：agent 卡住了")

    assert _state(world, "T-0001") == "closed", "没有 result 的卡不该记成 done"
    body = _card_text(world, "T-0001")
    assert "关闭作废" in body
    assert "现场：" in body, "作废必须留下现场（原状态/owner/有无结果）"
    assert "无结果" in body


def test_close_forced_done(world):
    world.add_task("T-0001", title="人工认定完成")
    world.hive("close", "T-0001", "--note", "人工确认做完了", "--as-done")
    assert _state(world, "T-0001") == "done"


def test_reopen_brings_card_back_to_ready(world):
    world.add_task("T-0001", title="要重开的卡")
    world.hive("close", "T-0001", "--note", "误操作")
    assert _state(world, "T-0001") == "closed"

    world.hive("reopen", "T-0001", "--reason", "误点终止，继续做")
    assert _state(world, "T-0001") == "ready"
    body = _card_text(world, "T-0001")
    assert "重开" in body and "从 closed 拉回 ready" in body
    assert "误点终止" in body
    # owner 必须清空，否则 dispatch 会以为还有人占着
    for line in body.splitlines():
        if line.startswith("owner:"):
            assert line.split(":", 1)[1].strip() in ("", "null")
            break


def test_reopen_refuses_non_terminal_card(world):
    world.add_task("T-0001", title="还在待领取的卡")
    r = world.hive("reopen", "T-0001", check=False)
    assert r.returncode != 0
    assert "只有已完成" in (r.stderr + r.stdout)
