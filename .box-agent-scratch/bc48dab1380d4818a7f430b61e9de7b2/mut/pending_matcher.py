"""agent 自领任务的匹配判定：一张卡适不适合这个 agent。

## 为什么单独一个模块

派发流水线的终局是“agent 自己定时来领任务”。现在判定逻辑埋在 `Hive.cmd_dispatch`
里，那是**总控视角**：它一边判“谁适合”，一边还在改负载、写计划、拿占用集。
agent 主动来问“我该领哪张”时，既拿不到同样的判定，也不该触发那些写操作。

这里把判定抽成**只读纯函数**，作为 `hive_list_pending --for-agent <id>` 与
MCP 工具 `hive_list_pending`（接线见 T-0188）的唯一真源。

硬约束：

- 纯标准库，只 import `conflict`（文件级锁的精确判定）；
- **不 import `hive.py`** —— 那会带进 `sys.argv` 解析与运行时循环依赖；
- 不读写任何文件、不碰网络、不判断租约、不 claim；
- 同一输入必得同一输出（排序不依赖 dict 插入顺序）。

## 判定规则（顺序即优先级，语义对齐 `Hive.cmd_dispatch`，hive.py:1285 起）

| # | 规则 | 不满足时 |
|---|---|---|
| 1 | `state == "ready"` 且 `owner` 为空 | `matched=False` |
| 2 | `repo` 在 `agent["repos"]` 的键里 | `matched=False` |
| 3 | `agent` 的 `abilities ∪ capabilities` 覆盖 `card["needs"]` 全部标签 | `matched=False`，reasons 点名缺哪些 |
| 4 | `agent` 不是验收模式；若是，`card["paths"]` 必须为空 | `matched=False` |
| 5 | `depends_on` 里每张卡都已 `done`（需传 `cards`） | `matched=False`，reasons 点名前置卡 |
| 6 | 与 in-flight 卡的 `paths`/`locks` 不冲突 | **仍匹配**，每条冲突扣 40 分 |

第 6 条刻意不判 False：审批权在 claim 时的租约（`Hive.cmd_claim` 抢锁），
不在匹配阶段。这里只负责把“会跟谁抢文件”提前说清楚，让人自己决定要不要现在领。

## 打分（固定值，便于测试钉死）

- 基础 100；
- `priority`：`P1` +30 / `P2` +20 / `P3` +10 / 其它 +0；
- `needs` 非空且完全命中 +5（体现“专门对口”）；
- 每条 in-flight 冲突 −40；
- `paths` 是否落在 agent 的 worktree 里**不参与打分**：那是路径预算问题，
  本模块不猜，只用 `paths_conflict` 判冲突。

`matched=False` 的卡统一返回 `score=0` —— 排序只用得到能领的卡，
未匹配的分数没有意义，留 0 让调用方不容易误判“分数低但能领”。

## 与 `cmd_dispatch` 的三处有意差异

1. **不排除 manual 模式 agent**。dispatch 里的 `manual` 分支（hive.py:1322）是
   为了不让总控把活硬塞给等人操作的 agent；本模块服务的是 agent **主动来问**，
   manual 正是它该用的场景。
2. **不判负载上限**（`max_concurrency`）。负载是“已派/在飞多少张”的运行时事实，
   不是卡的属性；本模块只看卡，调用方要限流自己再过滤。
3. **不做“只派 hive 仓”的收口**。那是 dispatch 的过渡策略（其它仓 worktree 约定未定），
   不属于“卡适不适合 agent”，由接线层决定。

## 冲突判定口径与 `Hive._paths_conflict` 一致

`paths_conflict(a, b)` 读的是**卡对象**（card dict），不是裸 paths 列表：

- 双方都写了非空 `locks` → 交给 `conflict._locks_conflict` 按精确集合判；
- 否则回退目录前缀规则：任一方 `paths` 为空即冲突（未声明 = 可能改任何地方）；
- 目录前缀规则复刻 `Hive._path_overlap`（hive.py:1185）：去空白、去尾斜杠、
  空串等于“任何地方”、父子目录互相覆盖，且按 `/` 目录边界比较
  （`tests/` 与 `testing/` 不冲突）。
"""

from __future__ import annotations

from conflict import _locks_conflict, normalize_locks

# --------------------------------------------------------------------------- #
# 打分常量：全是固定值，测试直接钉死这些数字
# --------------------------------------------------------------------------- #

BASE_SCORE = 100
PRIORITY_SCORE = {"P1": 30, "P2": 20, "P3": 10}
NEEDS_BONUS = 5
CONFLICT_PENALTY = -40

# 冲突理由最多逐条列这么多张，其余折成一句汇总。
# 真实卡表里 `paths` 为空的卡会跟全仓每一张在飞卡冲突（46 张），
# 逐条打出来会把真正有用的信息淹掉；分数仍按全量冲突数照扣。
CONFLICT_REASON_LIMIT = 5

# “在飞”包含 claimed：租约分支已推但还没流转到 assigned 的卡同样占着文件。
# hive.py 的 IN_FLIGHT_STATES 只有 assigned/running，这里多认 claimed。
IN_FLIGHT_STATES = ("assigned", "claimed", "running")

# 终态：卡已经收口，即便 owner 还挂在卡面上也不会再改文件。
# 真实卡表里 done 的卡**保留 owner**（70 张里 62 张如此），不排除就会把整仓
# 误判成“全都跟我在抢同一片文件”（见 tests 里 test_done_cards_keep_owner_but_not_in_flight）。
TERMINAL_STATES = ("done", "closed")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def _as_list(value) -> list[str]:
    """把卡面/agent 配置里的字段统一成“非空字符串列表”。

    frontmatter 单值写法（`paths: src/a.py`）会被解析成字符串，直接迭代会拆成单字符，
    所以先按标量包一层。`None` / `""` / `[]` 一律变空列表。
    """
    if value is None:
        return []
    items = [value] if isinstance(value, (str, bytes)) else list(value)
    out: list[str] = []
    for it in items:
        s = str(it).strip()
        if s:
            out.append(s)
    return out


def _is_verify_agent(agent: dict) -> bool:
    """agent 是不是验收模式（只验收、不改代码）。

    `mode == "verify"` 与 `manual is False and mode == "verify"` 是同义的，
    卡面明确要求两种写法都认 —— 保留后一种是为了将来 `manual` 的默认值变严格时
    行为不漂移。
    """
    mode = str(agent.get("mode") or "").strip()
    if mode == "verify":
        return True
    return agent.get("manual") is False and mode == "verify"


def _in_flight(card: dict) -> bool:
    """这张卡是不是“在飞”：有人持有且还没收口，或状态落在 assigned/claimed/running。

    **先看终态**：`done` / `closed` 的卡已经把活干完了，`owner` 只是历史痕迹
    （`Hive.cmd_close` 不清理 owner），它不再改文件，不能算“抢同一片文件”。
    这一点跟 `Hive._in_flight`（hive.py:1224）口径略有不同 —— 那边先按
    `IN_FLIGHT_STATES` 过滤，终态卡天然进不来，效果等价。

    反过来，`owner` 有值但 state 是空的畸形卡按“有人持有”处理（保守）。

    与 `Hive._in_flight` 尚存一处差异：那边还会用 `result_info(tid)` 排除
    “成果已提交但还没 close”的卡（真实数据里 T-0185 就是这个形状）。
    本模块是纯函数，拿不到 res 分支，所以那张卡仍算在飞、仍会扣分 ——
    偏保守（多扣分比漏扣分安全），接线层若已知道结果可以先把 `state` 改掉再传进来。
    """
    state = str(card.get("state") or "").strip()
    if state in TERMINAL_STATES:
        return False
    if str(card.get("owner") or "").strip():
        return True
    return state in IN_FLIGHT_STATES


# --------------------------------------------------------------------------- #
# 路径重叠 / 冲突
# --------------------------------------------------------------------------- #


def path_overlap(x: str, y: str) -> bool:
    """两条路径声明是否相互覆盖（含父子目录）。

    复刻 `Hive._path_overlap`（hive.py:1185），四条规则一字不差：

    1. 两端去空白、去尾斜杠后比较（`src/` == `src`）；
    2. 任一方为空串 → `True`：空串等于“任何地方”（hive.py:1189）；
    3. 完全相同 → `True`（hive.py:1190）；
    4. 一方是另一方的子路径 → `True`，比较时补 `/` 保证按目录边界走
       （`tests/` 与 `testing/` 不算冲突，hive.py:1192）。
    """
    nx = str(x if x is not None else "").strip().rstrip("/")
    ny = str(y if y is not None else "").strip().rstrip("/")
    if not nx or not ny:
        return True
    if nx == ny:
        return True
    return nx.startswith(ny + "/") or ny.startswith(nx + "/")


def paths_conflict(a: dict, b: dict) -> bool:
    """两张卡声明的 `paths` / `locks` 会不会打架。

    判定顺序与 `Hive._paths_conflict`（hive.py:1194）一致：

    1. 双方都声明了非空 `locks` → `conflict._locks_conflict` 按精确文件集合判
       （`src/a.py` vs `src/b.py` 不冲突，`src/` vs `src/a.py` 冲突）；
    2. 否则回退目录前缀规则：**任一方 `paths` 为空即冲突**
       （未声明 = 可能改任何地方，hive.py:1220）。

    保守优先没有被削弱：只有两张卡都主动写了 `locks` 才走精确逻辑，
    少写一方仍然判冲突 —— 否则少写个字段就能绕过并发保护。
    """
    a = a or {}
    b = b or {}
    la = normalize_locks(a.get("locks"))
    lb = normalize_locks(b.get("locks"))
    if la and lb:
        return _locks_conflict(la, lb)
    pa = _as_list(a.get("paths"))
    pb = _as_list(b.get("paths"))
    if not pa or not pb:
        return True
    return any(path_overlap(x, y) for x in pa for y in pb)


# --------------------------------------------------------------------------- #
# 匹配判定
# --------------------------------------------------------------------------- #


def explain_match(card: dict, agent: dict, *,
                  cards: dict[str, dict] | None = None) -> dict:
    """这张卡适不适合这个 agent，并把理由逐条讲清楚。

    返回 `{"matched": bool, "reasons": [str], "score": int}`。

    `cards` 是“全量卡表”（任务号 → 卡对象），用于第 5、6 条规则。
    不传就只能判前四条，reasons 里会明说依赖与在飞冲突没检查 ——
    宁可显式留白，也不假装查过了。

    理由按规则顺序累积（不是遇错即停）：一条链上可能同时有“仓不匹配”和
    “能力缺失”，一次全列出来比让人改一版再问一次强。
    """
    card = card or {}
    agent = agent or {}
    reasons: list[str] = []
    fatal = False

    def deny(msg: str) -> None:
        nonlocal fatal
        fatal = True
        reasons.append(msg)

    # 1. 可领状态：ready 且没有人持有
    state = str(card.get("state") or "").strip()
    owner = str(card.get("owner") or "").strip()
    if state != "ready":
        deny(f"状态不可领（state={state or '未填'}，要 ready）")
    elif owner:
        deny(f"已有人持有（owner={owner}）")
    else:
        reasons.append("状态可领（ready，无人持有）")

    # 2. 仓匹配：agent 必须登记过这张卡的 repo
    repo = str(card.get("repo") or "").strip()
    repos = agent.get("repos") or {}
    if repo and repo in repos:
        reasons.append(f"仓匹配（{repo}）")
    else:
        deny(f"仓不匹配（agent 未登记 {repo or '未填仓'}）")

    # 3. 能力匹配：abilities 与 capabilities 的并集要盖住 needs
    needs = _as_list(card.get("needs"))
    have = set(_as_list(agent.get("abilities"))) | set(_as_list(agent.get("capabilities")))
    lack = sorted(n for n in needs if n not in have)
    if lack:
        deny("能力缺失：" + ", ".join(lack))
    elif needs:
        reasons.append("能力全覆盖（" + ", ".join(sorted(needs)) + "）")
    else:
        reasons.append("卡片未要求特定能力")

    # 4. 验收模式不接改代码的卡（paths 非空即算改代码）
    paths = _as_list(card.get("paths"))
    if _is_verify_agent(agent) and paths:
        deny("验收模式不接改代码的卡（paths 非空：" + ", ".join(paths) + "）")
    elif _is_verify_agent(agent):
        reasons.append("验收模式可接（卡未声明 paths）")

    # 5. 依赖就绪（需要 cards 才能查）
    deps = _as_list(card.get("depends_on"))
    if cards is None:
        reasons.append("未提供卡片表：依赖与在飞冲突未检查")
    else:
        bad = sorted(d for d in deps if not _dep_done(d, cards))
        if bad:
            deny("依赖未完成：" + ", ".join(bad))
        elif deps:
            reasons.append("依赖就绪（" + ", ".join(sorted(deps)) + "）")

    # 6. 与 in-flight 卡抢文件：只扣分，不判死（审批权在 claim 的租约）
    conflicts: list[str] = []
    if cards is not None:
        me = str(card.get("id") or "").strip()
        for tid in sorted(cards):
            if tid == me:
                continue
            other = cards[tid] or {}
            if str(other.get("id") or tid).strip() == me:
                continue
            if not _in_flight(other):
                continue
            # 与 cmd_dispatch 一致：只在同仓内比 paths（不同仓的路径不在一棵树里）
            if str(other.get("repo") or "").strip() != repo:
                continue
            if paths_conflict(card, other):
                conflicts.append(tid)
        # 冲突逐条点名，但超过上限就折成汇总：paths 为空的卡会跟全仓在飞卡全撞，
        # 逐条列 40 多条会把依赖/能力这些真正的原因淹掉。分数照扣全量。
        for tid in conflicts[:CONFLICT_REASON_LIMIT]:
            reasons.append(f"与在飞的 {tid} 抢同一片文件（{CONFLICT_PENALTY}）")
        rest = len(conflicts) - CONFLICT_REASON_LIMIT
        if rest > 0:
            reasons.append(f"另有 {rest} 张在飞卡同样抢同一片文件"
                           f"（合计 {len(conflicts)} 张，共 {CONFLICT_PENALTY * len(conflicts)}）")
        if not conflicts:
            reasons.append("与在飞的卡不冲突")

    if fatal:
        return {"matched": False, "reasons": reasons, "score": 0}

    pri = str(card.get("priority") or "").strip().upper()
    score = BASE_SCORE
    if pri in PRIORITY_SCORE:
        score += PRIORITY_SCORE[pri]
        reasons.append(f"优先级 {pri}（+{PRIORITY_SCORE[pri]}）")
    if needs and not lack:
        score += NEEDS_BONUS
        reasons.append(f"能力完全对口（+{NEEDS_BONUS}）")
    score += CONFLICT_PENALTY * len(conflicts)
    return {"matched": True, "reasons": reasons, "score": score}


def _dep_done(tid: str, cards: dict[str, dict]) -> bool:
    """前置卡是否已完成。

    只认 `state == "done"`：卡表里查不到的前置卡一并按“未完成”处理
    （保守 —— 宁可让人去查，也别把依赖缺失当成就绪）。
    """
    dep = cards.get(tid)
    if not dep:
        return False
    return str(dep.get("state") or "").strip() == "done"


def match_tasks_for_agent(cards: dict[str, dict], agent: dict, *,
                          limit: int | None = None) -> list[dict]:
    """这个 agent 现在能领哪些卡，按推荐顺序排好。

    返回 `[{"task": <卡对象>, "score": int, "reasons": [str]}]`：

    - 只含 `matched=True` 的卡；
    - 按 score 降序、同分按任务号升序（同一输入必得同序，不依赖 dict 插入顺序）；
    - `limit` 截断（`0` 得空表，负数按 0 处理）；
    - 不判租约、不判负载、不 claim —— 本函数只回答“适不适合”。
    """
    rows: list[tuple[str, dict]] = []
    # 刻意不预先排序输入：确定性必须由下面的 sort key 保证，而不是“迭代时恰好有序”。
    # （先前写成 `for tid in sorted(cards)`，导致同分 tie-break 的 key 从未被真正考验过）
    for tid, card in (cards or {}).items():
        card = card or {}
        verdict = explain_match(card, agent, cards=cards)
        if not verdict["matched"]:
            continue
        rows.append((str(tid), {"task": card, "score": verdict["score"],
                                "reasons": verdict["reasons"]}))
    # 同分按任务号升序：用卡表里的键，不用卡里的 id 字段（后者可能缺失）
    rows.sort(key=lambda row: (-row[1]["score"], row[0]))
    items = [row[1] for row in rows]
    if limit is not None:
        items = items[:max(0, int(limit))]
    return items
