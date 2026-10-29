# hive —— 多机 AI Agent 协同的任务总线

> **状态：活跃开发中**，按任务卡迭代，变更记在 `CHANGELOG.md`。
> hive 是自包含的工具，**没有**并入任何其它项目。「光明（LightLang）」是一门独立的
> 中文编程语言，与本项目无关——只是早期拿它的代码仓当过登记示例。

一个 Python 脚本（`hive.py`）+ 一个 git 仓库，把「总控 AI 拆任务」和「多台机器上的 agent 干活」连起来。
MCP server 与 LLM 总控已拆成独立模块（`mcp_server.py` / `llm_controller.py`）。

- **总控**只做三件事：写任务卡、分派、验收。
- **worker** 自己拉取、原子领取、在独立 worktree 里干活、把结果推回。
- **不需要** SSH 进 Windows，**不需要**共享目录，**不需要**中央数据库。
- 领取靠 `git push` 的原子性：两个 worker 抢同一张卡，只有一个 push 成功。
- 自带 `dispatch`（能力/负载/依赖感知）、`doctor`（体检）、`stats`（成功率/耗时）。

## 1. 协调仓库

推荐做法：**把本工具仓直接推成 `skywalk/hive`**，这样 `hive.py`、`windows/`、`docs/`
都跟着仓库走：

```sh
cd ~/dswork/hive
git remote add origin http://192.168.1.5:3000/skywalk/hive.git
git push -u origin main
```

任意机器 clone 之后跑一次 `init` 补齐任务目录：

```sh
git clone http://192.168.1.5:3000/skywalk/hive.git ~/hive
python3 ~/hive/hive.py --dir ~/hive init
```

也可以从空仓库开始（`init --remote ...` 会建目录并推第一版），但那样就没有现成的
`windows/` 和 `docs/`，需要自己拷。

`init` 会把 `hive.py`（以及可选的 `hive_ports.py` / `hive_cluster.py` / `conflict.py` / `dispatch_prompt.py`）
一起放进仓库，之后每台机器 clone 下来就能直接跑，不用另外装。

目录：

```
hive/
├── hive.py              # 工具本体（随仓库走）
├── hive_ports.py        # 端口默认值的唯一来源（8787/8788/8789/8799）
├── hive_cluster.py      # 常驻集群（可选：发现/注册/选主/实时进度）
├── tasks/T-*.md         # 任务卡（总控写）
├── inbox/<agent>/       # 分派信号（总控写）
├── results/T-*.json     # 结果（worker 写，在 res/<task> 分支）
├── claims/T-*.json      # 租约（worker 写，在 claim/<task> 分支）
└── agents/<agent>.json  # 每台机器的配置
```

## 1.5 常驻集群（可选，但强烈建议）

**建议**每台跑 agent 的机器都起一个常驻集群进程：自动发现同网段的其他 hive、向主节点注册、
实时同步进度和租约；主节点退出后自动重选。不跑也能用，只是跨机信息要等 git push。
**git 仍是唯一真相**，集群只是把「等下一次 git push 才能看到进度」变成秒级。

```sh
python3 ~/hive/hive.py --dir ~/hive cluster start     # 后台常驻
python3 ~/hive/hive.py --dir ~/hive cluster status    # 谁是主节点 / 谁在线
python3 ~/hive/hive.py --dir ~/hive cluster peers
python3 ~/hive/hive.py --dir ~/hive cluster watch     # 事件流
python3 ~/hive/hive.py --dir ~/hive cluster sync      # 本机代码同步状态
```

- 默认 HTTP `8788`、UDP 发现 `8789`；跨网段用 `--seed 192.168.1.5:8788`。
- 端口规划（看板 8787 / 集群 8788 / 发现 8789 / 冒烟 8799）集中在
  [`docs/端口与服务规划.md`](docs/端口与服务规划.md)，代码默认值只在
  [`hive_ports.py`](hive_ports.py) 定义一次；`hive ports` 查看，`hive ports --check` 体检。
- 跨机建议设置同一个 `HIVE_CLUSTER_TOKEN`。
- `hive work` / `hive orchestrate` 会自动拉起本机集群（`HIVE_CLUSTER_AUTOSTART=0` 关闭）。
- 新机器在同网段启动即可自动注册；主节点停掉后 1~8 秒内自动出新主。
- verify-only / GUI 机器用 `cluster start --no-elect` 做 observer，只观察不参选。
- **P0 起进度默认不写 Git**：`HIVE_STATUS_GIT=auto` 只在里程碑且集群不可用时兜底；
  land 后各机按 `repo.updated` 自动 `ff-only` 同步主检出。
- 细节、API、选主/任期/化身号/宽限期的取舍：见 [docs/集群服务.md](docs/集群服务.md)。

## 2. 登记机器

agent 配置里写清楚：这台机器能干什么、仓库在哪、用什么命令启动 agent。

```sh
# FreeBSD 主力机：dsh headless，prompt 从 stdin 进
python3 ~/hive/hive.py --dir ~/hive agent add \
  --name fb88-dsh \
  --command "dsh --profile headless -" \
  --capabilities freebsd python compiler pytest \
  --repo hive=/home/skywalk/dswork/hive \
  --worktrees /home/skywalk/.hive/wt
```

Windows VM（没有 agent CLI 也能当验收机）：

```powershell
python C:\hive\hive.py --dir C:\hive agent add --name win10 --mode verify `
  --capabilities windows exe powershell `
  --repo hive=C:\hive --worktrees C:\hive-wt
```

三种模式：

| 模式 | 配置 | 行为 |
|---|---|---|
| `exec` | `--command "..."` | 调 agent，prompt 从 stdin 进，cwd 是 worktree |
| `verify` | `--mode verify` | 不调 agent，只跑任务卡的 `verify`，不产出代码分支 |
| `manual` | `--manual` | 备好 worktree，等人/GUI agent 干完写 `.hive/summary.md` |

> `exec` 的约定：**任务提示词从 stdin 传入**，进程的 cwd 就是任务 worktree。
> `dsh --profile headless -` 正好满足；Claude Code 用 `claude -p`；Codex 用 `codex exec -`。
> 国产 GUI agent 大多没有 CLI，用 `verify` 或 `manual`（见 §6、§7）。

## 3. 建卡与分派（总控做的事）

```sh
python3 ~/hive/hive.py --dir ~/hive task new \
  --title "修复 LLVM 后端 Windows 路径分隔符" \
  --repo hive \
  --paths src/codegen/ tests/ \
  --verify "python -m pytest -q tests/test_llvm_paths.py" \
  --depends T-0100 \
  --body-file /tmp/card.md

# 手工指定
python3 ~/hive/hive.py --dir ~/hive assign T-0101 --agent win10

# 自动分派：先看计划，再 --apply
python3 ~/hive/hive.py --dir ~/hive dispatch
python3 ~/hive/hive.py --dir ~/hive dispatch --apply --limit 3
```

任务卡就是 `tasks/T-0101.md`，可以直接改、可以 review、可以回滚。
`dispatch` 只看 `ready`、依赖已完成的卡，按 `needs`/`repo` 匹配 agent，按 `max_concurrency` 控负载。

## 4. worker

```sh
# FreeBSD：丢进 tmux，断线也不怕
tmux new -s hive-fb88 -d "python3 ~/hive/hive.py --dir ~/hive work --agent fb88-dsh --interval 30"

# Windows：计划任务 / 开机启动，登录后常驻
python C:\hive\hive.py --dir C:\hive work --agent win10 --interval 60
```

`scripts/` 下有两个省事的壳：`start-worker.sh <agent>`（自动把 `OPENAI_API_KEY`
映射成 dsh 要的 `DEEPSEEK_API_KEY`，多 worker 用 `HIVE_DIR` 各自 clone）；
`dispatch-loop.sh`（总控侧每分钟 `dispatch --apply`，自动分派循环）。

每轮：

1. `git fetch` + `reset --hard origin/main`（协调仓库只读）；
2. 扫 `inbox/win10/T-*.md`，跳过已完成 / 被别人领走的；
3. 推 `claim/T-xxxx` 分支抢锁，输了就下一张；
4. 在代码仓库建独立 worktree（`exec`/`manual` 用 `task/T-xxxx`，`verify` 用只读的 `verify/T-xxxx`）；
5. `exec` 跑 `--command`（stdin 喂任务卡）；`manual` 等人；`verify` 跳过；
6. `exec`/`manual` 提交、push `task/T-xxxx`；`verify` 不产出代码分支；
7. 跑 `--verify`，写 `results/T-xxxx.json` 到 `res/T-xxxx` 分支。

日志在 `~/.hive/agents/<agent>/logs/`，`--keep` 可以保留 worktree 排查。

## 5. 看板与运维

```sh
python3 ~/hive/hive.py --dir ~/hive status
#  ID      STATE      OWNER        RESULT   CLAIM            TITLE
#  T-0101  assigned   win10        done     win10@10-22T23 修复 LLVM 后端…

python3 ~/hive/hive.py --dir ~/hive doctor   # 任务卡/租约/结果/agent 配置体检
python3 ~/hive/hive.py --dir ~/hive stats    # 成功率、中位耗时、按 agent/仓库

python3 ~/hive/hive.py --dir ~/hive land   T-0101            # 合入 dry-run（默认，不动东西）
python3 ~/hive/hive.py --dir ~/hive land   T-0101 --apply    # 合入 main 并 close
python3 ~/hive/hive.py --dir ~/hive land   --all --apply     # 把能合的都合掉

python3 ~/hive/hive.py --dir ~/hive result T-0101      # 看摘要
python3 ~/hive/hive.py --dir ~/hive retry  T-0101      # 失败重跑（清结果+租约，续用原分支）
python3 ~/hive/hive.py --dir ~/hive retry  T-0101 --fresh  # 从 main 重来
python3 ~/hive/hive.py --dir ~/hive gc --minutes 180   # 回收跑飞的租约
python3 ~/hive/hive.py --dir ~/hive prune              # 清代码仓里已合并的 task/verify 分支（dry-run）
python3 ~/hive/hive.py --dir ~/hive close T-0101 --note "已 land 合入"

# 不跑 worker 的人工链路（GUI agent 场景，与该 agent 的 worker 二选一）
python3 ~/hive/hive.py --dir ~/hive take   T-0101 --agent workbuddy
python3 ~/hive/hive.py --dir ~/hive submit T-0101
```

`land` 是最后一步的自动化：在**临时 worktree** 里真跑一遍 merge（不是靠 `merge-tree` 猜），
干净就推 main 并 `close`，冲突就把文件清单列出来交给人/回给 owner 开新卡 —— 不自动解、不 force。
默认 dry-run，`--apply` 才动；详见 [`docs/操作手册.md`](docs/操作手册.md) §5.1。

`close` 会把 `res/` 里的结果归档到 `main:results/T-0101.json`，然后删掉 `claim/T-0101`、`res/T-0101`
两个分支——Gitea 分支列表里只留正在跑的任务，审计记录仍在 main 上。
合并进代码仓 `main` 之后跑一次 `prune`：它只删「已合并且不在飞」的 `task/*`、`verify/*`
分支（在飞的卡哪怕内容已并入也保留），默认 dry-run，`--apply` 才真删。

`retry` 会把远端结果分支删掉；worker 下一轮发现「本地有完成标记但远端没结果」会自动重跑，不需要去每台机器清缓存。

## 6. verify 模式（Windows 验收机）

适合**没有 agent CLI** 的机器：只跑验收，不改代码。任务卡里必须至少有一条 `verify`。

```sh
agent add --name win10 --mode verify --repo hive=C:\hive ...
```

要验证某个修复分支，把任务卡的 `base` 写成 `task/T-0101` 即可；worker 检出那条分支跑命令，
结果里带 `"mode": "verify"`，不会产生新的代码分支。

Windows 专属 bug 的三卡闭环：诊断卡（win10/verify）→ 修复卡（fb88/exec）→ 验收卡（win10/verify, base=task/T-xxxx）。

## 7. GUI agent（小浣熊 / WorkBuddy / IDE 里的 agent）

这类 agent 没有稳定的命令行入口，用 `--manual`：

```sh
# Windows 里的 dumate / Trae / CodeArts：直接打开工作区
python C:\hive\hive.py --dir C:\hive agent add --name win10-gui --mode manual `
  --launch "trae {worktree}" `
  --repo hive=C:\hive --worktrees C:\hive-wt
```

`launch` 支持 `{worktree}`、`{task}`、`{prompt}`；`trae` 不在 PATH 就写完整路径，或用 `explorer {worktree}`。
worker 把 worktree 和 `.hive/task.md` 准备好、把提示词放进 Windows 剪贴板、执行 launch，然后等 GUI agent 改完并写出 `.hive/summary.md`。

GUI 卡的 `timeout_minutes` 统一写 180、`lease_minutes` 写 240——实测小浣熊一张文档卡就要
65 分钟左右，默认 60 分钟会在它写完 summary 前超时；超时之后 GUI 才写完的恢复步骤
（补 commit/push + 补 summary + `retry` 不带 `--fresh`）见 chroot 文档。
压根不想在这个 agent 上跑 worker 的话，也可以走人工链路：`take` 领取、`submit` 提交（见 §5）。

chroot 里的小浣熊 / WorkBuddy 的完整接法（共享 `/home`、manual worker、WorkBuddy root 权限坑）：
[`docs/CHROOT_GUI_AGENTS.md`](docs/CHROOT_GUI_AGENTS.md)。适合：
- 需要人盯着界面的操作（浏览器验证、IDE 重构）；
- 小浣熊这种深链登录、沙箱受限的 Electron 客户端；
- 暂时不想给 agent 配 API key 的机器。

**小浣熊接 MCP 的现成配置**（已在 `~/.box-agent/config/mcp.json` 验证可用，14 个工具）：

```json
{"command": "python3",
 "args": ["/home/skywalk/dswork/hive/hive.py", "mcp", "--agent", "raccoon"]}
```

接上后它就能自己查卡/领卡/交了，不用人喂；流程见
[AGENT 快速上手](docs/AGENT快速上手.md)。

## 8. 限制（有意为之，v0.3.0）

- 一个 worker 串行跑任务；要并行就在同一台机器起多个 worker + 多个 agent 名（`max_concurrency` 只影响 dispatch 的分配）。
- `dispatch` 的并发锁粒度是 **paths 不是 repo**：同一个仓里改互不相干目录的卡可以同时派给不同 agent；
  paths 重叠（含父子目录）或没声明 paths 的卡才互相排队，上限 `--max-per-repo`（默认 1）。
- claim/res 是分支，只在进行中存在；`close` 会归档结果并删分支。
- 没有内置 PR 创建、没有自动合并到 main；合并留给人或总控用 git 做，`close` 只做验收归档。
- 看板是只读的；写操作（`take` / `submit` / `close` / `retry`）走 CLI。
- 任务卡 frontmatter 只支持标量和标量列表，够用就好。
- 自建裸仓建议关掉 `receive.autogc` + `gc.auto`，夜间手动 gc；原因见 `docs/操作手册.md` §8。
- Windows 引导脚本只在 PowerShell 7 上验证过语法和 dry-run，没在真机跑过；国产 GUI agent 没有 CLI，走 `manual`。

## 9. 自测

```sh
python3 selftest.py
```

会在临时目录里模拟「一个代码仓库 + 两个 worker 抢同一张卡」，断言只有一个领取成功、只产出一个结果；还覆盖 verify-only 模式、跨卡验收（base=task/T-xxxx）、失败上报、`retry`/`gc`。

## 10. 看板

```sh
cd ~/dswork/hive
PORT=8787 INTERVAL=20 ./scripts/serve-dashboard.sh
# 浏览器打开 http://127.0.0.1:8787
```

远程看：`ssh -L 8787:127.0.0.1:8787 skywalk@192.168.0.88`，浏览器开 `http://127.0.0.1:8787`。

端口只有一个来源：`HIVE_DASHBOARD_PORT`（默认 **8787**），`PORT` 是历史兼容的覆盖项。
服务化入口 `scripts/dashboard-service.sh start|stop|status`、`hive dash`、各平台自启安装器
用的都是这个默认值；任何 `hive` 命令也会顺手探活并拉起看板（`HIVE_DASH_AUTOSTART=0` 可关）。
全部端口（看板/集群/发现/测试/预留段）见
[`docs/端口与服务规划.md`](docs/端口与服务规划.md)，现场用 `hive ports --check` 查占用。
页面右上角会显示「快照 N 分钟前」——刷新循环停摆时，数据不会静默停在旧快照上。

五列看板（待领取 / 进行中 / 待合并 / 异常 / 已关闭），租约过期实时变红，
支持仓库/owner/搜索筛选、详情抽屉、深浅色。零依赖静态页，`serve.py` 只用标准库。
看板**只读**，写操作走 CLI（`take` / `submit` / `close` / `retry`）。
说明见 [dashboard/README.md](dashboard/README.md)，实跑验收见
[dashboard/VERIFICATION.md](dashboard/VERIFICATION.md)。

## 11. MCP：让 agent 自己来领活

`hive mcp` 起一个 MCP stdio server，把任务总线暴露成 14 个工具。接上之后 agent
不用等人喂卡，自助闭环：**查任务 → 领卡 → 干活 → 提交 → 干不下去还回去**。

```sh
python3 ~/hive/hive.py --dir ~/hive mcp --agent workbuddy
```

客户端配置：

```json
{
  "mcpServers": {
    "hive-workbuddy": {
      "command": "python3",
      "args": ["/home/skywalk/hive/hive.py",
               "mcp", "--agent", "workbuddy", "--dir", "/home/skywalk/hive"]
    }
  }
}
```

> `--dir` 必须写在 `mcp` **后面**。写在前面会被顶层的同名参数吃掉、子命令取不到而**静默回退**
> 到默认 clone（`~/.hive/clones/mcp-<agent>`），不报错。不确定就看启动那行 stderr 的 `dir=`。
> 也可以干脆不传 `--dir`，用默认独占 clone（推荐）。

| 工具 | 作用 |
|---|---|
| `hive_whoami` / `hive_list_tasks` / `hive_get_task` | 我是谁、有哪些卡、某张卡的全文 |
| `hive_next` | 推荐一张现在就能干的卡（跳过能力不匹配/依赖未就绪/已被领走） |
| `hive_claim` | 领取：指派给我 + 抢 claim 租约 + 备好 worktree 和提示词 |
| `hive_submit` | 提交：commit + push `task/<id>` + 跑 verify + 写结果 |
| `hive_abort` / `hive_extend_lease` / `hive_progress` | 放弃还卡 / 续租 / 中途记进度 |
| `hive_block` / `hive_unblock` | 条件没到先冻卡 / 解冻 |
| `hive_result` / `hive_stats` / `hive_doctor` | 查结果 / 吞吐统计 / 体检 |

零第三方依赖（标准库实现的 stdio JSON-RPC 2.0）；一个 server 进程 = 一个 agent，
**只能操作自己**：不能替别人领卡、提交、终止租约，越权调用直接返回错误。
返回沿用 LightMem 的 `{status, message, details}` 信封，失败时 `hint` 告诉你下一步。
详见 [MCP 接入](docs/MCP接入.md)。

## 12. 更多文档

- **[AGENT 快速上手](docs/AGENT快速上手.md)：第一次接 hive 的 agent 从这里开始（agent 视角，含实测输出和踩坑）**
- [操作手册](docs/操作手册.md)：每天的命令、总控流程、故障处理
- [任务卡模板](docs/任务卡模板.md)：代码卡 / 验收卡 / 诊断卡 / 手动卡
- [迁移指南](docs/迁移指南.md)：从聊天里手工分发迁到蜂巢
- [多机并行开发](docs/多机并行开发.md)：多台机器 + 多个 agent 并行的部署、host 路由、防冲突、合流
- [Windows 接入](windows/README.md)：bootstrap.ps1 + 计划任务
- [Gitea 接入](docs/Gitea接入.md)：私有仓、host-scoped 凭据 helper、T-0001 冒烟记录
- [chroot GUI agent](docs/CHROOT_GUI_AGENTS.md)：小浣熊 / WorkBuddy 的完整接法
- [MCP 接入](docs/MCP接入.md)：让任意 agent 通过 MCP 查/领/交/终止任务
- [集群架构 v3.1](docs/集群架构-v3.1.md)：目标架构（P0 已落地，P2 待做）：hived、本地副本、OSPF 式自适应、Git 里程碑
- [方案全文](../多机Agent协同开发方案.md)
- [CHANGELOG](CHANGELOG.md)
