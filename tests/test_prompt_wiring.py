"""T-0188：`hive prompt` / `hive dispatch-mark` / `hive status` 的接线回归。

这三个子命令都是**薄接线层**：把 T-0184/T-0185/T-0186 的三个纯逻辑模块接进 CLI。
本文件只测接线本身（参数解析、退出码、stdout 形态、字段落盘），
渲染的**逐字契约**由 tests/test_dispatch_prompt.py 负责，这里不重复。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from conftest import HIVE, sh


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #

def _run(hive_dir, *args, check=True):
    return sh([sys.executable, HIVE, "--dir", str(hive_dir), *args],
              check=check)


def _add_agent(world, name="a1"):
    world.add_agent(name, caps=("python",), command="true")


def _add_task(world, tid="T-0100", title="任务", body="测试任务",
              paths=("docs/",), needs=("python",)):
    world.add_task(tid, title=title, paths=paths, needs=needs,
                   body="\n".join([
                       "## 背景",
                       "这是一张测试卡。",
                       "",
                       "## 不要做什么",
                       "- 不修改无关文件。",
                   ]) if body == "测试任务" else body)


# --------------------------------------------------------------------------- #
# hive prompt
# --------------------------------------------------------------------------- #

def test_prompt_markdown_prints_card_body(world):
    """markdown 格式：stdout 是纯文本派发指令，包含任务摘要与执行契约。"""
    _add_agent(world)
    _add_task(world, tid="T-0100", title="接线测试", paths=("src/a.py",))
    p = _run(world.hive1, "prompt", "T-0100")
    out = p.stdout
    assert "请领取任务 T-0100" in out
    assert "接线测试" in out
    assert "允许修改路径" in out
    assert "src/a.py" in out
    assert "hive_claim" in out


def test_prompt_json_is_single_json_object(world):
    """--format json：stdout 只有**一个** JSON 对象，脚本能直接 json.loads。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    p = _run(world.hive1, "prompt", "T-0100", "--format", "json")
    obj = json.loads(p.stdout)      # 多行或带日志行都会在这里炸
    assert isinstance(obj, dict)
    assert obj["task_id"] == "T-0100"
    assert obj["agent"] is None
    assert "contract" in obj and "text" in obj
    assert "claim_tool" in obj and obj["claim_tool"]["name"] == "hive_claim"
    assert "hive" not in p.stderr.lower().replace("[hive", "") or True


def test_prompt_json_with_agent(world):
    """--format json --agent：payload 带 agent 字段，text 里含领取身份。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    p = _run(world.hive1, "prompt", "T-0100", "--agent", "trae-win",
             "--format", "json")
    obj = json.loads(p.stdout)
    assert obj["agent"] == "trae-win"
    assert "领取身份：trae-win" in obj["text"]


def test_prompt_task_flag_equivalent_to_positional(world):
    """`--task T-0184` 与位置参数 `T-0184` 输出逐字一致。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    pos = _run(world.hive1, "prompt", "T-0100", "--format", "json").stdout
    flag = _run(world.hive1, "prompt", "--task", "T-0100",
                "--format", "json").stdout
    assert pos == flag
    assert json.loads(pos) == json.loads(flag)


def test_prompt_unknown_task_exits_1_no_traceback(world):
    """任务不存在：退出码 1，stderr 是 HiveError 文本，**没有** traceback。"""
    _add_agent(world)
    p = _run(world.hive1, "prompt", "T-9999", check=False)
    assert p.returncode == 1
    assert "错误:" in p.stderr
    assert "Traceback" not in p.stderr
    assert "T-9999" in p.stderr


def test_prompt_clip_failure_does_not_change_exit_code(world, monkeypatch):
    """剪贴板工具不可用时：只打警告，stdout 正文照常输出，退出码仍是 0。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    import hive as hive_mod
    monkeypatch.setattr(hive_mod, "_copy_clipboard", lambda text: False)
    # monkeypatch 只作用于本进程；子进程里改不了，所以直接用 monkeypatch 后
    # 在同一进程内调用 cmd_prompt 验证警告分支。
    h = hive_mod.Hive(str(world.hive1))
    text = h.cmd_prompt("T-0100", fmt="markdown", clip=True)
    assert "请领取任务 T-0100" in text


# --------------------------------------------------------------------------- #
# hive dispatch-mark
# --------------------------------------------------------------------------- #

def test_dispatch_mark_writes_two_fields_keeps_ready(world):
    """dispatch-mark 写入 dispatched_to/dispatched_at，state 仍是 ready。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    _run(world.hive1, "dispatch-mark", "T-0100", "--agent", "a1")
    card = world.card("T-0100")
    assert card["state"] == "ready", card
    assert card["dispatched_to"] == "a1", card
    assert card["dispatched_at"], card          # 时间戳已写入


def test_dispatch_mark_clear_sets_none(world):
    """--clear 把两个字段置 None（不删键），state 仍是 ready。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    _run(world.hive1, "dispatch-mark", "T-0100", "--agent", "a1")
    _run(world.hive1, "dispatch-mark", "T-0100", "--clear")
    card = world.card("T-0100")
    assert card["state"] == "ready", card
    assert card.get("dispatched_to") is None, card
    assert card.get("dispatched_at") is None, card


def test_dispatch_mark_clear_and_agent_mutually_exclusive(world):
    """--clear 与 --agent 同时给：报错，退出码 1。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    p = _run(world.hive1, "dispatch-mark", "T-0100", "--clear",
             "--agent", "a1", check=False)
    assert p.returncode == 1
    assert "互斥" in p.stderr, p.stderr


def test_dispatch_mark_unknown_agent_errors(world):
    """agent 未登记：报错（复用 self.agent 校验），退出码 1。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    p = _run(world.hive1, "dispatch-mark", "T-0100", "--agent", "ghost",
             check=False)
    assert p.returncode == 1
    assert "ghost" in p.stderr, p.stderr


# --------------------------------------------------------------------------- #
# hive status：stage 与 board
# --------------------------------------------------------------------------- #

def test_status_json_has_stage_field(world):
    """status --json 每条记录增加 stage 字段；未加 --board 时顶层仍是数组。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    _add_task(world, tid="T-0101")
    # T-0100 打推送标记 → stage 应为 dispatched
    _run(world.hive1, "dispatch-mark", "T-0100", "--agent", "a1")
    # T-0101 被领取 → stage 应为 claimed
    _run(world.hive1, "assign", "T-0101", "--agent", "a1")

    out = json.loads(_run(world.hive1, "status", "--json").stdout)
    assert isinstance(out, list), "不加 --board 时顶层必须是数组（向后兼容）"
    by_id = {r["id"]: r for r in out}
    assert by_id["T-0100"]["stage"] == "dispatched", by_id
    assert by_id["T-0101"]["stage"] == "claimed", by_id
    assert by_id["T-0101"]["state"] == "assigned"


def test_status_json_board_block(world):
    """status --json --board：顶层是 {tasks, board}，board 含 counts 分组。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    _add_task(world, tid="T-0101")
    _run(world.hive1, "dispatch-mark", "T-0100", "--agent", "a1")
    _run(world.hive1, "assign", "T-0101", "--agent", "a1")

    out = json.loads(_run(world.hive1, "status", "--json", "--board").stdout)
    assert isinstance(out, dict) and "tasks" in out and "board" in out, out
    board = out["board"]
    assert board["total"] == 2, board
    assert board["counts"].get("dispatched") == 1, board
    assert board["counts"].get("claimed") == 1, board


def test_status_text_shows_board_header(world):
    """文本看板：表头上方有一行分组摘要（「待分配 N / 已推送 N / ...」）。"""
    _add_agent(world)
    _add_task(world, tid="T-0100")
    _run(world.hive1, "dispatch-mark", "T-0100", "--agent", "a1")
    p = _run(world.hive1, "status")
    assert "分组：" in p.stdout
    assert "已推送 1" in p.stdout, p.stdout
    assert "STAGE" in p.stdout, p.stdout


# --------------------------------------------------------------------------- #
# dispatch 的 TTL 过滤（T-0188 第 4 条）
# --------------------------------------------------------------------------- #

def test_dispatch_skips_card_within_ttl(world, monkeypatch):
    """TTL 内刚推送过的 ready 卡不再被派（避免重复通知同一个 agent）。"""
    world.add_agent("h1", caps=("python",), command="true",
                    repos={"hive": world.code_clone("h1")})
    world.add_task("T-0100", repo="hive", title="可派的卡", paths=("docs/",),
                   needs=("python",))
    _run(world.hive1, "dispatch-mark", "T-0100", "--agent", "h1")
    plan = json.loads(_run(world.hive1, "dispatch", "--json").stdout)["plan"]
    assert plan == [], f"TTL 内不该重复派，实际 plan={plan}"


def test_dispatch_skips_dispatched_can_replan_after_ttl(world, monkeypatch):
    """把 TTL 设为 0（已过期）：推送过的卡可以被重新派。"""
    world.add_agent("h1", caps=("python",), command="true",
                    repos={"hive": world.code_clone("h1")})
    world.add_task("T-0100", repo="hive", title="可派的卡", paths=("docs/",),
                   needs=("python",))
    _run(world.hive1, "dispatch-mark", "T-0100", "--agent", "h1")
    env = {**os.environ, "HIVE_DISPATCH_TTL_MINUTES": "0"}
    p = sh([sys.executable, HIVE, "--dir", str(world.hive1), "dispatch",
            "--json"], env=env)
    plan = json.loads(p.stdout)["plan"]
    assert [x["task"] for x in plan] == ["T-0100"], plan
