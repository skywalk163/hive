# Changelog

## 0.6.0 — 2026-10-23

### Added

- **`hive land T-xxxx [--apply] [--all] [--json]`**：把闭环最后一步自动化 ——
  以前 result=done 之后要人手工 merge + close（T-0101 就这样在分支上躺了两个小时）。
  - 合入条件：结果是 done、verify 全绿、卡未 close、依赖已全部 close；不满足就列原因。
  - 合并跑在临时 worktree（`~/.hive/land/<tid>`，用完即拆）里，**不碰主 checkout、不做 clone**；
    预演是**真的把 merge 跑一遍**（不是 `merge-tree` 猜），所以报冲突就是真冲突。
  - 冲突一律不自动解、不 force push；`conflicts` 给文件清单。非冲突性失败会带上 git 的原话。
  - merge 显式带 `-c user.name/-c user.email`，不依赖机器有没有配 git 身份 —— 测试环境里
    `git merge` 就是以「请告诉我你是谁」失败的，而且不留任何冲突标记，很难查。
  - 默认 dry-run；`--all` 按优先级合所有能合的，遇冲突停下。
  - 测试 4 例：合入并关闭 / 条件不满足被拦 / 真冲突且不 force / `--all` 只挑能合的。

### Ops

- T-0110 由 WorkBuddy 以 `workbuddy` agent 身份走 manual 模式（`hive take` + `hive submit`）
  完成 —— 第一次真正用 hive 自己的流程开发 hive，绕开了缺 DeepSeek key 的 exec 后端。

## 0.5.0 — 2026-10-23

### Added

- **`hive mcp`：MCP stdio server**（`mcp_server.py`，零第三方依赖）。任意支持 MCP 的
  agent 接上后能自助闭环：查阅任务 → 申请领取 → 干活 → 提交 → 干不下去还回去。
  12 个工具：`hive_whoami` / `hive_list_tasks` / `hive_get_task` / `hive_next` /
  `hive_claim` / `hive_submit` / `hive_abort` / `hive_extend_lease` / `hive_progress` /
  `hive_result` / `hive_stats` / `hive_doctor`。见 `docs/MCP接入.md`。
  - 协议层用标准库直接实现 MCP 的 stdio JSON-RPC 2.0（换行分隔），不引 `fastmcp`。
    支持 `initialize` / `tools/list` / `tools/call` / `ping` / `resources/list` / `prompts/list`。
  - 身份模型：**一个 server 进程 = 一个 agent**，所有写操作只作用在自己身上。
    不能替别人领卡、不能提交别人的卡、不能终止/续租别人的租约，由代码强制。
  - 沿用 LightMem 的返回约定：不抛异常，统一 `{status, message, details}` 信封；
    失败时 `isError=true`，`details.hint` 给下一步动作。
  - 工具执行期间把 `sys.stdout` 重定向到 stderr —— 底层 `cmd_submit` 有 print，
    不接住就会把 JSON-RPC 流搅坏（测试里专门盯了这条）。
  - 默认用独占 clone `~/.hive/clones/mcp-<agent>`：worker 初始化要 `reset --hard origin/main`，
    共用主工作区会把人未提交的改动冲掉；自动克隆时取源仓的 origin URL，
    不克隆成本地路径（否则 `push main` 被 `receive.denyCurrentBranch` 拒）。
- 测试：`tests/test_mcp.py` 15 个用例，起真实 server 子进程走 stdio 端到端。
- 夹具：`World.sync()` —— 卡片存在工作区文件里，别的 clone 推完不 `reset` 读到的还是旧卡。

### Fixed

- **`hive submit` 找 worktree 时忽略 `HIVE_STATE`**：路径推导和 `Worker.prepare` /
  `take` 不一致（后者认 `HIVE_STATE`），设了 `HIVE_STATE` 就报「worktree 不存在」。
  现在三处（`take` / `submit` / MCP）共用同一套推导：
  `worktrees` > `$HIVE_STATE/wt` > `~/.hive/agents/<agent>/wt`。

### Ops

- T-0101（分配健全化）合入 main（`32870dd`）并 close，17 用例全绿；
  `abilities` 注册表、dispatch 逐卡「派/不派」原因、`--max-per-repo` 同仓并发检测正式生效。

## 0.4.0 — 2026-10-23

### Added

- `hive prune [--repo <name>] [--apply]`：清理**代码仓**里已合并的 `task/*`、`verify/*`
  分支。安全规则：只认这两种前缀（`main`/`integration`/`release/*` 不碰）；必须同时满足
  「已合并进 `origin/main`（真合并或 squash 后内容一致）」和「不在飞（卡没有结果）」才删；
  默认 dry-run 只列计划，`--apply` 才推 `:refs/heads/<branch>`，本地分支能删就删。
  本地 `verify/*` 遗留（verify 模式不推远端产生的）也一并清。
  覆盖测试：合并+在飞+未合并+本地 verify 遗留四种各一条，断言只删该删的。

## 0.3.0 — 2026-10-23

### Added

- `hive take T-xxxx --agent <name>`：不启动 worker 的人工领取（assign + claim + 建 worktree，
  并打印工作目录 / 任务卡 / summary 路径），适合人盯着 GUI agent 干活的场景。
  与该 agent 的 worker 二选一，不要同时用。
- `hive submit T-xxxx`：人工提交（commit + push `task/T-xxxx` + 跑 verify + 写结果）；
  验证不通过会写 `failed`，改完再 submit 一次即可。配套 `docs/操作手册.md` §4.1 和测试覆盖。
- `dashboard/`：零依赖静态看板（`serve.py` 只用标准库）。五列分布、租约过期实时变红、
  仓库/owner/搜索筛选、详情抽屉、深浅色；`scripts/serve-dashboard.sh` 用
  `hive status --json` 定时刷新。jsdom 固定时钟 19 条 DOM 断言 +
  `dashboard/verify.sh`（MIME、目录穿越、端口隔离）全部通过。
- `scripts/gitea-credential.sh`：host-scoped 凭据 helper，从 `~/dswork/.env` 读 token，
  不进 git 配置、不进进程参数。
- `scripts/start-worker.sh`：启动 worker 时把 `OPENAI_API_KEY` 映射成 `DEEPSEEK_API_KEY`；
  多 worker 用 `HIVE_DIR` 各自 clone，避免抢同一个 git index。
- `scripts/dispatch-loop.sh`：总控侧自动分派循环（默认 60s 一次 `dispatch --apply`）。
- `docs/CHROOT_GUI_AGENTS.md`：chroot 里小浣熊 / WorkBuddy 的完整接法——共享 `/home`、
  manual worker、WorkBuddy root 权限坑、GUI 卡超时与「迟到 summary」的恢复步骤。
- manual 交接时打印 worktree 绝对路径（flush 到终端），避免 GUI agent 找错目录。

### Changed

- 任务卡模板与 chroot 指南：GUI 卡统一 `timeout_minutes: 180`、`lease_minutes: 240`
  （实测小浣熊一张文档卡约 65 分钟，默认 60 分钟会在它写完 summary 前超时）。

### Fixed

- `status` 在结果 summary 为空时不再崩（GUI agent 超时前只写了一半的场景）。

### Ops

- 工具仓推成 Gitea 仓 `skywalk/hive`（建仓为私有，10:45 改为公开，匿名可 clone；
  变更记录与收回命令见 `docs/Gitea接入.md` §2），登记 4 个 agent
  （fb88-verify / fb88-dsh / raccoon / workbuddy）。
- 真实任务闭环 4 张：T-0001 集成冒烟（verify 模式）、T-0002 / T-0003 known-issues
  状态同步、T-0004 VSCode 片段补齐，全部 `close`，代码合入 light main。

## 0.2.0 — 2026-10-23

### Added

- `hive dispatch`：自动分派。按 `needs` / `repo` / 能力标签匹配，按 `max_concurrency` 控负载，
  依赖未完成或没有空闲 agent 的卡会列出跳过原因；默认 dry-run，`--apply` 才写。
- `hive doctor`：体检任务卡、agent 配置、租约、结果、归档、inbox；有错误返回非 0。
- `hive stats`：成功率、中位/P90 耗时、按 agent、按仓库；`--json` 给看板用。
- 任务卡 `needs` 字段；agent `max_concurrency`。
- `verify` / `manual` 两种 worker 模式（不再只有 exec）；`manual` 支持 `--launch` 打开 GUI，
  Windows 下自动把提示词放进剪贴板。
- Windows：`windows/bootstrap.ps1`、`windows/run-worker.ps1`、接入文档。
- 测试：`tests/` pytest 套件（10 个用例）+ `tests/soak.py` 浸泡测试。
- 文档：`docs/操作手册.md`、`docs/任务卡模板.md`、`docs/迁移指南.md`、`check.sh`。

### Fixed

- **任务卡解析把列表里的 `true`/`false` 转成布尔值**，导致 `verify: ["true"]` 这类命令
  传给 subprocess 时报 `TypeError: 'bool' object is not iterable`。列表项现在一律按字符串解析。
- **领取提交共用工作区 index**：多个 worker 共用一个 clone 时，`read-tree/update-index`
  会互相踩，可能推出一个不含 `claims/T-xxxx.json` 的 claim 分支。现在用独立的
  `GIT_INDEX_FILE` 构造提交，不碰工作区。
- `dispatch` 的负载统计漏掉「已分派但未 claim」的卡，会把同一台机器塞爆。
- `stats` 只把有 claim 的卡算进行中，漏掉已分派未领取的卡；现在区分「进行中」和「待合并」。
- `doctor` 把「有结果但还没 close」的租约也算进行中；现在只统计真正在跑的。
- `dispatch` 的负载扫描只遍历在飞的卡，不再为几百张历史卡逐个 `git show`。
- `close` 现在把 `res/` 结果归档到 `main:results/T-xxxx.json`，再删 `claim/`、`res/` 分支；
  `retry` 会同时撤掉归档，worker 能自愈重跑。

### Ops

- 复现并规避了裸仓 `receive.autogc` 与并发 clone 的竞态：高频 push 后 clone 可能报
  `unable to read sha1 file`。测试仓默认 `receive.autogc=false` + `gc.auto=0`；
  Gitea 仓建议夜间手动 `git gc`，见 `docs/操作手册.md` §8。
