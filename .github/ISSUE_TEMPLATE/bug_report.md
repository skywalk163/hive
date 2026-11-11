---
name: Bug 报告
about: 提交 Bug 报告帮助改进 hive
title: '[Bug] '
labels: bug
assignees: ''
---

## 描述

请清晰简洁地描述这个 Bug。

## 复现步骤

> 四条必填：**平台 / Python 版本 / 复现命令 / 完整报错**。缺任何一条，排查成本都会翻倍。

1. 环境准备（在工作目录里，例如 `~/dswork` 或 `G:\dswork`）：

   ```sh
   git clone <hive 仓地址> hive
   python3 hive/hive.py --dir hive init
   ```

2. 执行命令（Windows 把路径分隔符换成 `\`，参数不变）：

   ```sh
   python3 hive/hive.py --dir hive <子命令>
   ```

3. 看到报错（**完整粘贴**，不要只贴最后一行）：

   ```
   粘贴完整报错：stdout + stderr，带 traceback 的请连 "Traceback (most recent call last)" 一起贴
   ```

## 期望行为

请清晰简洁地描述你期望发生什么。

## 实际行为

请描述实际发生了什么。

## 环境信息

- 操作系统： [例如 FreeBSD 15.1 / Ubuntu 24.04 / macOS 14 / Windows 11]
- Python 版本： [运行 `python3 --version` 的输出，例如 Python 3.12.0]
- hive 版本： [运行 `python3 hive/hive.py --version` 的输出，例如 0.16.00（修订段两位）]
- Git 版本： [运行 `git --version` 的输出]
- 部署形态： [单机 / 多机（几台）；有没有跑 `hive cluster start`；claim 后端是 Git 还是 `--store`]
- agent 模式： [exec / verify / manual / accept；常驻 worker / MCP 自助 / 定时 accept]
- 接单窗口： [accept 模式填 `accept_window_minutes`；常驻 orchestrate/hived 改代码后是否重启]

## 附加上下文

- 相关的日志（`~/.hive/agents/<agent>/logs/`、`dashboard/logs/`）
- 涉及的任务卡 / 结果：`tasks/T-xxxx.md`、`results/T-xxxx.json` 的关键字段
- 截图（如果有）
- 该问题是否在特定条件下才出现？（只在 Windows / 只在多机 / 只在租约过期后……）
