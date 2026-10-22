---
id: T-0101
title: 修复 LLVM 后端在 Windows 上的路径分隔符
repo: light
base: main
state: ready
owner: null
agent: null
priority: P1
paths:
  - src/codegen/
  - tests/
verify:
  - python -m pytest -q tests/test_llvm_paths.py
  - light compile examples/hello.light -o build/hello.exe
depends_on:
  - T-0100
lease_minutes: 180
timeout_minutes: 60
created: 2026-10-22T23:30:00+08:00
---

## 背景

`--backend llvm-typed` 在 Windows 上生成的中间文件用了 POSIX 分隔符，clang 报找不到路径。
Windows 上只复现、不修；**修在 FreeBSD 上做**，修完由 `T-0102` 在 Windows 上验收。

## 要做什么

1. 复现：在 `examples/` 里找最小用例（Windows 的复现日志见 `T-0100` 的 result）。
2. 修 `src/codegen/` 里拼路径的地方，统一走 `pathlib` / `os.path.join`。
3. 加一条 Windows 专属回归测试，Linux/FreeBSD 上 skip。
4. 跑验收命令，贴结果。

## 不要做什么

- 不要动 `stdlib/`，不要顺手格式化无关文件。
- 不要改 CI 配置（另开卡）。

## 完成标准

- 验收命令全绿；
- `.hive/summary.md` 里给出 Windows 实机输出；
- 分支 `task/T-0101` 已推送。
