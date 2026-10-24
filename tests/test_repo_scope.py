"""代码仓作用域守卫（T-0133）：hive 现阶段只能碰 hive 仓自己。

背景：4 张 `repo: light` 的卡被派出去后，hive 把代码推到了另一个项目 light 的 main 上
（共 8 个 commit）。用户明确「hive 初期不该动 light」。那 9 个 commit 已撤、卡已记作废，
但"能推"的线是 `agents/*.json` 里的 `repos` 登记——本测试把它钉死。

摘掉后由既有代码路径保证推不出去：
- dispatch / next / 领取判定：仓库不匹配 → 跳过；
- 手工 assign 一张 light 卡 → Worker.prepare 报「没有配置仓库 … 的本地路径」，任务记 failed；
- hive land 需要 code_repo() → 报「没有 agent 登记代码仓 … 的本地路径」。

注意：这个测试**纯静态**，不联网、不改仓库状态，也不依赖 /home/skywalk/src/light 是否真的
存在——换台机器也能跑。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"

# 现阶段 hive 只允许改动自己这个仓
ALLOWED_REPOS = {"hive"}
FORBIDDEN = {"light"}


def _agent_files():
    return sorted(AGENTS.glob("*.json"))


def test_agent_dir_populated():
    assert AGENTS.is_dir(), AGENTS
    assert _agent_files(), "agents/ 里没有任何 agent 配置"


def test_no_agent_registers_forbidden_repo():
    """谁把 light 加回来，这里立刻红。"""
    for p in _agent_files():
        repos = json.loads(p.read_text(encoding="utf-8")).get("repos") or {}
        bad = sorted(set(repos) & FORBIDDEN)
        assert not bad, (
            f"{p.name} 登记了禁止的代码仓 {bad}（hive 初期不碰其它项目；"
            f"要放开请先确认对方仓库的接入约定，并把 {ALLOWED_REPOS} 以外的名字加进白名单）")


def test_agent_repos_within_allowed_set():
    for p in _agent_files():
        repos = json.loads(p.read_text(encoding="utf-8")).get("repos") or {}
        extra = sorted(set(repos) - ALLOWED_REPOS)
        assert not extra, f"{p.name} 登记了白名单外的代码仓：{extra}"


def test_at_least_one_agent_registers_hive():
    """防止删过头：dispatch 至少要有一个能干 hive 卡的 agent。"""
    ok = [
        p.name for p in _agent_files()
        if "hive" in (json.loads(p.read_text(encoding="utf-8")).get("repos") or {})
    ]
    assert ok, "没有任何 agent 登记 hive 仓，dispatch 会直接废掉"
