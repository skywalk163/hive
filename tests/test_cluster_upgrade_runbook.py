"""T-0471：节点升级清单必须可执行、可复核（不是一篇散文）。

2026-11-25 现场：两个租约 bug 修好并推到三端，但**只在本机生效** ——
租约由主控签发，而主控 fb98 跑的还是 41 小时前的旧代码，下发的
`leader_lease_until` 仍是 now+5s。要真正收口，必须把 fb98 / IC6RTQ4
的仓更新并重启它们的集群进程。

这张卡的交付物是一份 runbook（`docs/cluster/节点升级清单.md`）+ 这份判据。
判据钉的不是"文档存在"，而是**文档里那几条不能错的硬信息**：

1. 三个节点的**真实仓路径**（写错就等于把人引到空目录）；
2. **重启顺序**：followers 先于 leader（先换主控会让集群短暂无主）；
3. 重启前必须**先查 running 任务**（不能静默打断别人的活）；
4. 升级后怎么**实测**确认（租约秒数 ≥ 10s，而不是旧代码的 5s）；
5. 红线：不许 `--force` / `--no-verify` / 手改 state.json。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "cluster" / "节点升级清单.md"

# 节点仓路径（来自节点登记的 hive_dir，2026-11-25 实测）
NODE_PATHS = {
    "fb98": r"/home/skywalk/dswork/hive",
    "IC6RTQ4": r"C:\dswork\hive",
    "DESKTOP-6LFQUO8": r"G:\dswork\duan-light-merge\hive",
}


@pytest.fixture(scope="module")
def runbook() -> str:
    assert RUNBOOK.is_file(), (
        f"缺少升级清单：{RUNBOOK.relative_to(ROOT)} —— "
        "这张卡的交付物就是这个文件")
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_lists_every_node_hive_dir(runbook):
    """三个节点的仓路径都要写对：写错等于把人引到空目录。"""
    missing = []
    for node, path in NODE_PATHS.items():
        if node not in runbook:
            missing.append(f"{node}（节点名本身缺失）")
        elif path not in runbook:
            missing.append(f"{node}（缺仓路径 {path!r}）")
    assert not missing, "升级清单缺节点信息：" + "; ".join(missing)


def test_runbook_has_pull_and_restart_commands(runbook):
    """每个节点都要给出可直接照抄的 pull + restart 命令。"""
    assert runbook.count("pull --ff-only") >= 2, \
        "至少两个节点要给出 `pull --ff-only` 命令"
    assert "cluster restart" in runbook, "要写重启命令"


def test_dangerous_flags_only_appear_as_prohibitions(runbook):
    """`--force` / `--no-verify` 只许作为「不许做」出现，不许出现在可照抄的命令块里。

    文档里必然要写「不要 push --force」这类红线 —— 把整篇一刀切禁掉这些词，
    等于逼文档不敢提红线。真正该禁的是它们出现在**命令**里。
    """
    prohibited = ("push --force", "push -f", "reset --hard", "--no-verify")
    for bad in prohibited:
        for line in runbook.splitlines():
            if bad not in line:
                continue
            # 命令行（缩进/以 git/hive 起头）里出现就是红线违规
            stripped = line.strip()
            is_command = (
                stripped.startswith(("git ", "hive ", "$ ", "#!"))
                or line.startswith("    ")
            )
            is_prohibition = (
                "不许" in line or "不要" in line or "别" in line
                or "禁止" in line or "红线" in line
            )
            assert is_prohibition or not is_command, (
                f"清单里 {bad!r} 出现在可照抄的命令行里：{line!r}")


def test_runbook_orders_followers_before_leader(runbook):
    """重启顺序：follower 先、leader 后。

    先换主控会让集群短暂无主（租约到期 + 重新选举），影响面最大。
    """
    lower = runbook.lower()
    idx_follower = lower.find("follower")
    idx_leader = lower.find("leader")
    assert idx_follower != -1 and idx_leader != -1, \
        "清单里要明确提到 follower / leader 两种角色"

    # 找「重启顺序」那一段，断言 follower 出现在 leader 之前
    m = re.search(r"重启顺序[^\n]*\n(.{0,600})", runbook, re.S)
    assert m, "清单里要有明确标注「重启顺序」的一段"
    seg = m.group(1).lower()
    pos_f = seg.find("follower")
    pos_l = seg.find("leader")
    assert pos_f != -1 and pos_l != -1, "重启顺序那一段要同时提到 follower 与 leader"
    assert pos_f < pos_l, (
        "重启顺序写反了：应当先 follower（IC6RTQ4）再 leader（fb98），"
        "先换主控会让集群短暂无主")


def test_runbook_checks_running_tasks_first(runbook):
    """重启前必须先看有没有 running 任务：不能静默打断别人的活。"""
    assert re.search(r"(running|在跑|运行中)", runbook), \
        "清单要提到「先查有没有在跑的任务」"
    assert "restart" in runbook and re.search(r"先.*(running|在跑)", runbook), (
        "要点明「先查任务、再重启」的先后关系，而不是只说一句'注意任务'")


def test_runbook_says_how_to_verify_lease_after_upgrade(runbook):
    """升级后怎么验：租约秒数要 ≥ 10s（旧代码是 5s）。"""
    assert "leader_lease_until" in runbook or "租约" in runbook, \
        "清单要说明升级后如何验证租约"
    # 必须给出可判定的数字门槛
    assert re.search(r"(≥\s*10|>=\s*10|10\s*s|10\s*秒)", runbook, re.I), (
        "要给出可判定的门槛（租约剩余 ≥ 10s，旧代码只有 5s），"
        "否则「升级成功」无法验收")


def test_runbook_forbids_hand_editing_state(runbook):
    """不许改 state.json / 手工造假租约来让数字好看。"""
    assert "state.json" in runbook, \
        "红线里要点名 state.json（不许手改状态来伪造升级成功）"


def test_lease_floor_regression_still_pinned():
    """本卡不应改动租约下限逻辑；那条判据必须仍在位且能跑。

    这里只做静态确认（实际执行由卡面的第二条 verify 负责）：
    `4730460` 引入的 `idle_heartbeat_interval * 3.0` 下限不能被人顺手删掉。
    """
    src = (ROOT / "hive_cluster.py").read_text(encoding="utf-8")
    assert "idle_heartbeat_interval * 3.0" in src, (
        "租约下限（空闲心跳 × 3）不见了 —— "
        "那是 4730460 修的租约撞车，删掉会复发")
    assert (ROOT / "tests" / "test_lease_heartbeat_floor.py").is_file(), \
        "租约下限的判据不见了"
