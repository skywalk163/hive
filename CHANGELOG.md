# Changelog

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
