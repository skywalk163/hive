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
