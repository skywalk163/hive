#!/bin/sh
# 启动蜂巢看板：后台定时把 `hive status --json` 落到 dashboard/status.json，
# 前台运行 dashboard/serve.py。刷新失败时保留上一份状态，不覆盖。
#
#   ./scripts/serve-dashboard.sh            # 默认 127.0.0.1:8787，20 秒刷新
#   PORT=8790 INTERVAL=10 ./scripts/serve-dashboard.sh
#   HIVE_DIR=/path/to/hive PORT=8787 ./scripts/serve-dashboard.sh
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
HIVE="${HIVE_DIR:-$DIR}"
PORT="${PORT:-8787}"
INTERVAL="${INTERVAL:-20}"
DASH="$DIR/dashboard"
STATUS="$DASH/status.json"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found" >&2
    exit 1
fi

mkdir -p "$DASH"

refresh() {
    if python3 "$HIVE/hive.py" --dir "$HIVE" status --json >"$STATUS.tmp" 2>"$DASH/status.err"; then
        mv "$STATUS.tmp" "$STATUS"
    else
        rm -f "$STATUS.tmp"
    fi
}

refresh || true
(
    while :; do
        sleep "$INTERVAL"
        refresh || true
    done
) &
LOOP=$!

cleanup() {
    kill "$LOOP" 2>/dev/null || true
    wait "$LOOP" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

cd "$DASH" || exit 1
python3 serve.py --status "$STATUS" --port "$PORT"
