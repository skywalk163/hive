"""T-0186：agent 自领任务的匹配与排序（`pending_matcher`）。

分四层测，坏在哪层一眼能看出来：

1. **路径规则**：`path_overlap` / `paths_conflict` 与 `Hive._path_overlap`、
   `Hive._paths_conflict` 逐条对齐 —— 期望值全部手工钉死，并在注释里注明对应
   `hive.py` 的哪条规则。**刻意不 import hive**：那会把 `sys.argv` 解析和整套
   文件 IO 带进纯函数测试，也掩盖“本模块不依赖 hive”这条红线。
2. **判定规则**：六条规则的匹配与不匹配，外加 reasons 点名。
3. **打分**：三个优先级档位、能力对口加分、在飞冲突扣分。
4. **排序**：确定性、同分按任务号、limit 截断、非 ready 卡不进结果。
"""

from __future__ import annotations

import pytest

import pending_matcher as pm
from conflict import _locks_conflict

# --------------------------------------------------------------------------- #
# 夹具：卡与 agent 的最小可用形状
# --------------------------------------------------------------------------- #


def card(**over) -> dict:
    """一张默认“能匹配”的卡，用例只覆盖自己关心的字段。"""
    base = {
        "id": "T-0001",
        "title": "示例卡",
        "repo": "hive",
        "state": "ready",
        "owner": None,
        "priority": "P2",
        "needs": [],
        "paths": ["pending_matcher.py"],
        "depends_on": [],
    }
    base.update(over)
    return base


def agent(**over) -> dict:
    """一个默认“能领活”的 agent，形状照 agents/trae-win.json 抄。"""
    base = {
        "name": "trae-win",
        "host": "win10",
        "mode": "manual",
        "manual": True,
        "abilities": ["code", "docs", "verify"],
        "capabilities": ["gui", "windows", "linux"],
        "repos": {
            "hive": "G:/dswork/duan-light-merge/hive",
            "light": "G:/dswork/duan-light-merge/light-merge",
        },
        "worktrees": "C:/Users/skywalk/.hive/wt/trae-win",
        "max_concurrency": 1,
    }
    base.update(over)
    return base


def table(*cards: dict) -> dict[str, dict]:
    """卡表：任务号 → 卡对象，就是 `hive.cards()` 的形状。"""
    return {c["id"]: c for c in cards}


# --------------------------------------------------------------------------- #
# 1. 路径规则：与 Hive._path_overlap 对齐
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("x,y,expected,why", [
    ("src/a.py", "src/a.py", True, "完全相同（hive.py:1190 `nx == ny`）"),
    ("src/", "src", True, "两端都去尾斜杠后相同（hive.py:1187 rstrip('/')）"),
    (" src/ ", "src/a.py", True, "两端去空白（hive.py:1187 strip()）"),
    ("src", "src/a.py", True, "父目录覆盖子路径（hive.py:1192 startswith(ny+'/')）"),
    ("src/a.py", "src", True, "反向：子路径被父目录覆盖（对称）"),
    ("src/", "src/sub/b.py", True, "多级子路径仍覆盖"),
    ("src/sub/b.py", "src/sub/", True, "子目录内的文件被该目录覆盖（反向）"),
    ("tests/", "testing/", False, "前缀相同但非目录边界（hive.py:1192 补 '/' 比较）"),
    ("src/a.py", "src/b.py", False, "同目录不同文件"),
    ("src/sub/", "src/a.py", False, "兄弟目录不互相覆盖（只有真父子才算）"),
    ("", "src/a.py", True, "空串等于任何地方（hive.py:1189）"),
    ("src/a.py", "", True, "反向：任一方空即覆盖（hive.py:1189）"),
    ("/", "src/a.py", True, "只写 '/' 去尾斜杠后为空串，仍是任何地方"),
])
def test_path_overlap_matches_hive_rule(x, y, expected, why):
    """`path_overlap` 的四种边界：父子目录 / 尾斜杠 / 空串 / 目录边界。

    每条期望值都手工钉死并注明 `hive.py` 的对应规则，本模块改了就先在这里红；
    拿不准时用 `Hive._path_overlap` 手工核对同一组输入，而不是在测试里 import hive。
    """
    assert pm.path_overlap(x, y) is expected, why
    assert pm.path_overlap(y, x) is expected, "覆盖关系必须对称"


def test_path_overlap_ignores_dot_slash_prefix():
    """`./src` 与 `src` 在本模块**不算**重叠 —— 与 `Hive._path_overlap` 一致。

    hive.py:1187 只做 strip + rstrip('/')，不剥 `./`；`conflict._norm` 才剥。
    这是刻意保留的不一致：`paths` 一律写成不带 `./` 的相对路径，卡面校验会拦。
    """
    assert pm.path_overlap("./src", "src/a.py") is False
    assert pm.path_overlap("./src/a.py", "./src/a.py") is True      # 完全相同仍成立


def test_paths_conflict_empty_paths_is_conservative():
    """任一方 `paths` 为空即冲突：未声明 = 可能改任何地方（hive.py:1220）。"""
    assert pm.paths_conflict(card(paths=[]), card(paths=["src/a.py"])) is True
    assert pm.paths_conflict(card(paths=["src/a.py"]), card(paths=[])) is True
    assert pm.paths_conflict(card(paths=[]), card(paths=[])) is True
    # 字段整个缺失等同于空
    a = card()
    a.pop("paths")
    assert pm.paths_conflict(a, card(paths=["docs/"])) is True


def test_paths_conflict_uses_locks_when_both_declared():
    """双方都写了非空 locks 时按精确集合判（hive.py:1216-1217）。

    同样写 `paths: src/` 的两张卡，锁到互不相干的文件就不冲突 —— 这正是
    T-0163 引入 `locks` 的目的。期望值先与 `conflict._locks_conflict` 对一次，
    确认本模块确实把活交给了它，而不是自己另写一套。
    """
    a = card(paths=["src/"], locks=["src/a.py"])
    b = card(paths=["src/"], locks=["src/b.py"])
    assert pm.paths_conflict(a, b) is False
    assert pm.paths_conflict(a, b) is _locks_conflict(["src/a.py"], ["src/b.py"])

    # 目录锁覆盖文件锁 -> 冲突
    assert pm.paths_conflict(card(paths=["src/"], locks=["src/"]),
                             card(paths=["src/"], locks=["src/a.py"])) is True
    # 通配锁命中 -> 冲突
    assert pm.paths_conflict(card(paths=["src/"], locks=["src/*.py"]),
                             card(paths=["src/"], locks=["src/a.py"])) is True
    # 同一文件两次 -> 冲突
    assert pm.paths_conflict(card(paths=["src/"], locks=["src/a.py"]),
                             card(paths=["src/"], locks=["src/a.py"])) is True


def test_paths_conflict_falls_back_when_only_one_side_has_locks():
    """只有一方写 locks 时回退 paths 目录规则（hive.py:1218-1222）。

    保守优先不能被“少写个字段”绕过：A 精确声明只改 `src/a.py`，
    B 只写 `paths: src/`，仍然按目录前缀判冲突。
    """
    assert pm.paths_conflict(
        card(paths=["src/"], locks=["src/a.py"]),
        card(paths=["src/b.py"])) is True
    # 目录不同则回退后确实不冲突（locks 不参与）
    assert pm.paths_conflict(
        card(paths=["src/"], locks=["src/a.py"]),
        card(paths=["docs/"])) is False
    # 空 locks 列表按“没声明”处理，不走精确分支
    assert pm.paths_conflict(
        card(paths=["src/"], locks=[]),
        card(paths=["src/a.py"], locks=[])) is True


# --------------------------------------------------------------------------- #
# 2. 判定规则
# --------------------------------------------------------------------------- #


def test_matched_basic():
    """正常命中：repo 登记过 + needs 全覆盖 + ready 无人持有。"""
    verdict = pm.explain_match(
        card(needs=["windows"]), agent())
    assert verdict["matched"] is True
    # 100 基础 + 20（P2）+ 5（能力对口）
    assert verdict["score"] == 125
    assert any("仓匹配" in r for r in verdict["reasons"])
    assert any("能力全覆盖" in r for r in verdict["reasons"])


def test_repo_not_registered():
    """repo 不在 `agent["repos"]` 的键里 -> 不匹配，reasons 说明是哪个仓。"""
    verdict = pm.explain_match(card(repo="lightharness"), agent())
    assert verdict["matched"] is False
    assert verdict["score"] == 0
    assert any("仓不匹配" in r and "lightharness" in r for r in verdict["reasons"])


def test_repo_missing_on_card():
    """卡上仓字段缺失同样不匹配：没仓就没法映射 worktree。"""
    verdict = pm.explain_match(card(repo=""), agent())
    assert verdict["matched"] is False
    assert any("仓不匹配" in r for r in verdict["reasons"])


def test_needs_missing_one_tag_is_named():
    """needs 缺一个能力标签 -> 不匹配，reasons 点名缺的那个标签。"""
    verdict = pm.explain_match(
        card(needs=["windows", "freebsd"]), agent())
    assert verdict["matched"] is False
    lack_lines = [r for r in verdict["reasons"] if r.startswith("能力缺失")]
    assert len(lack_lines) == 1
    # 只列缺的：有 freebsd、没有 windows
    assert lack_lines[0].endswith("freebsd"), lack_lines[0]
    assert "windows" not in lack_lines[0], "命中的标签不该出现在缺失清单里"


def test_abilities_and_capabilities_are_merged():
    """`abilities` 与 `capabilities` 两个键都读，任一侧命中即算有。"""
    verdict = pm.explain_match(
        card(needs=["freebsd"]),
        agent(abilities=["code"], capabilities=["freebsd"]))
    assert verdict["matched"] is True
    # 两个键都缺失按空列表处理，不炸
    verdict = pm.explain_match(
        card(needs=["freebsd"]),
        agent(abilities=None, capabilities=None))
    assert verdict["matched"] is False


def test_verify_agent_rejects_cards_with_paths():
    """验收模式 agent 不接改代码的卡（paths 非空）。"""
    v = agent(mode="verify", manual=False)
    assert pm.explain_match(card(paths=["src/a.py"]), v)["matched"] is False
    assert any("验收模式" in r for r in
               pm.explain_match(card(paths=["src/a.py"]), v)["reasons"])


def test_verify_agent_accepts_cards_without_paths():
    """验收模式 agent 可以接纯验收卡（paths 为空）。"""
    v = agent(mode="verify", manual=False)
    verdict = pm.explain_match(card(paths=[]), v)
    assert verdict["matched"] is True
    assert any("验收模式可接" in r for r in verdict["reasons"])


def test_verify_mode_detected_by_mode_alone():
    """`mode == "verify"` 单独出现也认（卡的两种写法都要接住）。"""
    for v in (agent(mode="verify", manual=None),
              agent(mode="verify", manual=False),
              agent(mode="verify", manual=True)):
        assert pm.explain_match(card(paths=["x.py"]), v)["matched"] is False
    # 非验收模式不受影响
    assert pm.explain_match(card(paths=["x.py"]),
                            agent(mode="exec", manual=False))["matched"] is True


def test_dependency_not_done_is_named():
    """依赖未 done -> 不匹配，reasons 点名前置卡。"""
    cards = table(
        card(id="T-0100", state="assigned", owner="fb88-dsh"),
        card(id="T-0101", state="ready"),
    )
    verdict = pm.explain_match(card(depends_on=["T-0100", "T-0101"]),
                               agent(), cards=cards)
    assert verdict["matched"] is False
    text = " ".join(verdict["reasons"])
    assert "T-0100" in text and "T-0101" in text


def test_dependency_done_allows_match():
    """前置卡全 done -> 依赖就绪，正常匹配。"""
    cards = table(card(id="T-0100", state="done", owner="fb88-dsh"))
    verdict = pm.explain_match(card(depends_on=["T-0100"]), agent(), cards=cards)
    assert verdict["matched"] is True
    assert any("依赖就绪" in r for r in verdict["reasons"])


def test_missing_dependency_card_counts_as_not_done():
    """卡表里查不到的前置卡按未完成处理：宁可不派，也别把缺失当就绪。"""
    verdict = pm.explain_match(card(depends_on=["T-9999"]), agent(), cards={})
    assert verdict["matched"] is False
    assert any("T-9999" in r for r in verdict["reasons"])


def test_without_cards_table_dependency_check_is_declared_skipped():
    """不传 cards 时不假装查过依赖 —— reasons 里必须留白。"""
    verdict = pm.explain_match(card(depends_on=["T-9999"]), agent())
    assert verdict["matched"] is True
    assert any("未提供卡片表" in r for r in verdict["reasons"])


def test_inflight_path_conflict_penalises_but_still_matches():
    """与在飞卡抢同一片文件 -> 仍匹配，但 score 减 40，且点名对手。"""
    cards = table(
        card(id="T-0002", state="assigned", owner="trae-win",
             paths=["pending_matcher.py"], priority="P1"),
        card(id="T-0001", paths=["pending_matcher.py"]),
    )
    verdict = pm.explain_match(cards["T-0001"], agent(), cards=cards)
    assert verdict["matched"] is True, "审批权在 claim 时的租约，匹配阶段不判死"
    # 100 + 20（P2）+ 0（needs 为空）− 40
    assert verdict["score"] == 80
    assert any("T-0002" in r and "抢同一片文件" in r for r in verdict["reasons"])
    # 有冲突时不能再冒出一条“不冲突”，否则同一份 reasons 自相矛盾
    assert not any("与在飞的卡不冲突" in r for r in verdict["reasons"])


def test_inflight_conflict_penalty_is_per_card():
    """每条冲突各扣 40：两张在飞卡抢同一片文件就是 −80。"""
    cards = table(
        card(id="T-0002", state="running", paths=["pending_matcher.py"]),
        card(id="T-0003", state="claimed", paths=["pending_matcher.py"]),
        card(id="T-0001", paths=["pending_matcher.py"], priority="P3"),
    )
    verdict = pm.explain_match(cards["T-0001"], agent(), cards=cards)
    # 100 + 10（P3）− 40 − 40
    assert verdict["score"] == 30
    assert sum(1 for r in verdict["reasons"] if "抢同一片文件" in r) == 2


def test_inflight_conflict_only_within_same_repo():
    """只在同仓内比 paths：不同仓的路径不在一棵树里，不该互相扣分。"""
    cards = table(
        card(id="T-0002", repo="light", state="running",
             paths=["pending_matcher.py"]),
        card(id="T-0001", paths=["pending_matcher.py"]),
    )
    verdict = pm.explain_match(cards["T-0001"], agent(), cards=cards)
    assert verdict["score"] == 120, "跨仓同名文件不算冲突"
    assert not any("抢同一片文件" in r for r in verdict["reasons"])


def test_inflight_locks_drive_conflict_decision():
    """在飞卡与本卡都写 locks 时，冲突判定跟着 locks 走（不是 paths）。"""
    cards = table(
        card(id="T-0002", state="assigned", owner="x",
             paths=["src/"], locks=["src/b.py"]),
        card(id="T-0001", paths=["src/"], locks=["src/a.py"]),
    )
    verdict = pm.explain_match(cards["T-0001"], agent(), cards=cards)
    assert verdict["score"] == 120, "locks 精确到互不相干的文件，不该扣分"
    assert any("与在飞的卡不冲突" in r for r in verdict["reasons"])


def test_claimed_state_counts_as_in_flight():
    """claimed 也算在飞：租约已推但还没流转到 assigned 的卡同样占着文件。"""
    cards = table(
        card(id="T-0002", state="claimed", owner="x",
             paths=["pending_matcher.py"]),
        card(id="T-0001", paths=["pending_matcher.py"]),
    )
    assert pm.explain_match(cards["T-0001"], agent(), cards=cards)["score"] == 80


def test_done_cards_keep_owner_but_not_in_flight():
    """done 卡保留 owner 但不再占文件 —— 真实卡表就是这个形状，不排除会全仓误撞。

    这是真实数据跑出来的坑：本仓 70 张卡里 62 张 state=done 且 owner 有值
    （`Hive.cmd_close` 不清理 owner）。若把“有 owner”当在飞，任何 `paths` 为空的卡
    都会跟全仓 46 张卡判冲突，分数被扣成负几百，排序完全失真。
    """
    cards = table(
        card(id="T-0002", state="done", owner="fb88-dsh",
             paths=["pending_matcher.py"]),
        card(id="T-0003", state="done", owner="trae-win", paths=[]),
        card(id="T-0001", paths=["pending_matcher.py"]),
    )
    verdict = pm.explain_match(cards["T-0001"], agent(), cards=cards)
    assert verdict["matched"] is True
    assert verdict["score"] == 120, "已收口的卡不该扣分"
    assert any("与在飞的卡不冲突" in r for r in verdict["reasons"])


def test_card_with_result_but_not_closed_still_counts_in_flight():
    """成果已提交但还没 close 的卡仍算在飞 —— 本模块拿不到 res 分支，保守偏严。

    真实数据里 T-0185 就是这个形状（state=assigned 但 results/T-0185.json 已存在）。
    `Hive._in_flight` 会把它排除；本模块多扣 40 分。这是刻意的降级：
    纯函数不读 git ref，宁可多扣也不能漏判“这卡可能还在改文件”。
    """
    cards = table(
        card(id="T-0002", state="assigned", owner="doubao-win",
             paths=["pending_matcher.py"]),
        card(id="T-0001", paths=["pending_matcher.py"]),
    )
    assert pm.explain_match(cards["T-0001"], agent(), cards=cards)["score"] == 80


def test_closed_cards_are_terminal_too():
    """`closed`（作废卡）同样是终态：不会再改文件，即便 owner 还挂着。

    这才是 T-0185 之后最常见的形状：卡没跑出结果、被 `hive close` 作废，
    owner 留在卡面上。作废卡跟 done 一样不该占文件。
    """
    cards = table(
        card(id="T-0002", state="closed", owner="workbuddy",
             paths=["pending_matcher.py"]),
        card(id="T-0001", paths=["pending_matcher.py"]),
    )
    verdict = pm.explain_match(cards["T-0001"], agent(), cards=cards)
    assert verdict["score"] == 120
    assert any("与在飞的卡不冲突" in r for r in verdict["reasons"])


def test_in_flight_reasons_collapse_after_limit():
    """冲突超过 CONFLICT_REASON_LIMIT 张时折叠成汇总，分数仍按全量扣。"""
    many = [card(id=f"T-01{n:02d}", state="assigned", owner="x",
                 paths=["pending_matcher.py"]) for n in range(1, 9)]
    cards = table(*many, card(id="T-0001", paths=["pending_matcher.py"]))
    verdict = pm.explain_match(cards["T-0001"], agent(), cards=cards)
    detail = [r for r in verdict["reasons"] if "抢同一片文件" in r and "另有" not in r]
    assert len(detail) == pm.CONFLICT_REASON_LIMIT
    assert any("另有 3 张在飞卡" in r for r in verdict["reasons"])
    # 100 + 20 − 40×8
    assert verdict["score"] == 100 + 20 - 320


def test_reasons_accumulate_instead_of_short_circuit():
    """多条不匹配理由一次全列出来，别让人改一版再问一次。"""
    verdict = pm.explain_match(
        card(repo="nope", needs=["freebsd"]), agent())
    text = " ".join(verdict["reasons"])
    assert "仓不匹配" in text and "能力缺失" in text


# --------------------------------------------------------------------------- #
# 3. 打分
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pri,expected", [
    ("P1", 135),   # 100 + 30 + 5
    ("P2", 125),   # 100 + 20 + 5
    ("P3", 115),   # 100 + 10 + 5
])
def test_priority_scoring(pri, expected):
    """P1 > P2 > P3，且档位加分是固定值。"""
    verdict = pm.explain_match(card(priority=pri, needs=["windows"]), agent())
    assert verdict["score"] == expected


def test_unknown_priority_gets_no_bonus():
    """P0/空/畸形优先级不加分，也不炸。"""
    for pri in ("P0", "", "HIGH", None):
        verdict = pm.explain_match(card(priority=pri), agent())
        assert verdict["score"] == 100, f"priority={pri!r} 不该带加分"


def test_needs_bonus_requires_non_empty_needs():
    """needs 为空时不加“能力对口”这 5 分：没有要求就谈不上对口。"""
    verdict = pm.explain_match(card(needs=[]), agent())
    assert verdict["score"] == 120
    assert not any("完全对口" in r for r in verdict["reasons"])


# --------------------------------------------------------------------------- #
# 4. 排序、过滤、确定性
# --------------------------------------------------------------------------- #


def test_sort_by_score_then_id():
    """按 score 降序；同分按任务号升序。"""
    cards = table(
        card(id="T-0003", priority="P3"),
        card(id="T-0002", priority="P1"),
        card(id="T-0001", priority="P2"),
        card(id="T-0009", priority="P2"),
    )
    out = pm.match_tasks_for_agent(cards, agent())
    assert [r["task"]["id"] for r in out] == ["T-0002", "T-0001", "T-0009", "T-0003"]
    assert [r["score"] for r in out] == [130, 120, 120, 110]


def test_limit_truncates():
    """limit 截断；limit=0 得空表；负数按 0 处理。"""
    cards = table(*[card(id=f"T-000{i}", priority="P2") for i in range(1, 6)])
    assert len(pm.match_tasks_for_agent(cards, agent())) == 5
    assert len(pm.match_tasks_for_agent(cards, agent(), limit=2)) == 2
    assert pm.match_tasks_for_agent(cards, agent(), limit=0) == []
    assert pm.match_tasks_for_agent(cards, agent(), limit=-3) == []


def test_non_ready_cards_are_excluded():
    """blocked / assigned / running / done / needs_review 都不进结果。"""
    cards = table(
        card(id="T-0001"),
        card(id="T-0002", state="blocked"),
        card(id="T-0003", state="assigned", owner="trae-win"),
        card(id="T-0004", state="running"),
        card(id="T-0005", state="done"),
        card(id="T-0006", state="needs_review"),
        card(id="T-0007", state="ready", owner="fb88-dsh"),   # ready 但有人持有
    )
    out = pm.match_tasks_for_agent(cards, agent())
    assert [r["task"]["id"] for r in out] == ["T-0001"]


def test_empty_input():
    """空卡表得空列表，不抛异常。"""
    assert pm.match_tasks_for_agent({}, agent()) == []
    assert pm.match_tasks_for_agent(None, agent()) == []


def test_state_and_owner_are_stripped():
    """state / owner 两端空白不影响判定（parse_card 已 strip，这里兜一层）。

    卡面在 git 里被手工编辑过、值带了空格或换行时，别因为一个空白字符就判不可领。
    """
    assert pm.explain_match(card(state=" ready ", owner="  "), agent())["matched"] is True
    assert pm.explain_match(card(state="ready\n", owner="\t"), agent())["matched"] is True
    # 空白之外的差异仍然拦下
    assert pm.explain_match(card(state=" ready-but-not ", owner=""), agent())["matched"] is False


def test_in_flight_state_is_stripped_too():
    """在飞判定里的 state 也去空白：` assigned ` 仍算在飞（别因空白漏判冲突）。"""
    cards = table(
        card(id="T-0002", state=" assigned ", owner="x",
             paths=["pending_matcher.py"]),
        card(id="T-0003", state="done ", owner="y", paths=["pending_matcher.py"]),
        card(id="T-0001", paths=["pending_matcher.py"]),
    )
    verdict = pm.explain_match(cards["T-0001"], agent(), cards=cards)
    # 只跟 T-0002 扣 40；T-0003 带空白的 done 仍是终态，不扣
    assert verdict["score"] == 100 + 20 - 40
    assert any("T-0002" in r for r in verdict["reasons"])


def test_result_shape():
    """返回结构固定：task / score / reasons 三键，task 是原卡对象本身。"""
    c = card(id="T-0001")
    out = pm.match_tasks_for_agent(table(c), agent())
    assert len(out) == 1
    assert set(out[0]) == {"task", "score", "reasons"}
    assert out[0]["task"] is c
    assert isinstance(out[0]["score"], int)
    assert isinstance(out[0]["reasons"], list)


def test_deterministic_across_calls_and_dict_order():
    """同一输入两次调用结果完全相同；打乱卡表插入顺序也不影响输出。"""
    cards = table(
        card(id="T-0003", priority="P2"),
        card(id="T-0001", priority="P2"),
        card(id="T-0002", priority="P1"),
        card(id="T-0004", priority="P3"),
    )
    first = pm.match_tasks_for_agent(cards, agent())
    second = pm.match_tasks_for_agent(cards, agent())
    assert first == second

    reversed_cards = dict(reversed(list(cards.items())))
    third = pm.match_tasks_for_agent(reversed_cards, agent())
    assert [r["task"]["id"] for r in third] == [r["task"]["id"] for r in first]
    assert [r["score"] for r in third] == [r["score"] for r in first]


def test_module_does_not_import_hive():
    """红线：本模块不 import hive（避免 sys.argv 解析与运行时循环依赖）。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pm))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "hive" not in imported, f"pending_matcher 不得 import hive，实际 import 了 {imported}"
    # 依赖面就这么窄：标准库 + conflict
    assert imported <= {"__future__", "conflict"}, f"多余的依赖：{imported}"
