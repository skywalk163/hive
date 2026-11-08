#!/usr/bin/env bash
# 统一测试入口：stdin 静态重定向 + 硬超时。
# 为什么要有这层：hive.py run() 曾因未隔离子进程 stdin 导致 git 后代挂住管道，
# 表现是「测试全绿但不退出」。这里用 </dev/null + timeout 双保险（T-0261）。
set -uo pipefail
cd "$(dirname "$0")/.."
exec timeout "${HIVE_TEST_TIMEOUT:-600}" python -m pytest -q "$@" </dev/null
