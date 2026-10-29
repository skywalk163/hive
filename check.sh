#!/bin/sh
# 一键自检：语法 + pytest + 端到端 selftest
set -e
cd "$(dirname "$0")"
python3 -m py_compile hive.py hive_ports.py hive_cluster.py hive_store.py \
  mcp_server.py mcp_auth.py mcp_cache.py mcp_http.py mcp_bridge.py \
  dashboard/serve.py tests/conftest.py tests/test_core.py tests/test_cluster.py \
  tests/test_dashboard_cluster.py tests/soak.py
python3 -m pytest -q
python3 selftest.py
echo "ALL CHECKS PASSED"
