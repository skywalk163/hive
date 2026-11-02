# T-0223 hive 补 issue 模板（bug / feature）

## 做了什么

照 `light/.github/ISSUE_TEMPLATE/` 的两个模板（`bug_report.md`、`feature_request.md`）补到 hive 仓，
结构、frontmatter 字段（`name` / `about` / `title` / `labels` / `assignees`）按原样保留，
把项目名、「光明」代码示例、`duan run` 之类的命令换成 hive 的：

- `.github/ISSUE_TEMPLATE/bug_report.md`（新增）
- `.github/ISSUE_TEMPLATE/feature_request.md`（新增）

## bug 模板的四个必填项

卡片要求「平台 / Python 版本 / 完整报错 / 复现命令」四项必须能填，落点如下：

| 四项 | 模板位置 |
|---|---|
| 平台 | `## 环境信息` → 操作系统（FreeBSD 15.1 / Ubuntu / macOS / Windows 11） |
| Python 版本 | `## 环境信息` → Python 版本（`python3 --version`） |
| 复现命令 | `## 复现步骤` → 第 1/2 步：`clone` + `init` + `python3 hive/hive.py --dir hive <子命令>` |
| 完整报错 | `## 复现步骤` → 第 3 步的代码块，要求连 `Traceback` 一起贴、不要只贴最后一行 |

「复现步骤」标题下加了一行显式提示：四条必填、缺任何一条排查成本翻倍，避免填卡人只写「跑了一下 work」。
hive 特有的环境信息顺带进了模板：`hive --version`、`git --version`、部署形态（单机/多机、
有没有 `hive cluster start`、claim 后端是 Git 还是 `--store`）、agent 模式（exec/verify/manual、`hive work` 还是 MCP）。

## feature 模板

保留「问题描述 / 建议方案 / 替代方案 / 适用场景 / 影响范围 / 附加上下文」六段；
示例从 `.duan` 代码块换成 `python3 hive/hive.py --dir hive <子命令>`；
适用场景与影响范围按 hive 的真实切面重写（多机抢卡、Windows verify 机、GUI manual、看板、MCP；
任务卡与租约、dispatch、worker 生命周期、land/close/prune、集群、看板、MCP、windows/）。

## 验证

```
test -f .github/ISSUE_TEMPLATE/bug_report.md        -> rc=0
test -f .github/ISSUE_TEMPLATE/feature_request.md   -> rc=0
```

两条都真跑过：Windows 上没有 `sh`，经 `hive_compat` 把 `test -f` 改写成本机判断后执行，
退出码均为 0（`hive_compat.py run 'test -f ...'`）。

## 边界

只新增 paths 内这两个文件，未改其它文件；未碰任务卡与 agents 配置；无 token / 密钥。
`config.yml`（issue 选择器）没有加——不在卡片的 paths 里，需要的话另开卡。

## 交付（workbuddy，2026-11-02 04:4x）

### 这张卡为什么「异常」

不是产物有问题，也不是判据有问题，而是**从来没被真正做完，且被 watchdog 回收后卡在 blocked**：

- 卡面只有一行记录：`watchdog 回收（2026-11-02T01:18:58）：orphan_assigned，回到 ready`，
  而 `state:` 仍是 `blocked`、`owner: null`。也就是「声明回了 ready，实际没回到 ready」。
- 取证：交接时 `git tag | wc -l` = **0**（hive 一个 tag 都没有），
  三个远端 `git ls-remote --tags` 均空，`refs/heads/res/T-0226` 与 `refs/heads/task/T-0226` 也不存在。
  说明上一轮 agent 领了卡但没产出，租约到期被回收。

### 做了什么

打 annotated tag 并推到三个远端（origin / gitcode / github）。

**一处主动判断**：首次打 tag 时指向 `origin/main` HEAD = `30f7e34`，但那是 hive 自己的队列记账提交
（消息是 `take T-0226 -> workbuddy`）。发布 tag 指在内部流水提交上，外部用户点开 v0.1.0 会看到
一条库内记录，不合适。已重打到最后一个**内容提交** `d712b3d`（`fix(hive): 修三处判据机制缺陷…`，
是 `30f7e34` 的父提交），并用 `git push -f` 更新三个远端。

### 取证（全部本机实测）

| 项 | 值 |
|---|---|
| tag 对象 | `3ed7e85`（`git cat-file -t v0.1.0` = **tag**，即 annotated，非轻量） |
| peeled commit | `d712b3d`（= `v0.1.0^{commit}`） |
| origin / gitcode / github | 三处 `ls-remote 'refs/tags/v0.1.0*'` **逐字一致**（3ed7e85 + d712b3d 两行都在） |
| v0.1.0 树内 | `LICENSE`、`.github/ISSUE_TEMPLATE/{bug_report,feature_request}.md`、`hive.py`，共 529 文件 |
| 卡面判据 | `git rev-parse -q --verify refs/tags/v0.1.0` → **rc=0** |

### 说明

- 未改动任何代码与文件，`paths` 为空，属「只打 tag」的验收卡。
- 现版 hive 已让 `paths: []` 的卡自动放行「零改动」闸门（`d712b3d` 里的修复），
  因此本卡不再需要 `allow_empty_diff: true`；T-0227 那轮遇到的「加开关被 retry 吞掉」也不会重演。
