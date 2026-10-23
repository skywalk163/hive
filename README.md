# hive —— 多机 AI Agent 协同的任务总线

一个约 1300 行的 Python 脚本 + 一个 git 仓库，把「总控 AI 拆任务」和「多台机器上的 agent 干活」连起来。

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

`init` 会把 `hive.py` 一起放进仓库，之后每台机器 clone 下来就能直接跑，不用另外装。

目录：

```
hive/
├── hive.py              # 工具本体（随仓库走）
├── tasks/T-*.md         # 任务卡（总控写）
├── inbox/<agent>/       # 分派信号（总控写）
├── results/T-*.json     # 结果（worker 写，在 res/<task> 分支）
├── claims/T-*.json      # 租约（worker 写，在 claim/<task> 分支）
└── agents/<agent>.json  # 每台机器的配置
```

## 2. 登记机器

agent 配置里写清楚：这台机器能干什么、仓库在哪、用什么命令启动 agent。

```sh
# FreeBSD 主力机：dsh headless，prompt 从 stdin 进
python3 ~/hive/hive.py --dir ~/hive agent add \
  --name fb88-dsh \
  --command "dsh --profile headless -" \
  --capabilities freebsd python compiler pytest \
  --repo light=/home/skywalk/src/light \
  --repo lightmem=/home/skywalk/src/lightmem \
  --repo lightharness=/home/skywalk/src/lightharness \
  --worktrees /home/skywalk/.hive/wt
```

Windows VM（没有 agent CLI 也能当验收机）：

```powershell
python C:\hive\hive.py --dir C:\hive agent add --name win10 --mode verify `
  --capabilities windows exe powershell `
  --repo light=C:\src\light --worktrees C:\hive-wt
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
  --repo light \
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

python3 ~/hive/hive.py --dir ~/hive result T-0101      # 看摘要
python3 ~/hive/hive.py --dir ~/hive retry  T-0101      # 失败重跑（清结果+租约，续用原分支）
python3 ~/hive/hive.py --dir ~/hive retry  T-0101 --fresh  # 从 main 重来
python3 ~/hive/hive.py --dir ~/hive gc --minutes 180   # 回收跑飞的租约
python3 ~/hive/hive.py --dir ~/hive close T-0101 --note "已合并到 integration"
```

`close` 会把 `res/` 里的结果归档到 `main:results/T-0101.json`，然后删掉 `claim/T-0101`、`res/T-0101`
两个分支——Gitea 分支列表里只留正在跑的任务，审计记录仍在 main 上。

`retry` 会把远端结果分支删掉；worker 下一轮发现「本地有完成标记但远端没结果」会自动重跑，不需要去每台机器清缓存。

## 6. verify 模式（Windows 验收机）

适合**没有 agent CLI** 的机器：只跑验收，不改代码。任务卡里必须至少有一条 `verify`。

```sh
agent add --name win10 --mode verify --repo light=C:\src\light ...
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
  --repo light=C:\src\light --worktrees C:\hive-wt
```

`launch` 支持 `{worktree}`、`{task}`、`{prompt}`；`trae` 不在 PATH 就写完整路径，或用 `explorer {worktree}`。
worker 把 worktree 和 `.hive/task.md` 准备好、把提示词放进 Windows 剪贴板、执行 launch，然后等 GUI agent 改完并写出 `.hive/summary.md`。适合：
- 需要人盯着界面的操作（浏览器验证、IDE 重构）；
- 小浣熊这种深链登录、沙箱受限的 Electron 客户端；
- 暂时不想给 agent 配 API key 的机器。

## 8. 限制（v0.1 有意为之）

- 一个 worker 串行跑任务；要并行就在同一台机器起多个 worker + 多个 agent 名（`max_concurrency` 只影响 dispatch 的分配）。
- claim/res 是分支，只在进行中存在；`close` 会归档结果并删分支。
- 没有内置 PR 创建；合并这一步留给人或总控用 git 做。
- 任务卡 frontmatter 只支持标量和标量列表，够用就好。
- 自建裸仓建议关掉 `receive.autogc` + `gc.auto`，夜间手动 gc；原因见 `docs/操作手册.md` §8。

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

五列看板（待领取 / 进行中 / 待合并 / 异常 / 已关闭），租约过期实时变红，
支持仓库/owner/搜索筛选、详情抽屉、深浅色。零依赖静态页，`serve.py` 只用标准库。
说明见 [dashboard/README.md](dashboard/README.md)，实跑验收见
[dashboard/VERIFICATION.md](dashboard/VERIFICATION.md)。

## 11. 更多文档

- [操作手册](docs/操作手册.md)：每天的命令、总控流程、故障处理
- [任务卡模板](docs/任务卡模板.md)：代码卡 / 验收卡 / 诊断卡 / 手动卡
- [迁移指南](docs/迁移指南.md)：从聊天里手工分发迁到蜂巢
- [Windows 接入](windows/README.md)：bootstrap.ps1 + 计划任务
- [方案全文](../多机Agent协同开发方案.md)
