# Changelog

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
