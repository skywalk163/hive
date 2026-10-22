---
id: T-0102
title: 验收 T-0101 的 Windows 腿
repo: light
base: task/T-0101
state: ready
owner: null
agent: null
priority: P1
paths: []
verify:
  - light compile examples/hello.light -o build/hello.exe
  - python -m pytest -q tests/test_llvm_paths.py
depends_on:
  - T-0101
lease_minutes: 60
timeout_minutes: 30
created: 2026-10-22T23:30:00+08:00
---

## 背景

`T-0101` 在 FreeBSD 上修完了，需要在 Windows 上确认修复真的成立。
这张卡是 **verify-only**：只跑命令、不改代码，派给 `win10`。

## 要做什么

1. 检出 `task/T-0101`；
2. 跑两条 verify；
3. 把 Windows 版本、clang 版本、EXE 路径贴进 `.hive/summary.md`。

## 完成标准

- 两条 verify 全绿；
- summary 里能看到 Windows 实机输出。
