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
CI_REPOS="${CI_REPOS:-skywalk/hive}"
CI_EVERY="${CI_EVERY:-6}"

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

# 顺手刷新 CI 状态到 ci.json（需要 Gitea 凭证，失败保留旧文件）
refresh_ci() {
    if CI_OUT="$DASH/ci.json.tmp" CI_REPOS="$CI_REPOS" "$DIR/scripts/ci-check.sh" >/dev/null 2>&1; then
        mv "$DASH/ci.json.tmp" "$DASH/ci.json"
    else
        rm -f "$DASH/ci.json.tmp"
    fi
}

refresh || true
refresh_ci || true
TICKS=0
(
    while :; do
        sleep "$INTERVAL"
        TICKS=$((TICKS + 1))
        refresh || true
        if [ $((TICKS % CI_EVERY)) -eq 0 ]; then
            refresh_ci || true
        fi
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
