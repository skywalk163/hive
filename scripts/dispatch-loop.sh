#!/bin/sh
# 总控的自动分派循环：每分钟跑一次 dispatch --apply，把 ready 的卡分给空闲 agent。
#   ./scripts/dispatch-loop.sh
#   INTERVAL=120 ./scripts/dispatch-loop.sh
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
INTERVAL="${INTERVAL:-60}"

while :; do
    python3 "$DIR/hive.py" --dir "$DIR" dispatch --apply 2>&1 \
        | sed "s/^/[$(date '+%H:%M:%S')] /"
    sleep "$INTERVAL"
done
