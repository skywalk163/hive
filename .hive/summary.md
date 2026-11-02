# T-0221 交付：hive 补 MIT LICENSE

## 结论

hive 仓原本没有 LICENSE（四仓里唯一缺的）。按卡要求新增标准 MIT 文本，版权行
`Copyright (c) 2024-2026 Light Contributors`，与 light、lightharness 口径一致。

## 改了什么

| 文件 | 改动 |
| --- | --- |
| `LICENSE`（新增） | 标准 MIT 全文：`MIT License` 首行 + 版权行 + 两段许可/免责条款 |

只加文件，未改任何代码、测试、配置（`git status` 仅 `?? LICENSE`）。

## 验证

- 卡上验收：`head -1 LICENSE | grep -qi mit` → **exit 0**（Windows 无 bash，用 `C:\Program Files\Git\bin\bash.exe` 跑同一命令）。
- 首行实测为 `MIT License`；文件为纯 UTF-8 无 BOM，行尾 LF。
- 与兄弟仓比对：首行与正文段落逐段一致，仅版权行按卡模板取 `2024-2026 Light Contributors`
  （light 仓现为 `Dumate`、lightharness 为 `2026 Light Contributors`，均属历史写法，本卡以任务说明为准）。

## 风险与边界

- 未在 GitHub 页面上肉眼确认徽章（本地无远端渲染能力）；徽章由 GitHub 依默认分支根目录 LICENSE 自动识别，
  首行合法 MIT 即可触发，若未显示通常是缓存或本分支尚未合并。
- 未改 `README.md` 加徽章（不在允许路径内）；若需要显式徽章图片，建议另开卡。

## 接手说明（workbuddy，2026-11-02）

这张卡不是没做完，是**被自己的判据卡住了**：

- deepseek-win 已把 LICENSE 提交在 `task/T-0221`（head `715e8d3`），产物合格。
- 但卡上判据是 `head -1 LICENSE | grep -qi mit`，Windows worker 用 cmd.exe + `shell=True`，
  PATH 里没有 `head`/`grep`，实测 exit 255（报「'head' 不是内部或外部命令」），
  于是结果被记成 failed → blocked。
- 判据已改为 **`git grep -qi "MIT License" -- LICENSE`**：git 是 hive 的硬依赖，两端都有，
  且能同时判「文件存在」与「内容正确」。改后本机实测 rc=0。

结论：产物未改一行，仅修正了验收口径并重新提交。
