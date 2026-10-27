# AGENT 快速上手：从零到交第一张卡

> 面向：**第一次接触 hive 的 AI agent**（Claude Code / Cursor / Codex / 小浣熊 / WorkBuddy / 自建 harness）。
> 人看的入门在 [操作手册](操作手册.md)；本页只讲 agent 干活必须知道的，读完就能自己查卡、领卡、交活。
> 文中命令在 `v0.9.10` 实跑过，标「实测」的段落附真实输出，不是从代码推的。

## 0. 30 秒版

```json
{
  "mcpServers": {
    "hive-<你的agent名>": {
      "command": "python3",
      "args": ["/home/skywalk/dswork/hive/hive.py", "mcp", "--agent", "<你的agent名>"]
    }
  }
}
```

然后按顺序调：`hive_whoami` → `hive_next` → `hive_claim` → 干活 → 写 `.hive/summary.md` → `hive_submit` → `hive_result`。

三条最容易踩的：

1. **`--dir` 必须写在 `mcp` 后面**，写在前面会被静默忽略（见 §5.1）。
2. **别把主工作区当 `--dir`**，MCP 每次调用都 `reset --hard`（见 §5.2）。
3. **`claimable: []` 不一定是故障**，可能真的没卡（见 §4.3）。

## 1. hive 是什么（agent 视角）

一句话：**一个把「谁该干什么」存在 git 分支里的任务总线**。没有数据库、没有调度中心、不用 SSH。

| 你关心的事 | 实现方式 |
|---|---|
| 任务卡在哪 | `tasks/T-<id>.md`，frontmatter + 正文 |
| 谁领了 | `claim/T-<id>` 分支存在 = 有人持有租约 |
| 抢锁怎么原子 | `git push` 同一个分支，只有一个成功 |
| 结果在哪 | `res/T-<id>` 分支的 `results/T-<id>.json` |
| 干完怎么验收 | 卡面上的 `verify` 命令，退出码写进结果 |
| 分派怎么决定 | `needs` 能力 + `repo` + `depends_on` + `paths` 冲突检测 |

对你的直接影响：**你的工作目录是一个独立 worktree**，不是协调仓本身。领卡后 `hive_claim` 会告诉你路径。

## 2. 第一步：确认身份

别急着领卡，先调 `hive_whoami`。它一屏告诉你四件事：我是什么模式、我能干什么、我手上有什么、我现在能领什么。

实测（`agent=raccoon`）：

```json
{
  "status": "success",
  "message": "我是 raccoon（manual 模式）",
  "details": {
    "agent": "raccoon", "role": "worker", "mode": "manual",
    "capabilities": ["gui", "chroot", "linux"],
    "abilities": [],
    "repos": {"light": "/home/skywalk/src/light", "hive": "/home/skywalk/dswork/hive"},
    "worktrees": "/home/skywalk/.hive/wt/raccoon",
    "max_concurrency": 1,
    "hive_dir": "/home/skywalk/.hive/clones/mcp-raccoon",
    "mine": [{"id": "T-0003", "state": "done", "owner": "raccoon", "title": "..."}],
    "claimable": []
  }
}
```

怎么读：

- `mode: manual` → 卡上的 commit/push/verify **由 `hive_submit` 替你做**，你别自己 `git commit`。
- `capabilities` + `abilities` 合起来是你会的技能；卡的 `needs` 必须被它**完全覆盖**。
- `repos` 里没登记的仓库，那些卡你永远领不到（直接判「仓库没登记在我名下」）。
- `claimable` 是**现在**能领的；`mine` 是**历史上**归你的。

> 如果 `whoami` 就报 `没有登记 agent xxx`，说明这个身份还没建。让总控跑一次：
> ```sh
> python3 hive.py agent add --name <名字> --mode manual \
>   --capabilities gui linux --repo hive=/path/to/hive --worktrees ~/.hive/wt/<名字>
> ```

## 3. 标准闭环

### 3.1 挑卡

```
hive_next()                      # 推荐一张「现在就能干」的
hive_list_tasks(state="ready")   # 或者自己翻
hive_get_task(task_id="T-0131")  # 领之前先看全文：paths / verify / 依赖
```

`hive_next` 的筛选口径（照代码抄的）：`state == ready`、没有 owner、没有结果、没有租约，
并且 `_can_do` 通过 —— 仓库在你名下、`needs` 无缺失、`depends_on` 全部 done。

### 3.2 领卡

```
hive_claim(task_id="T-0131")
```

实测返回：

```json
{
  "status": "success",
  "message": "T-0130 已领取，worktree 备好",
  "details": {
    "task_id": "T-0130",
    "worktree": "/home/skywalk/.hive/wt/testbot/T-0130",
    "task_md": ".../.hive/task.md",
    "summary_md": ".../.hive/summary.md",
    "branch": "task/T-0130",
    "paths": ["docs/"],
    "verify": ["true"],
    "hint": "在 worktree 里改代码，把工作说明写进 summary.md，然后调 hive_submit；干不下去就调 hive_abort 把卡还回去"
  }
}
```

**从这一刻起租约开始计时**，默认 180 分钟。干活慢就 `hive_extend_lease(task_id, minutes=60)`
续，别等过期被 `gc` 回收——回收后你的 worktree 白干。

### 3.3 干活

在 `worktree` 里改，**只改 `paths` 声明的范围**。越界改动会被 `hive audit` 和提交时的
越界检测抓出来（`lessons.md` 里有真实案例：顺手改了 README 但没声明，只能回退重写）。

长任务中途记一笔，方便别人接手：

```
hive_progress(task_id="T-0131", text="已改完编号，正在补测试")
```

写进 `<worktree>/.hive/progress.md`，不进 git，不影响验收。

### 3.4 写总结

`.hive/summary.md` 是人类和总控判断你干了什么的唯一依据。四段就够：

```markdown
# T-0131 操作手册章节编号整理

## 做了什么
- 把 §6.x 的四块移回 §6 内部，编号重排为 6.1–6.6

## 验证
- `python3 -m pytest tests/test_docs.py -q` → 6 passed（改前 2 failed）

## 没做 / 风险
- 没动 §8 的 gc 已知坑（本卡 paths 不含）
```

### 3.5 提交

```
hive_submit(task_id="T-0131")     # 自动 commit + push task/<id> + 跑 verify + 写结果
```

实测：

```json
{
  "status": "success",
  "message": "T-0130 提交完成：done",
  "details": {"status": "done", "branch": "task/T-0130", "duration_s": 98.0,
              "verify": [{"cmd": "true", "code": 0, "tail": []}], "error": ""}
}
```

`status: done` = verify 全绿，等总控合并。`failed` = 看 `verify[].tail` 改完再 submit 一次。

### 3.6 收尾

```
hive_result(task_id="T-0131")   # 看最终结果和每条 verify 的退出码
hive_abort(task_id="T-0131", reason="环境起不来")   # 干不下去就还回去
```

`hive_abort` 默认把卡退回 `ready`，**别人立刻能接**。这比干耗到租约过期体面得多，
也不会触发重试熔断（同一张卡自动回收 3 次后会被判 `blocked`，从此退出流水线）。

## 4. 会咬人的硬规则

### 4.1 只能操作自己的卡

代码强制，不靠你自觉：

| 操作 | 越权结果 |
|---|---|
| 提交别人的卡 | `T-xxx 不归我（owner=yyy），只能提交自己的卡` |
| 领已被别人持有的卡 | `T-xxx 已被 yyy 领取，抢不到` |
| 给别人的租约续期/终止 | 返回 `status: error` |

### 4.2 verify 必须自己先跑过

`lessons.md` 第一条：T-0114 的验收命令自己写错了（用了 `sys.exit` 却没 `import sys`），
submit 判 failed，**代码其实全对**。领卡后先手动跑一遍 `verify` 里的每条命令再动手。

### 4.3 `claimable: []` / `hive_next` 报没卡，先看是不是真没卡

分三种情况，别一上来就以为坏了：

1. **真没卡**。实测当天全总线 25 张：23 done + 1 running + 1 blocked，`ready` 为 0。
2. **能力不匹配**。唯一没完成的 T-0102 要 `windows`，而 `raccoon` 只有 `gui/chroot/linux`
   → 就算它是 ready 也领不到。
3. **已被别人领走**。看 `hive_list_tasks` 的 `claim_agent` 字段。

排查顺序：`hive_list_tasks` 看全貌 → `hive_stats` 看总体 → `hive_doctor` 看体检。

### 4.4 别自己 commit

`hive_submit` 会替你 commit + push + 跑 verify + 写结果。你自己 `git commit` 会让
`commit_and_push` 的改动清单和 summary 对不上，`hive audit` 会标风险。

### 4.5 worktree 残留会挡路

报「已经检出到」时：

```sh
git -C <代码仓> worktree remove --force <worktree路径>
git -C <代码仓> worktree prune
```

## 5. 两个必须知道的坑（本次实测踩到）

### 5.1 `--dir` 写在 `mcp` 前面会被静默忽略

`hive.py` 顶层和 `mcp` 子命令各自注册了一个 `--dir`。写成 README 里的
`hive.py --dir <仓> mcp --agent x` 时，`--dir` 落进**顶层**参数，`mcp` 子命令读的是
自己的 `mcp_dir`，取不到就回退默认 clone，**不报错**。

实测（故意给一个不存在的路径）：

```sh
# 错：写在 mcp 前面 → 指向默认 clone，你给的路径被无视
python3 hive.py --dir /tmp/fake-hive-dir mcp --agent raccoon
#   → hive mcp: agent=raccoon role=worker dir=/home/skywalk/.hive/clones/mcp-raccoon

# 对：写在 mcp 后面
python3 hive.py mcp --agent <名字> --dir /path/to/hive
```

不确定时看启动那行 stderr 的 `dir=`，它才是真正生效的路径。

### 5.2 MCP 每次调用都会 `reset --hard`，绝不能指向主工作区

`mcp_server.py` 启动时和**每次工具调用前**都做 `fetch` + `reset --hard origin/main`
（设计如此：保证你读到最新的卡）。所以：

- ✅ 用默认的独占 clone（`~/.hive/clones/mcp-<agent>`），别管它；
- ❌ **不要** `--dir` 指向你正在改代码的仓库 —— 一启动就把未提交改动抹掉。

这也是 CHANGELOG 0.9.9 里「Worker 不再裸 reset」的另一半：Worker 已改成 `sync_main()`
守卫，但 MCP 的两处 reset **有意保留**，因为它作用在独占 clone 上。

## 6. 报错速查

| 报错 | 原因 | 怎么办 |
|---|---|---|
| `hive mcp: 必须指定 --agent` | 没给身份 | 加 `--agent <名字>` 或设 `HIVE_AGENT` |
| `hive mcp: 没有登记 agent xxx` | 身份没建 / 拼错 / `--dir` 指错了仓 | 先 `agent add`；再确认 §5.1 |
| `hive mcp: 准备 clone 失败` | 源仓没配 origin 或网络不通 | 手动 `git clone <远端> <目标>` 一次 |
| `现在没有我能接的卡` | 无 ready / 能力不匹配 / 被领走 | 见 §4.3 |
| `T-xxx 已被 yyy 领取，抢不到` | 并发抢锁输了 | 换一张，或等 yyy 交 |
| `T-xxx 不归我（owner=yyy）` | 想交别人的卡 | 先 `hive_claim` 领过来 |
| `worktree 不存在` | 没领卡就 submit，或 worktree 路径对不上 | 先 `hive_claim`；见 §4.5 |
| `T-xxx 已经有结果或已关闭` | 重复领取/提交 | 要重跑找总控 `hive_retry` |
| `verify` 红了 | 代码没改对，或验收命令本身写错 | 先手跑一遍命令，区分是哪种 |
| `paths 越界` | 改了声明外的文件 | 回退越界改动，或找总控改卡 |

## 7. worker 还是 controller

| | worker（默认） | controller |
|---|---|---|
| 工具数 | **14**（实测） | **21**（实测） |
| 多出来的 | — | `hive_land` `hive_close` `hive_retry` `hive_assign` `hive_dispatch` `hive_gc` `hive_watchdog` |
| 能动的范围 | 只有自己的卡 | 会改 `main`（合入、关闭、重派、回收） |
| 起法 | `mcp --agent x` | `mcp --agent x --role controller` |

controller 视角的破坏性操作**默认 dry-run**，要 `apply=true` 才真执行。`hive_land` 撞冲突时
只报冲突文件、**不自动解冲突、不 force push**。

**一个 server 进程 = 一个身份**，同一个 agent 名别起两个 server。

## 8. 干不下去别直接 abort：先用 block

`hive_block` 和 `hive_abort` 的区别，是新手最容易用错的一处：

| | `hive_block` | `hive_abort` |
|---|---|---|
| 适用 | 等外部资源（Windows 真机没装好、依赖的卡没完成） | 这活我干不了 / 不该我干 |
| 卡的归宿 | `blocked`，`dispatch` 与 worker 都跳过 | 退回 `ready`，别人立刻能接 |
| 会不会被重派 | 不会，直到有人 `unblock` | 会，可能又派给别人 |
| 租约 | 释放 | 释放 |

判据一句话：**"不是这活的问题，是条件还没到" → block；"这活我干不了" → abort。**

`blocked` 的卡会在 `hive_doctor` 里点名，不会被静默遗忘。

> 这两个工具曾经因为 `TOOLS` 里 schema 少一个闭合 `}` 而从 `tools/list` 消失（T-0135 已修）。
> 如果你的 `tools/list` 只有 14 个而没有 `hive_block`，说明客户端连的是旧 clone，重启一次即可。

## 9. 一页速查

```
# 只读
hive_whoami()                              我是谁、能领什么
hive_next()                                推荐一张能干的卡
hive_list_tasks(state="ready", repo="hive")
hive_get_task(task_id="T-0131")            全文：paths/verify/依赖/租约
hive_result(task_id="T-0131")              结果 + 每条 verify 退出码
hive_stats()                               成功率/耗时/待合并
hive_doctor()                              体检

# 写
hive_claim(task_id="T-0131")               领：抢租约 + 建 worktree
hive_progress(task_id="T-0131", text="...")
hive_extend_lease(task_id="T-0131", minutes=60)
hive_submit(task_id="T-0131", note="...")  交：commit/push/verify/结果
hive_block(task_id="T-0131", reason="...") 挂起：等外部条件，不会被重派
hive_unblock(task_id="T-0131", reason="...")
hive_abort(task_id="T-0131", reason="...") 还回去：让别人能接

# 返回值统一信封
{ "status": "success"|"error", "message": "...", "details": { "hint": "下一步" } }
```

失败先读 `details.hint`，它通常直接告诉你该调哪个工具。

## FAQ

> 面向 agent 的高频疑问，每条给一句「详见」，深挖看对应章节或 `docs/lessons.md`。

**Q1. `claimable: []` / `hive_next` 说没卡，是不是坏了？**

不一定是故障，先分清三种情况（详见 §4.3）：

1. **真没卡** —— 全总线没有 `ready` 的卡，正常。
2. **能力不匹配** —— 卡的 `needs` 你覆盖不了（例：卡要 `windows`，你只有 `gui/linux`）。
3. **已被别人领走** —— 卡被人持有租约，看 `hive_list_tasks` 的 `claim_agent` 字段。

排查顺序：`hive_list_tasks` 看全貌 → `hive_stats` 看总体 → `hive_doctor` 看体检。

**Q2. 为什么领卡后要先把 `verify` 命令手跑一遍？**

`lessons.md` 第一条：T-0114 的验收命令**本身写错了**（用了 `sys.exit` 却没 `import sys`），
`submit` 判 `failed`，代码其实全对。领卡后先手动跑一遍 `verify` 里的每条命令，区分
「代码没改对」还是「验收命令本身写错」。详见 §4.2。

**Q3. 我能自己 `git commit` 吗？**

别。`hive_submit` 会替你 `commit + push + 跑 verify + 写结果`。自己 `git commit` 会让改动清单和
summary 对不上，`hive audit` 会标风险。详见 §4.4。

**Q4. `--dir` 到底写在哪？**

**必须写在 `mcp` 子命令后面**：

```sh
python3 hive.py mcp --agent <名字> --dir /path/to/hive
```

写在 `mcp` 前面会落进顶层参数、被静默忽略并回退默认 clone，不报错。不确定就看启动那行
stderr 的 `dir=`。详见 §5.1。

**Q5. 干不下去：`hive_block` 还是 `hive_abort`？**

- `hive_block`：**等外部资源**（真机没装好、依赖卡没完成）。卡变 `blocked`，不会被重派。
- `hive_abort`：**这活我干不了 / 不该我干**。卡退回 `ready`，别人立刻能接。

判据一句话：**"不是这活的问题，是条件还没到" → block；"这活我干不了" → abort。** 详见 §8。

**Q6. `paths` 越界会怎样？**

只改卡面 `paths` 声明的范围。越界改动会被 worker 提交前的越界检测抓出来；`lessons.md` 里有
真实案例（T-0112 顺手改 README 但没声明，只能回退重写）。默认只警告（写进结果 `out_of_paths`），
`HIVE_STRICT_PATHS=1` 或 agent 配 `strict_paths` 时判失败。详见操作手册 §5.3。

**Q7. 租约快到了 / 已经过期怎么办？**

用 `hive_extend_lease(task_id, minutes=60)` 续租，别等过期被 `gc` 回收——回收后你的 worktree 白干。
详见 §3.2。

## 术语表

新人看的一屏黑话，按出现频率排。

| 术语 | 一句话 |
|---|---|
| **task 卡** | `tasks/T-<id>.md`，一张「谁该干什么」的任务单：frontmatter（`paths`/`verify`/`needs`/`depends_on`）+ 正文 |
| **inbox** | `inbox/<agent>/T-<id>.md`，总控给某台机器的分派信号；worker 扫它发现自己该干的卡 |
| **claim 租约** | `claim/T-<id>` 分支存在 = 有人正持有这张卡；抢锁靠 `git push` 同一分支的原子性，只有一个成功 |
| **worktree** | 领卡后你的独立工作目录（`~/.hive/wt/<agent>/<tid>`），不是协调仓本身，改它不影响别人 |
| **lease（租期）** | 从领卡起计的时限（默认 180 分钟）；到期没续会被 `gc` 回收，可用 `hive_extend_lease` 续 |
| **dispatch** | 总控的自动分派：按 `needs`/`repo`/`depends_on`/`paths` 冲突把 `ready` 卡派给空闲且能力匹配的 agent |
| **land** | 把 `done` 的卡合入 `main` 并 close；在临时 worktree 里真跑一遍 merge，冲突不自动解、不 force push |
| **watchdog** | 自动回收死卡的看门狗：把 orphan / zombie / stale 的孤儿卡退回 `ready` 重派 |
| **gc** | 回收过期租约（等价清理遗留的 `claim/<tid>` 分支），让卡重新可领 |
| **res / 结果** | `res/T-<id>` 分支上的 `results/T-<id>.json`：commit 分支、各条 verify 退出码、耗时、summary |
