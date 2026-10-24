# Changelog

## 0.9.4 — 2026-10-24

### Added

- **MCP 总控视角**：`hive mcp --agent <name> --role controller`（或 `HIVE_ROLE=controller`）。
  以前 MCP 只有 worker 视角，总控还得敲 CLI；现在合入/关闭/重跑/分派/回收都能走 MCP。
  - controller 额外 6 个工具：`hive_land` / `hive_close` / `hive_retry` /
    `hive_assign` / `hive_dispatch` / `hive_gc`。
  - **权限隔离有明确证据**：worker 视角的 `tools/list` 里根本看不到这 6 个
    （测试直接断言集合交集为空），即使 worker 猜到工具名去 `tools/call` 也会被拒。
  - **破坏性操作一律默认 dry-run**：`hive_land` / `hive_dispatch` / `hive_gc`
    不传 `apply=true` 就只出计划；`hive_land` 遇冲突返回 error 并把冲突文件提到
    `details.conflicts`，绝不自动解冲突、绝不 force push。
  - `hive_whoami` 在 controller 下多返回 `role` 和 `todo`
    （待合入 / conflict / blocked / needs_review / ready 五类计数），
    让总控 agent 知道今天该干什么。
  - `initialize` 的 instructions 区分角色，controller 版明确写「你的操作会改动 main 分支」。
- 测试 6 例（含权限隔离与冲突不 force），全量 **86 passed**。

## 0.9.3 — 2026-10-24

### Added

- **verify 强度分级**：`_verify_strength(cmd)` / `_verify_verdict(verify)`。
  `strong` = 真的会执行被测对象（pytest / npm test / go test / cargo test /
  make test / unittest…）；`weak` = 只查痕迹（grep / test -f / which / echo /
  cat / wc…）。判断刻意保守，拿不准就算 weak；**混合 shell 管线按最弱的一环算** ——
  T-0101 那条 `grep && grep && pytest` 因此被判成 weak，正是想要的信号。
  - `hive result`：每条 verify 标强弱，整卡给结论 `strong-pass` / `weak-pass` /
    `fail`；弱验收时显式提醒「成果没有被真正执行验证」。`--json` 带
    `verify_strength` 与 `verify_entries`。
  - `hive doctor`：只有弱验收的卡点名 warning。
  - `draft lint`：**草稿没有强验收就报 error 挡转正**。
  - MCP `hive_result` 返回带上 `verify_strength`。
- 测试 5 组（含 15 个参数化用例），全量 **80 passed**。

## 0.9.2 — 2026-10-24

### Fixed

- **`changed_files` 两个 git 输出陷阱**（T-0121 提交时自己被咬到）：
  - `core.quotePath`：git 默认把非 ASCII 路径转义成八进制
    （`docs/操作手册.md` → `"docs/\346\223\245..."`），中文名文件会被误判成越界；
  - 未跟踪**目录**默认被折叠成目录名（只显示 `docs/`），里面的文件根本不出现在
    改动清单里，paths 检查会漏。加 `-uall` 逐文件列出。
  两条都加上对应测试。

## 0.9.1 — 2026-10-24

### Added

- **`hive digest [--days N] [--out FILE] [--json]`**：复盘摘要，把经验从结果里捞出来。
  - **失败归因**：从 `result.error` 聚类 —— 超时 / verify 未通过（附上挂掉的命令）/
    paths 越界（严格模式拦下）/ 分支推送失败 / 其它。每类列出卡号，
    一眼看出是「卡写得太难」还是「环境不稳」。
  - 耗时最长的 5 张卡（往往是「卡拆得不够小」的信号）；
    反复重跑的卡（第一版验收就没过）。
  - `--out docs/lessons.md` 写 markdown，`--json` 给机器读（`draft lint` 和看板要消费它）。
  - **只读**：不写任务卡、不推分支，`--out` 只写普通文件。
- 任务卡新增 `retries` 字段：`retry` 会覆盖上一次结果，所以重跑次数只能记在卡上，
  否则 digest 永远看不出「这张卡反复在重跑」。
- `docs/lessons.md` 第一版：五类教训（写卡 / 环境运维 / 并发提交 / 改 hive 自己 / 用法），
  全部来自真实踩过的坑。

### Fixed

- **`result.error` 在「agent 正常退出但 verify 挂掉」时会写「agent 退出码 0」** ——
  既误导人，也让 digest 的失败归因全跑偏。现在优先报 verify 未通过并附上命令。
- 测试 3 例，全量 **59 passed**。

## 0.9.0 — 2026-10-24

### Added

- **`hive draft`：任务卡草稿，让 agent 也能出题**。以前只有人会写卡 ——
  写一张卡要读代码、想 paths 和 verify、组织语言，几分钟到十几分钟；agent 一天能干
  20 张卡的活，但只能等人把卡写出来。吞吐卡在出题这一端。
  - `hive draft new [名字] [--repo hive]`：生成带「背景 / 目标 / 红线 / 验收」四段骨架的
    模板（文件名给不给 `.md` 都认）；
  - `hive draft list` / `hive draft lint [文件]`：草稿校验，不传文件就 lint 全部；
  - `hive draft adopt <文件>`：**过 lint 才转正**，自动分配任务号、从 `drafts/` 移进
    `tasks/`、草稿撤掉，并打印下一步。
  - `draft_lint()` 抽成模块级函数（error / warning 分级），`doctor` 和 `adopt` 共用。
    error 会挡住：没填 repo、repo 没被任何 agent 登记、**一条 verify 都没有**、
    `needs` 里有没有任何 agent 能满足的能力、依赖不存在的卡。
    最后两条是 T-0102 那种僵尸卡的成因，现在从源头拦住。
  - `tasks/` 仍只能由 `adopt` 或 `task new` 写入 —— 草稿是给人 review 的界面：
    agent 一次出十张是廉价的，人只做「同意/打回」这个 O(1) 判断。
- 测试 3 例，全量 **56 passed**。

## 0.8.0 — 2026-10-23

### Added

- **状态机补齐**：新增 `needs_review` / `conflict` / `blocked` 三个状态，
  异常卡终于有地方待，不用再靠反复 `retry` 清场。
  - `hive block T-xxxx --reason "..."` / `hive unblock T-xxxx`：等外部资源时挂起。
    dispatch 和 worker 都跳过它，`status` 里单独可见，`doctor` 计数。
  - `land` 撞上合并冲突时自动把卡置为 `conflict`（以前只在返回里说，没有落点）；
    `doctor` 把 `conflict` 当**错误**报（退出码非 0）—— 有卡冲突不解决，后面全堵着。
  - `needs_review`：状态机支持，给后面「agent 出卡草稿 → 人确认」的流程预留。
  - MCP 新增 `hive_block` / `hive_unblock`；`hive_next` 与 `whoami.claimable`
    都不再推荐这三个状态的卡。
- 状态常量集中到 `STATES` / `IN_FLIGHT_STATES` / `HANDOFF_STATES` / `ERROR_STATES`，
  「在飞」的判断不再散落在各处字符串比较里（原来有 4 处硬编码 `("assigned","running")`）。

### Ops

- T-0102（Windows 接入）置为 `blocked`：它 `needs: windows`，但 4 个 agent 没有一个
  声明这个能力，会永远躺在 ready 列表里骚扰 dispatch —— 这正是僵尸卡的成因。
- 测试 3 例，全量 **53 passed**。

## 0.7.1 — 2026-10-23

### Fixed

- **`commit_and_push` 收口**：新增 `paths` 参数，7 个调用点都改成只提交本命令该动的文件
  （`task new` 只交卡片、`assign`/`take` 交卡片+inbox、`close` 交卡片+归档结果、
  `retry` 交卡片+inbox+归档、`agent add` 只交该 agent 的配置）。
  以前无条件 `git add -A`，总控工作区里任何未提交改动都会被卷进这次提交 ——
  协调仓库的 main 是审计记录，提交信息和内容对不上之后就没法靠 `git log` 回溯。
  **真实事故两起**：mcp_server.py（705 行）被卷进 `task: add T-0110`；
  sync_main 修复被卷进 `close T-0110`。
  没给 paths 时保留全量行为（向后兼容），但会打一条告警日志。
- **`sync_main` 不再把未跟踪文件当「脏」**：`reset --hard` 不会动未跟踪文件，
  之前用 `git status --porcelain` 判断会把草稿、编辑器 lock 文件也算进去，
  导致 land 推完 main 之后紧接的 close 直接卡死。改成 `--untracked-files=no`。
- 测试 5 例，全量 49 passed。

## 0.7.0 — 2026-10-23

### Added

- **`hive audit [T-xxxx | --all] [--json]`**：合并前的一页纸。比对「agent 说做了什么」
  和「diff 里真有什么」—— 声明的 paths、实际改动文件、越界清单、每条 verify 的退出码、
  summary 全文，外加风险标记。风险来源：没有 verify、没声明 paths、有越界改动、
  **summary 里出现「没能跑 / 无法验证 / 未测试」这类口供**（T-0101 那张卡就是这么过的）。
- 结果 json 新增 `declared_paths` / `changed_files` / `out_of_paths` 三个字段。

### Changed

- **paths 越界检测**：worker 提交前算一次改动清单，跟任务卡声明的 `paths` 比对。
  自举时代码仓和协调仓库是同一个，agent 能顺手改掉自己的任务卡 —— 现在能看见了。
  - **默认只警告不阻断**（怕一上来把正常流程卡死）：越界写进结果和日志，任务照常完成；
  - `HIVE_STRICT_PATHS=1` 或 agent 配置 `"strict_paths": true` 才判失败：
    不提交、不跑 verify，error 里列出越界文件。
  - `paths` 为空表示不约束（不检查，但 audit 会提醒一句）。
- 测试 4 例，全量 44 passed。

## 0.6.1 — 2026-10-23

### Changed

- **`hive dispatch` 的并发锁从「按 repo 计数」细化成「按 paths 重叠计数」**：
  自举阶段 hive 是唯一在开发的仓，按 repo 计数等于把所有卡强制串行 ——
  方案文档阶段 2 原本写的就是「同时跑 3 张路径互斥的卡」，粒度一直对不上。
  - 新增 `Hive._paths_conflict(a, b)`：任一方没声明 paths 就算冲突（未声明=可能改任何地方）；
    相同或有父子目录关系（`docs/` vs `docs/操作手册.md`）算冲突；否则可以并行。
    前缀比较按目录边界走，`tests/` 与 `testing/` 不算冲突。
  - 跳过原因从笼统的「同仓并发已满」改成**点名跟谁抢**：
    「paths 冲突：与在飞的 T-0001 抢同一片文件（hive 同组上限 1）」。
  - `--max-per-repo` 的**名字和默认值都没变**（向后兼容），语义变成
    「同一 repo 里 paths 互相冲突的卡同时在飞的上限」。
  - 测试 4 例：路径互斥可并行 / 父子路径被拦且点名 / 空 paths 保守处理 / 判定规则单测；
    既有的 `test_dispatch_concurrency` 按新语义改写。全量 40 passed。

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
