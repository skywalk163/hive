---
name: 功能请求
about: 为 hive 提出新功能建议
title: '[Feature] '
labels: enhancement
assignees: ''
---

## 问题描述

请清晰简洁地描述这个功能请求要解决的问题。

例如：当我用 `hive work` 跑多机时，经常需要 [...]，这很不方便。

## 建议方案

请清晰简洁地描述你希望的解决方案。

```sh
# 可选：展示你期望的命令行用法示例
python3 hive/hive.py --dir hive <你希望有的子命令>
```

## 替代方案

请描述你考虑过的替代方案或变通方法（现有的 `dispatch` / `land` / `take`+`submit` 组合能不能凑出来？）。

## 适用场景

这个功能在什么场景下最有用？

- [ ] 单机自用
- [ ] 多机并行（多 agent 抢卡）
- [ ] Windows 验收机 / verify 模式
- [ ] GUI agent（manual 模式）
- [ ] 看板与运维（doctor / stats / gc）
- [ ] MCP / agent 自助领卡
- [ ] 其他：______

## 影响范围

- [ ] 任务卡与 claim 租约
- [ ] 分派（dispatch / assign）
- [ ] worker 生命周期（work / take / submit / retry）
- [ ] 合入与验收（land / close / prune）
- [ ] 常驻集群（hive_cluster.py）
- [ ] 看板 / dashboard
- [ ] MCP server（mcp_server.py）
- [ ] Windows 接入（windows/、.ps1 / .cmd）
- [ ] 文档
- [ ] 其他：______

## 附加上下文

请提供任何其他有助于理解该功能请求的上下文、截图或参考资料。
