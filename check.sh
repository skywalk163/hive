#!/bin/sh
# 一键自检：语法 + pytest + 端到端 selftest
set -e
cd "$(dirname "$0")"
python3 -m py_compile hive.py tests/conftest.py tests/test_core.py tests/soak.py
python3 -m pytest -q
python3 selftest.py
echo "ALL CHECKS PASSED"
