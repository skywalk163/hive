#!/usr/bin/env bash
# 统一测试入口：stdin 静态重定向 + 硬超时。
# 为什么要有这层：hive.py run() 曾因未隔离子进程 stdin 导致 git 后代挂住管道，
# 表现是「测试全绿但不退出」。这里用 </dev/null + timeout 双保险（T-0261）。
#
# 可移植性（T-0285）：解释器与 timeout 都做成可探测/可覆盖，别写死。
#   PYTHON      解释器路径（默认依次探测 python / python3）
#   TIMEOUT_BIN 超时命令（默认依次探测 timeout / gtimeout；都没有就不加超时）
#   HIVE_TEST_TIMEOUT 秒数（默认 1500）
#
# 默认值为什么是 1500 而不是 600（T-0291）：完整套件实测 975s（FreeBSD 15.1，
# 921 passed / 16 errors），600s 的默认值会让「裸全量」这条判据**必然** exit=124
# ——不是测试挂死，是超时比测试本身还短。1500s 给全量留出约 50% 余量；
# 只跑定向集合（几十秒）的卡不受影响。
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1; then
    PY=python
  else
    PY=python3
  fi
fi

TIMEOUT_BIN="${TIMEOUT_BIN:-}"
if [ -z "$TIMEOUT_BIN" ]; then
  if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_BIN=timeout
  elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_BIN=gtimeout
  fi
fi

if [ -n "$TIMEOUT_BIN" ]; then
  exec "$TIMEOUT_BIN" "${HIVE_TEST_TIMEOUT:-1500}" "$PY" -m pytest -q "$@" </dev/null
fi
exec "$PY" -m pytest -q "$@" </dev/null
