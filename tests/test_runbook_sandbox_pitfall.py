"""T-0476：升级清单必须写清「agent 沙箱会回收集群进程」这个坑。

2026-11-25 现场（IC6RTQ4，详见 T-0473 的节点重启记录）：
`hive cluster restart` 打印「集群已启动」、当场 `health` 也通，但
**工具调用一结束，进程就被 agent 沙箱当作「脱离会话的子进程」回收**——
端口不通、集群里显示 dead。计划任务拉起同样被 Ctrl+C 结束
（Windows `LastTaskResult=0xC000013A`），`cluster.log` 里没有 traceback
（不是自己崩的，是被外部终止）。

这个坑的危险之处：**当场所有判据都满足**，最容易误判成「重启成功」。
所以它必须进操作手册，而不是只躺在记录文件里。

判据钉四件事：
1. `### 2.8` 这一节存在，且位置正确（在 2.7 之后、`## 重启顺序` 之前）；
2. 该节讲清三类信息：会被回收 / 要隔几分钟复采 / 该在普通终端或计划任务里跑；
3. 文末「一句话版本」也带上了复采步骤（大多数人只看最后那段）；
4. 不泄露口令。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "cluster" / "节点升级清单.md"

SECTION_HEAD = "### 2.8"
AFTER_HEAD = "### 2.7"
STOP_HEAD = "## 重启顺序"


@pytest.fixture(scope="module")
def text() -> str:
    assert RUNBOOK.is_file(), f"缺少升级清单：{RUNBOOK.relative_to(ROOT)}"
    return RUNBOOK.read_text(encoding="utf-8")


def _section_28(text: str) -> str:
    """取出 `### 2.8` 到下一个同级或更高级标题之间的正文。"""
    m = re.search(rf"^{re.escape(SECTION_HEAD)}.*?$", text, re.M)
    assert m, f"清单里没有 `{SECTION_HEAD}` 这一节"
    rest = text[m.end():]
    stop = re.search(r"^#{2,3}\s", rest, re.M)
    return rest[: stop.start()] if stop else rest


def test_section_28_exists_after_27_and_before_order(text):
    """位置要对：紧跟 2.7 之后、`## 重启顺序` 之前。

    放在文件末尾或塞进别的章节都算不合格 —— 那正是它之前
    「埋在记录文件里没人翻到」的老问题。
    """
    i27 = text.find(AFTER_HEAD)
    i28 = text.find(SECTION_HEAD)
    iorder = text.find(STOP_HEAD)
    assert i27 != -1, f"找不到 `{AFTER_HEAD}`"
    assert i28 != -1, f"找不到 `{SECTION_HEAD}`"
    assert iorder != -1, f"找不到 `{STOP_HEAD}`"
    assert i27 < i28, "2.8 应当排在 2.7 之后"
    assert i28 < iorder, "2.8 应当排在「重启顺序」之前（属于升级步骤的一部分）"


def test_section_28_mentions_reclaim(text):
    """必须讲清「进程会被回收」，而不是只写「注意进程可能退出」。"""
    sec = _section_28(text)
    assert re.search(r"回收|收回|结束掉|终止", sec), (
        "2.8 没提到「进程会被回收/终止」这个核心现象")
    # 要能对上现场：不是自己崩的
    assert re.search(r"traceback|自己崩|外部终止|Ctrl\+C|0xC000013A", sec, re.I), (
        "2.8 要说明「不是自己崩的、是被外部终止」，否则人会去翻 cluster.log 找异常")


def test_section_28_requires_delayed_recheck(text):
    """必须要求「隔几分钟复采」——这是唯一能识破假成功的动作。"""
    sec = _section_28(text)
    assert re.search(r"3\s*[~-]\s*5\s*分钟|几分钟|过几分钟|隔\s*\d+\s*分钟", sec), (
        "2.8 没给出「隔几分钟再采一次」的复查要求")
    assert re.search(r"复采|再采|再次确认|再查一次|重新采样", sec), (
        "2.8 要明确「重新采样 uptime」这个动作")
    assert "uptime" in sec, "复查要具体到 uptime_s，不能只说「再看看」"


def test_section_28_says_where_to_run(text):
    """必须给出正确做法：普通终端 / 计划任务，不要套在 agent 的 exec 里。"""
    sec = _section_28(text)
    assert re.search(r"普通终端|登录会话|计划任务|schtasks|\.cmd", sec), (
        "2.8 没给出「该在哪儿跑」的正确做法")
    assert re.search(r"不要.*(exec|后台|agent)|别.*(exec|后台)", sec), (
        "2.8 要明确「不要套在 agent 的 exec/后台任务里」")


def test_section_28_covers_non_windows_nodes(text):
    """不能只写 Windows。

    集群里有 FreeBSD（fb98）和 Linux 节点，只教 `schtasks`/`.cmd` 等于把它们
    漏掉了。至少要给一个脱离会话的通用做法（`setsid` / `systemd --user` /
    `rc.d` 之类），否则那几台节点的人照抄不了。

    注意：跨平台说明必须是**正文散文**，不能只出现在代码块的注释里 ——
    否则删掉正文那句、只剩一行注释，也照样能骗过判据（本卡反跑时踩过）。
    """
    sec = _section_28(text)
    # 去掉 fenced code block 后再判断，这样代码注释里的关键词不算数
    prose = re.sub(r"```.*?```", "", sec, flags=re.S)
    assert re.search(r"Linux|FreeBSD", prose), (
        "2.8 的正文（非代码块）里没提 Linux/FreeBSD 节点该怎么做")
    assert re.search(r"setsid|systemd|rc\.d|nohup|daemon", prose), (
        "2.8 的正文里要给出脱离会话拉起的具体手段（setsid / systemd / rc.d / nohup）")
    # 具体手段在代码块里也要有可抄的样例
    assert re.search(r"```sh.*?setsid|```sh.*?nohup", sec, re.S), (
        "2.8 应给出可直接照抄的 sh 代码块（setsid/nohup）")


def test_section_28_explains_reclaim_vs_crash(text):
    """必须讲清「怎么区分被回收和自己崩」。

    这是本文最实用的一条判据 —— 判错方向，人就会一头扎进
    `hive_cluster.py` 里找不存在的 bug。要能对上现场特征：
    日志无 traceback、退出码是 Ctrl+C 语义、只在 agent 里复现。
    """
    sec = _section_28(text)
    assert re.search(r"区分|怎么判断|区别|判错", sec), (
        "2.8 没说明「怎么区分被回收 vs 自己崩」")
    assert re.search(r"traceback", sec, re.I), (
        "区分手段里必须包含「看 cluster.log 有没有 traceback」")
    assert re.search(r"0xC000013A|Ctrl\+C|SIGTERM|信号", sec), (
        "区分手段里要提到退出码/信号特征")
    assert re.search(r"普通终端.*(稳定|不掉)|只在.*agent.*(掉|回收)", sec), (
        "2.8 应给出可操作的经验判据：普通终端稳定、agent 里掉 = 被回收")


def test_section_28_warns_uptime_zero_is_not_proof(text):
    """`uptime` 归零不等于原来那个进程活着——可能被重新拉起。

    只看「uptime 是个位数」会误判成功，必须要求「持续增长」。
    """
    sec = _section_28(text)
    assert re.search(r"持续增长|仍在增长|还在增长|一直在涨", sec), (
        "2.8 要说明「uptime 要持续增长」才算真活着")


def test_section_28_has_ic6rtq4_timeline(text):
    """要带现场时间线，证明这是真实踩过的坑而不是理论。"""
    sec = _section_28(text)
    assert re.search(r"09:5[0-9]|10:0[0-9]", sec), "2.8 应带 IC6RTQ4 的实际时间点"
    assert "IC6RTQ4" in sec or "T-0473" in sec, "2.8 应点名出处（IC6RTQ4 / T-0473）"


def test_one_liner_includes_recheck(text):
    """文末「一句话版本」也要带复采步骤——大多数人只看那一段。

    注意只看**那一段代码块内部**：`tail` 会一直吃到文件末尾，
    若把范围放大，2.8 正文里的「隔 3~5 分钟」会被误匹配，
    这条判据就变成永远通过了（本卡第一次写就踩了这个坑，已修）。
    """
    m = re.search(r"^##\s*一句话版本.*$", text, re.M)
    assert m, "找不到「一句话版本」段"
    tail = text[m.end():]
    # 只取紧随其后的第一个 ``` 代码块
    block = re.search(r"```.*?```", tail, re.S)
    assert block, "「一句话版本」段里没有代码块"
    body = block.group(0)
    assert re.search(r"复采|再采|再次确认|隔\s*\d+\s*[~-]?\s*\d*\s*分钟", body), (
        "「一句话版本」的代码块里没有复采步骤，只看速查的人仍会被假成功骗过")
    assert "uptime" in body, "「一句话版本」的复采要看 uptime_s，不能只说「再确认」"


def test_no_secrets_leaked(text):
    """清单是对外文档，不许出现口令。"""
    for bad in ("password=", "PASS=", "密码：", "密码:", "token="):
        assert bad not in text, f"清单里出现了疑似口令：{bad!r}"
    # 路径里的真实用户名要占位化
    assert not re.search(r"C:\\Users\\[A-Za-z0-9_.-]+\\.hive", text), (
        "示例路径应使用 <user> 占位符，不要写死真实用户名")
