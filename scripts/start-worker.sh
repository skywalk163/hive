#!/bin/sh
# 启动一个 worker，并从 ~/dswork/.env 读取 OPENAI_API_KEY 映射成 DEEPSEEK_API_KEY。
#
#   ./scripts/start-worker.sh fb88-dsh
#   HIVE_DIR=~/.hive/clones/fb88-dsh ./scripts/start-worker.sh fb88-dsh --once
#   ./scripts/start-worker.sh fb88-dsh --max-concurrency 3   # worker 内并发跑卡上限
#
# 除 agent 名之外的参数原样透传给 `hive.py work`；`--max-concurrency N` 覆盖 agent
# 配置里的 max_concurrency（不传就用配置值，默认 1=串行）。
#
# 工具从本仓库取，工作目录默认也是本仓库；多 worker 时请给每个 worker 单独 clone，
# 用 HIVE_DIR 指过去，避免和总控/其它 worker 抢同一个 git index。
set -u

TOOL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="${HIVE_DIR:-$TOOL_DIR}"
AGENT="${1:-fb88-dsh}"
shift 2>/dev/null || true

ENV_FILE="${HIVE_ENV:-$TOOL_DIR/../.env}"
if [ -f "$ENV_FILE" ]; then
    KEY=$(python3 - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines():
    line = line.strip()
    if line.startswith("OPENAI_API_KEY") and "=" in line:
        print(line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
)
    if [ -n "${KEY:-}" ]; then
        export DEEPSEEK_API_KEY="$KEY"
    fi
fi

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    echo "[hive] warning: DEEPSEEK_API_KEY 为空，exec 模式的 agent 调用会失败" >&2
fi

exec python3 "$TOOL_DIR/hive.py" --dir "$WORK_DIR" work --agent "$AGENT" "$@"
