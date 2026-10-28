"""代码仓作用域守卫（T-0133）：只有登记过的仓，hive 才推得出去。

背景：4 张 `repo: light` 的卡被派出去后，hive 把代码推到了另一个项目 light 的 main 上
（共 8 个 commit）。用户明确「hive 初期不该动 light」。那 9 个 commit 已撤、卡已记作废，
但"能推"的线是 `agents/*.json` 里的 `repos` 登记——本测试把它钉死。

摘掉后由既有代码路径保证推不出去：
- dispatch / next / 领取判定：仓库不匹配 → 跳过；
- 手工 assign 一张 light 卡 → Worker.prepare 报「没有配置仓库 … 的本地路径」，任务记 failed；
- hive land 需要 code_repo() → 报「没有 agent 登记代码仓 … 的本地路径」。

2026-10-28 更新：light / lightharness 都已按 docs/操作手册.md 的流程正式接入
（前者有 T-0181/T-0183 在飞，后者有 T-0170~T-0179 的升级线），白名单随之放宽。
守卫的职责没有变——**没登记的仓永远推不出去**；要禁某个仓，把它塞进 FORBIDDEN。

注意：这个测试**纯静态**，不联网、不改仓库状态，也不依赖 /home/skywalk/src/light 是否真的
存在——换台机器也能跑。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"

# 允许 agent 登记的代码仓。
#
# 历史：T-0133 时发生过 hive 把 commit 推到 light main 的事故，于是这里钉死成
# {"hive"} + FORBIDDEN={"light"}。之后两个仓都是**有意接入**的：
#   - lightharness：a69924e 为 0.2.1-rc.3 升级任务（T-0170~T-0179）登记；
#   - light：9f973b9 登记 light-merge 路径，当前有活跃卡（T-0181 编译器形态、
#     T-0183 mbedTLS 握手死锁）。
# 按 docs/操作手册.md「要放开就先确认对方仓库的接入约定，并同步更新白名单」的流程，
# 2026-10-28 把这两个仓正式纳入白名单 —— 注意这**不是**放开事故防线：
# 事故的根因是"未登记也能推"，现在仍然只有登记过的仓才动得了。
ALLOWED_REPOS = {"hive", "lightharness", "light"}

# 已明确退役/禁止接入的仓库。要重新禁掉某个仓，只做两步：
#   1) 把仓名加到这里；
#   2) 清掉 agents/*.json 里对应的 repos 键。
# 测试会立刻变红，逼着改动方把在飞的卡一并处理（改派/作废）。
FORBIDDEN: set[str] = set()


def _agent_files():
    return sorted(AGENTS.glob("*.json"))


def test_agent_dir_populated():
    assert AGENTS.is_dir(), AGENTS
    assert _agent_files(), "agents/ 里没有任何 agent 配置"


def test_no_agent_registers_forbidden_repo():
    """FORBIDDEN 里的仓一旦被登记就立刻红（当前为空集：light / lightharness 已正式接入）。"""
    for p in _agent_files():
        repos = json.loads(p.read_text(encoding="utf-8")).get("repos") or {}
        bad = sorted(set(repos) & FORBIDDEN)
        assert not bad, (
            f"{p.name} 登记了禁止的代码仓 {bad}（该仓已退役/禁止接入；"
            f"要放开请先确认对方仓库的接入约定，把它从 FORBIDDEN 移进 {ALLOWED_REPOS}）")


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
