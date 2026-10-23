# MCP 接入：让任意 agent 自己来查任务、领任务、交任务

`hive mcp` 起一个 **MCP stdio server**，把 hive 的任务总线暴露成 12 个工具。
支持 MCP 的 agent（Claude Desktop / Cursor / Claude Code / Codex CLI / 自己写的 harness）
接上之后就能自助：**查阅自己的任务 → 申请领取 → 在 worktree 里干活 → 提交验收 → 干不下去就还回去**。

不需要 SSH，不需要共享目录，不需要中央数据库 —— 协调仍然靠 git 分支的原子性。

## 1. 为什么用 MCP 而不是继续加 CLI

CLI 的问题是人要敲。agent 能调 MCP 之后，闭环里就少一个人：

| 环节 | 以前 | 有了 MCP |
|---|---|---|
| 我有什么活 | 人跑 `hive status` 念给 agent 听 | agent 自己 `hive_next` |
| 领卡抢锁 | 人跑 `hive take --agent x` | agent 自己 `hive_claim` |
| 交活 | 人跑 `hive submit` | agent 自己 `hive_submit` |
| 卡住了 | 卡到租约过期，等人 `gc` | agent 自己 `hive_abort` 把卡还回去 |

## 2. 起服务

```sh
python3 ~/dswork/hive/hive.py --dir ~/hive mcp --agent workbuddy
```

- **必须指定 `--agent`**（或设 `HIVE_AGENT`）。一个 server 进程 = 一个 agent 的身份，
  所有写操作只作用在这个 agent 自己身上。
- 协调仓库路径不给就用 `~/.hive/clones/mcp-<agent>`。**必须是独占 clone**：
  worker 初始化时会 `reset --hard origin/main`，共用主工作区会把人未提交的改动冲掉。
  第一次跑会自动从 hive 仓的 origin URL 克隆一份（不会克隆成指向本地路径的 origin，
  否则 `push main` 会被 `receive.denyCurrentBranch` 拒掉）。
- 日志走 stderr，stdout 只吐协议报文。

## 3. 客户端配置

MCP 客户端里加一段：

```json
{
  "mcpServers": {
    "hive-workbuddy": {
      "command": "python3",
      "args": ["/home/skywalk/dswork/hive/hive.py", "--dir", "/home/skywalk/hive",
               "mcp", "--agent", "workbuddy"]
    }
  }
}
```

同一台机器上多个 agent 各起一个 server（名字别重）：

```json
{
  "mcpServers": {
    "hive-fb88-dsh":    { "command": "python3", "args": [".../hive.py", "mcp", "--agent", "fb88-dsh"] },
    "hive-workbuddy":   { "command": "python3", "args": [".../hive.py", "mcp", "--agent", "workbuddy"] },
    "hive-raccoon":     { "command": "python3", "args": [".../hive.py", "mcp", "--agent", "raccoon"] }
  }
}
```

Windows 上把 `command` 换成 `python`、`args` 里的路径换成 `C:\hive\hive.py` 即可。

## 4. 工具清单

| 工具 | 作用 | 备注 |
|---|---|---|
| `hive_whoami` | 我是谁：模式/能力/仓库/并发上限 + 我名下的卡 + 我能领的卡 | 干活前先调一次 |
| `hive_list_tasks` | 列任务，可按 state / repo / 只看自己的过滤 | |
| `hive_get_task` | 一张卡的完整内容：frontmatter、正文、paths、verify、租约、结果 | |
| `hive_next` | 推荐一张**现在就能干**的卡（跳过能力不匹配/依赖未就绪/已被领走） | 只推荐，不领取 |
| `hive_claim` | 申请并领取：指派给我 + 推 claim 租约分支抢锁 + 备好 worktree 和提示词 | 返回工作目录 |
| `hive_submit` | 提交：commit + push `task/<id>` + 跑 verify + 写结果到 `res/<id>` | 先用 summary.md 或 `note` 说明干了什么 |
| `hive_abort` | 终止/放弃：释放我的租约，默认把卡退回 ready 让别人能领 | 已出结果的卡不许终止 |
| `hive_extend_lease` | 给租约续期（在原到期时间上累加） | GUI agent 干活慢时用 |
| `hive_progress` | 中途记一笔进度，写进 `<worktree>/.hive/progress.md` | 不进 git、不影响验收 |
| `hive_result` | 查结果：状态、耗时、每条 verify 的退出码与输出尾部、agent 总结 | |
| `hive_stats` | 吞吐与成功率、中位/P90、待合并数 | |
| `hive_doctor` | 体检：卡片格式、租约、结果、归档、agent 配置 | |

## 5. 典型流程

```
hive_whoami          → 确认身份，看看能领什么
hive_next            → 挑一张（或 hive_list_tasks 自己翻）
hive_claim(task_id)  → 拿到 worktree 路径，比如 ~/.hive/wt/workbuddy/T-0110
   …在 worktree 里改代码…
   …把工作说明写进 .hive/summary.md（长任务中途可以 hive_progress）…
hive_submit(task_id) → 自动 commit/push/跑 verify/写结果
hive_result(task_id) → 看验收过了没；挂了就改完再 submit 一次
```

干不下去就 `hive_abort(task_id, reason="环境起不来")`，别干耗到租约过期被 `gc` 回收 ——
自己还回去，卡立刻就能被别人接手。

## 6. 返回值约定

沿用 LightMem MCP 的路子：**不抛异常，统一信封**。

```json
{ "status": "success", "message": "…", "details": { … } }
{ "status": "error",   "message": "…", "details": { "hint": "下一步该怎么办" } }
```

失败时 MCP 响应的 `isError` 为 `true`，`message` 说清原因，`details.hint` 给下一步动作
（比如「不归我，先 hive_claim 把它领过来」）。

## 7. 身份与权限

一个 server 进程只认一个 agent，且**只能操作自己**：

- 不能替别人领卡（claim 抢锁谁先到谁得，抢不到会明确告诉你是谁抢走了）；
- 不能提交别人的卡（owner 不是我就拒）；
- 不能终止别人的租约，也不能替别人续租。

这些由代码强制，不靠 agent 自觉 —— 越权调用返回 `status: error` 而不是执行。

## 8. 实现说明

- 零第三方依赖：`mcp_server.py` 用标准库直接实现 MCP 的 stdio JSON-RPC 2.0
  （换行分隔），不引 `fastmcp` / `mcp` SDK。
- 每次工具调用都 `fetch` + `reset --hard origin/main` 再读，保证看到的是最新的卡。
- **stdout 被协议独占**：工具执行期间把 `sys.stdout` 重定向到 stderr，
  底层 hive 代码里的 `print`（比如 `cmd_submit` 的提交回执）不会搅坏协议流。
- 协议层支持 `initialize` / `tools/list` / `tools/call` / `ping` / `resources/list` /
  `prompts/list`；坏 JSON 回 `-32700`，未知工具回 `-32602`。

## 9. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `hive mcp: 必须指定 --agent` | 没给身份 | 加 `--agent <name>` 或设 `HIVE_AGENT` |
| `没有登记 agent xxx` | agent 还没 `hive agent add` | 先登记，或检查名字拼错 |
| `worktree 不存在` | 没领卡就想交，或 `HIVE_STATE` 与 worktree 路径对不上 | 先 `hive_claim`；路径推导见下 |
| server 一启动就退出 | `--dir` 不是 git 仓库 | 指向 clone，或删掉让它自动克隆一次 |
| 客户端报 JSON 解析错 | 有东西写进了 stdout | 检查分支代码里的 `print`；工具执行期已重定向，启动日志走 stderr |
| 领卡后别人看不到 | 卡片是工作区文件 | 每次工具调用都会 `fetch + reset`，对方重新调一次即可 |

**worktree 路径推导**（三处必须一致，代码里共用同一套逻辑）：

```
HIVE_STATE 有值  →  $HIVE_STATE/wt/<任务号>
否则             →  ~/.hive/agents/<agent>/wt/<任务号>
agent 配了 worktrees →  <worktrees>/<任务号>   （优先级最高）
```

`hive take` / `hive submit` / MCP 的 `hive_claim` / `hive_submit` 都按这个来；
以前 `hive submit` 漏了 `HIVE_STATE`，设了就对不上，已修。

## 10. 自测

```sh
python3 -m pytest tests/test_mcp.py -q
```

15 个用例，起真实的 server 子进程走 stdio，覆盖协议握手、工具清单结构、
查/挑/领/交/终止/续租/记进度、跨 agent 越权拦截、坏 JSON 与未知工具的协议错误码，
以及「stdout 只跑协议」这条硬约束。