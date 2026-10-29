# MCP 接入：让任意 agent 自己来查任务、领任务、交任务

`hive mcp` 起一个 **MCP stdio server**，把 hive 的任务总线暴露成 **15 个工具**
（`--role controller` 是 22 个）。
支持 MCP 的 agent（Claude Desktop / Cursor / Claude Code / Codex CLI / 小浣熊 / 自己写的 harness）
接上之后就能自助：**查阅自己的任务 → 申请领取 → 在 worktree 里干活 → 提交验收 → 干不下去就还回去**。

第一次接、想尽快跑通，直接看 **[AGENT 快速上手](AGENT快速上手.md)**（agent 视角，含实测输出与踩坑）。

不需要 SSH，不需要共享目录，不需要中央数据库 —— 协调仍然靠 git 分支的原子性。

> **P1 推荐入口：hived HTTP**。`hive daemon start` 会在 `127.0.0.1:8790` 起一个本机
> MCP 入口，agent 只配 URL + token，不用给每个 agent 起 stdio 子进程：
>
> ```sh
> $H daemon start
> $H agent enroll --name workbuddy
> # 把输出的 mcpServers 配置写进客户端
> ```
>
> 读工具走 hived 的只读缓存；写工具仍走 Git/claim。只有确实只支持 stdio 的客户端才用
> `hive mcp --connect --token <token>` 桥，或下文的 `hive mcp --agent` 独立 server。

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
python3 hive/hive.py mcp --agent workbuddy
# 要指定协调仓就写在 mcp 后面：... hive.py mcp --agent workbuddy --dir hive
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

Windows 上把 `command` 换成 `python`、`args` 里的路径换成 `G:\dswork\hive\hive.py` 即可。

### 3.1 `--dir` 必须写在 `mcp` 后面（否则静默失效）

`hive.py` 顶层和 `mcp` 子命令各有一个 `--dir`。写成 `hive.py --dir <仓> mcp --agent x`
（旧 README 的写法）时，`--dir` 落进**顶层**参数，子命令读自己的 `mcp_dir` 取不到，
于是**静默回退**到默认 clone，不报错：

```sh
python3 hive.py --dir /tmp/fake-hive-dir mcp --agent raccoon
#   → hive mcp: agent=raccoon role=worker dir=/home/skywalk/.hive/clones/mcp-raccoon
python3 hive.py mcp --agent raccoon --dir /path/to/hive    # ← 正确
```

**推荐干脆不传 `--dir`**，用默认的独占 clone。传之前先想清楚：MCP 启动时和每次工具调用前
都会 `reset --hard origin/main`，指向你正在改的仓会抹掉未提交改动。

## 4. 工具清单

| 工具 | 作用 | 备注 |
|---|---|---|
| `hive_whoami` | 我是谁：模式/能力/仓库/并发上限 + 我名下的卡 + 我能领的卡 | 干活前先调一次 |
| `hive_list_tasks` | 列任务，可按 state / repo / 只看自己的过滤 | |
| `hive_get_task` | 一张卡的完整内容：frontmatter、正文、paths、verify、租约、结果 | |
| `hive_next` | 推荐一张**现在就能干**的卡（跳过能力不匹配/依赖未就绪/已被领走） | 只推荐，不领取 |
| `hive_list_pending` | 列出我能领的卡：按匹配分数排序并附理由（能力/仓库/依赖/在飞冲突） | 不给 `agent` 时返回全部 ready 且无人持有的卡（按任务号）；给了就打分排序。只读，不 claim |
| `hive_claim` | 申请并领取：指派给我 + 推 claim 租约分支抢锁 + 备好 worktree 和提示词 | 返回工作目录 |
| `hive_submit` | 提交：commit + push `task/<id>` + 跑 verify + 写结果到 `res/<id>` | 先用 summary.md 或 `note` 说明干了什么 |
| `hive_abort` | 终止/放弃：释放我的租约，默认把卡退回 ready 让别人能领 | 已出结果的卡不许终止 |
| `hive_block` | 条件没到先冻卡：保留租约与进度，到期不被回收，解冻后继续 | 卡在等外部条件（真机 / 密钥 / 别人）时用 |
| `hive_unblock` | 解冻：条件到了继续干 | |
| `hive_extend_lease` | 给租约续期（在原到期时间上累加） | GUI agent 干活慢时用 |
| `hive_progress` | 中途记一笔进度，写进 `<worktree>/.hive/progress.md` | 不进 git、不影响验收 |
| `hive_result` | 查结果：状态、耗时、每条 verify 的退出码与输出尾部、agent 总结 | |
| `hive_stats` | 吞吐与成功率、中位/P90、待合并数 | |
| `hive_doctor` | 体检：卡片格式、租约、结果、归档、agent 配置 | |

## 4.5 总控视角（controller）

worker 视角只能操作自己的卡。要让 agent 当**总控**（合入、关闭、重跑、分派），
起 server 时加 `--role controller`：

```sh
python3 hive/hive.py mcp --agent workbuddy --role controller
```

工具数从 14 变 **21**（实测），多出 7 个全都是会改动 main 的操作：

| 工具 | 作用 | 默认行为 |
|---|---|---|
| `hive_land` | 合入已完成的卡 | **dry-run**，`apply=true` 才真推 |
| `hive_close` | 关闭一张卡 | 直接执行 |
| `hive_retry` | 重跑（清结果和租约） | 直接执行 |
| `hive_assign` | 指派给某个 agent | 直接执行 |
| `hive_dispatch` | 自动分派 | **dry-run**，`apply=true` 才写 |
| `hive_gc` | 回收过期租约 | **dry-run**，`apply=true` 才删 |
| `hive_watchdog` | 自动回收死掉/孤儿/僵尸的 in-flight 卡 | **dry-run**，`apply=true` 才退回 ready |

权限是**真的隔离**，不是靠提示词叮嘱：worker 视角的 `tools/list` 里看不到这 7 个，
即使猜到名字去调也会被拒。

三条硬约定：

1. 破坏性操作一律默认 dry-run，先看计划再 `apply=true`；
2. `hive_land` 遇冲突返回 error 并把冲突文件放在 `details.conflicts` ——
   **绝不自动解冲突、绝不 force push**。冲突时的 message 与实际 `apply` 一致：
   没传 apply 说「默认只预演」，传了 `apply=true` 说「已申请真合入、因冲突未改动」；
   另带 `details.applied` 字段，调用方不依赖自然语言也能判断自己处在什么模式；
3. `hive_retry` 会清掉结果和归档，对已 close 的卡尤其要谨慎。

`hive_whoami` 在 controller 下会多返回 `todo`：待合入、conflict、blocked、
needs_review、ready 各多少张 —— 总控 agent 开工前先看这个。

### 4.5.1 两种总控形态

- **确定性总控（推荐先上）**：直接跑 `hive orchestrate`（`hive.py` 里的一条命令），
  它内部循环调用 `watchdog --apply` → `dispatch --apply` → `land --all --apply` → `doctor`，
  把 exec 类任务从 `ready` 全自动推到 `closed`。常驻进程，加 `--once` / `--iterations N` /
  `--stop-file` 控制生命周期。**它不暴露成 MCP 工具**（会阻塞调用方）。
- **带判断的总控（LLM agent）**：起 controller 角色的 MCP server，让 LLM agent 自己循环调
  `hive_dispatch` / `hive_land` / `hive_watchdog` / `hive_retry` / `hive_close`，
  在 `needs_review` / `conflict` / 依赖外部资源等卡点人工/LLM 介入。适合需要「挑 agent、看 diff、
  判重试」这类看门狗做不了的判断。
  - 现成封装：`hive llm --llm openai --apply`（见《操作手册》§6.3）。它把上面这套 controller 工具面
    直接喂给 LLM 逐轮决策——`InProcessBackend` 复用 `HiveMcp` 的 handler，`McpStdioBackend` 则真·spawn
    `mcp_server.py` 走 JSON-RPC，两者是同一工具面。没 key 时 `--llm heuristic` 回退成「不做危险决策的 orchestrate」。

两条路不冲突：确定性总控管「机械循环」，LLM 总控管「卡点决策」。

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

## 11. Windows 客户端接入（小浣熊，实测）

本节记录在 Windows 本机把 `hive mcp` 接进 GUI 客户端（商汤小浣熊）的完整过程和真实坑，
2026-10 实测，客户端版 Box-Agent 1.0.0、Python 3.12.6、hive 0.9.12。

本机用独立身份 **`raccoon-win`**，与 Linux chroot 里的 `raccoon` 是两个 agent，不要混用：
一个 server 进程只认一个身份，同一台机器上两个客户端各起各的 server，名字别重。

### 11.1 配置写法

```json
{
  "mcpServers": {
    "hive-raccoon-win": {
      "command": "C:\\Users\\<你>\\.box-agent\\box-agent-runtime\\runtime\\python\\python.exe",
      "args": [
        "G:\\dswork\\duan-light-merge\\hive\\mcp_server.py",
        "--agent",
        "raccoon-win"
      ],
      "env": {
        "PYTHONPATH": "G:\\dswork\\duan-light-merge\\hive",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8"
      },
      "alwaysLoad": true,
      "connect_timeout": 60,
      "execute_timeout": 600
    }
  }
}
```

三个要点：

1. **`command` 必须写绝对路径的解释器**，不能写裸 `python`。客户端拉起 stdio 子进程时
   不一定带着你 shell 里的 `PATH`，裸 `python` 会解析失败，而且**失败是静默的**——
   日志里只有一个 `mcp/status count=N` 的数字对不上，不会提示哪个 server 起不来。
2. **直接跑 `mcp_server.py`，不要走 `hive.py mcp`**。`mcp_server.py` 只依赖标准库和同目录的
   `hive.py`，配好 `PYTHONPATH` 就能起；多一层 `hive.py` 转发只会多一个出错点。
   （`hive.py mcp` 在 Linux 上是能用的，见 §2，两者工具面完全一样。）
3. **路径用正斜杠或成对转义的反斜杠**。JSON 里写 `"G:\dswork\..."` 会被 `\d` 当转义吃掉，
   正确写法是 `"G:\\dswork\\..."` 或 `"G:/dswork/..."`。这个错不会报错，只会让路径变形。

### 11.2 本机实测值

| 项 | 实测值 |
|---|---|
| `serverInfo` | `{"name": "hive", "version": "0.9.12"}` |
| `protocolVersion` | `2024-11-05` |
| 工具数（worker） | 14 |
| 启动行（stderr） | `hive mcp: agent=raccoon-win role=worker dir=C:\Users\skywalk\.hive\clones\mcp-raccoon-win` |
| `hive_whoami` 的 `hive_dir` | `C:\Users\skywalk\.hive\clones\mcp-raccoon-win` |
| 注册后客户端日志 | `mcp/ready count=16`（系统 1 个 + hive 15 个） |

**自检：`hive_whoami` 返回的 `hive_dir` 必须是 `~/.hive/clones/mcp-<agent>`。**
如果它指向你正在改代码的主工作区，立刻停手——MCP 每次工具调用前都会
`fetch` + `reset --hard origin/main`（见 §8），会把未提交的改动冲掉。

### 11.3 工具何时出现

配置写进 `mcp.json` 后**不保证当场生效**：实测本会话内热加载没把 15 个工具注册进来，
重启客户端后才有（日志出现 `mcp/ready count=<原数+14>`）。

判断成功的标准，按从便宜到贵的顺序：

1. `tool_search` 能搜到 `hive_whoami`；
2. 直接调 `hive_whoami`，看 `agent`、`mode`、`repos`、`claimable` 对不对；
3. 看客户端日志的 `mcp/ready count` 有没有加上 14。

只看到“配置项是 enabled”**不能**当成已连上——那是文件状态，不是连接状态。

### 11.4 本机第一个闭环

接上之后走一遍最小闭环，证明写入链路（不只是读）是通的：

```
hive_whoami  → 我名下 / 能领什么
hive_claim   → 抢 claim 租约 + 备 worktree（返回 C:\Users\<你>\.hive\wt\<agent>\<任务号>）
  …在 worktree 里改，写 .hive/summary.md…
hive_submit  → commit + push task/<id> + 跑 verify + 写结果
```

`manual` 身份下**不要自己 `git commit`**，提交交给 `hive_submit`，否则改动清单和 summary 对不上。

Windows 上手工验证 server 能不能起（不经客户端，最快定位问题）：

```powershell
'"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"p","version":"0"}}}' |
  & "C:\Users\<你>\.box-agent\box-agent-runtime\runtime\python\python.exe" `
    "G:\dswork\duan-light-merge\hive\mcp_server.py" --agent raccoon-win
```

stdout 应有一行 `initialize` 响应，stderr 有一行带 `dir=` 的启动日志。手动能起、客户端起不来，
问题就在客户端的 `command` / `env`，不在 server。
