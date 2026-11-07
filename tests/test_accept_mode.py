"""accept 模式（派单—接单）：第四种 agent 模式。

现场（2026-11-07）：主控派给三台 Windows agent 的 9 张卡，10 分钟后被 watchdog
按 `dispatch_unclaimed` 全部回收，而定时任务每小时才醒一轮。根因与 manual 模式
无关，是「派单」和「接单」之间缺独立语义与足够长的等待窗口。

本文件钉住六条判据：
1. accept 是合法 mode、doctor 不报错、dispatch 能直接派（不需要 dispatchable）；
2. 接单窗口（默认 180 分钟）内 watchdog 不回收、不记 dispatch_miss；
3. 窗口用尽只退回 ready、保留派单关系与 inbox 便条，不转 blocked；
4. `hive accept` 只接派给自己的单，接单写 accepted_at 与 claim 租约；
5. MCP 工具清单与 handler 一一对应；
6. `accept_window_minutes` 可按 agent 配置覆盖。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import mcp_server


def _minutes_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=n)


def _set_assigned_at(world, tid: str, owner: str, when: datetime) -> None:
    p = world.hive1 / "inbox" / owner / f"{tid}.md"
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"^assigned_at:\s*\S+",
                  f"assigned_at: {when.isoformat(timespec='seconds')}",
                  text, flags=re.MULTILINE)
    p.write_text(text, encoding="utf-8")


def _add_accept_agent(world, name: str = "a1") -> None:
    # dispatch 测试环境的派发白名单默认只有 hive 仓
    world.add_agent(name, "accept", repos={"hive": world.code_clone(name)})


def test_accept_mode_is_legal_and_dispatchable(world):
    """accept 合法、doctor 不报非法 mode、且天然进 dispatch 候选。"""
    _add_accept_agent(world)
    world.add_task("T-0001", repo="hive")

    doc = world.hive("doctor")
    assert "mode='accept' 非法" not in doc.stdout, doc.stdout

    # 不带 dispatchable 也要能派：accept 就是「定时来接单」的声明
    world.hive("dispatch", "--apply")
    card = world.card("T-0001")
    assert card["state"] == "assigned", card
    assert card["owner"] == "a1", card
    assert card["dispatched_to"] == "a1", card


def test_accept_window_keeps_offer_alive(world):
    """派单 60 分钟（超过旧的 10 分钟阈值、未到 180 分钟）仍不能回收。"""
    _add_accept_agent(world)
    world.add_task("T-0001", repo="hive")
    world.hive("assign", "T-0001", "--agent", "a1")
    _set_assigned_at(world, "T-0001", "a1", _minutes_ago(60))

    world.hive("watchdog", "--apply")
    card = world.card("T-0001")
    assert card["state"] == "assigned", card
    assert card["owner"] == "a1", card
    assert not card.get("dispatch_miss"), card


def test_accept_timeout_returns_to_pool_without_miss_or_block(world):
    """窗口用尽：退回 ready，但保留派单与 inbox，不算派发失败。"""
    _add_accept_agent(world)
    world.add_task("T-0001", repo="hive")
    world.hive("assign", "T-0001", "--agent", "a1")
    _set_assigned_at(world, "T-0001", "a1", _minutes_ago(200))

    world.hive("watchdog", "--apply")
    card = world.card("T-0001")
    assert card["state"] == "ready", card
    assert not card.get("owner"), card
    assert card["dispatched_to"] == "a1", card   # 下一轮定时任务照样能接
    assert not card.get("dispatch_miss"), card
    assert "接单窗口超时" in card["_body"], card["_body"]
    assert (world.hive1 / "inbox" / "a1" / "T-0001.md").exists()


def test_accept_rejects_other_agents_orders(world):
    """不能接别人的单，也不能接无主单。"""
    _add_accept_agent(world, "a1")
    _add_accept_agent(world, "a2")
    world.add_task("T-0001", repo="hive")
    world.hive("assign", "T-0001", "--agent", "a1")

    p = world.hive("accept", "T-0001", "--agent", "a2", check=False)
    assert p.returncode != 0, p.stdout + p.stderr
    assert "不是 a2" in (p.stdout + p.stderr), p.stdout + p.stderr
    assert world.card("T-0001")["state"] == "assigned"

    world.add_task("T-0002", repo="hive")
    p2 = world.hive("accept", "T-0002", "--agent", "a1", check=False)
    assert p2.returncode != 0, p2.stdout + p2.stderr
    assert "还没派单" in (p2.stdout + p2.stderr), p2.stdout + p2.stderr


def test_accept_writes_marker_and_claim(world):
    """接单成功：抢到租约、卡面写 accepted_at、worktree 备好。"""
    _add_accept_agent(world)
    world.add_task("T-0001", repo="hive")
    world.hive("assign", "T-0001", "--agent", "a1")

    p = world.hive("accept", "T-0001", "--agent", "a1")
    assert p.returncode == 0, p.stdout + p.stderr
    card = world.card("T-0001")
    assert card.get("accepted_at"), card
    assert "接单成功" in (p.stdout + p.stderr), p.stdout + p.stderr


def test_mcp_accept_tool_is_advertised_and_handled():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert "hive_accept" in names
    assert "hive_accept" in mcp_server.HiveMcp.HANDLERS
    assert "hive_accept" not in mcp_server.CONTROLLER_TOOLS  # worker 自助接单


def test_accept_window_is_configurable_per_agent(world):
    """agent 自己把窗口调短，就按短窗口判定（不是硬编码 180）。"""
    _add_accept_agent(world)
    cfg_path = world.hive1 / "agents" / "a1.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["accept_window_minutes"] = 30
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    world.add_task("T-0001", repo="hive")
    world.hive("assign", "T-0001", "--agent", "a1")
    _set_assigned_at(world, "T-0001", "a1", _minutes_ago(40))

    world.hive("watchdog", "--apply")
    card = world.card("T-0001")
    assert card["state"] == "ready", card
    assert "接单窗口超时" in card["_body"], card["_body"]
    assert not card.get("dispatch_miss"), card
