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
# 关键：下面 refresh() 会调 `hive status`，而 hive 入口有「按需自愈」——
# 不关掉的话，看板刷新自己会再拉起一个看板（端口打架、进程越滚越多）。
export HIVE_DASH_AUTOSTART=0
# 端口来源只有一个：PORT（服务脚本会传下来），其次 HIVE_DASHBOARD_PORT，最后 8787。
# 8787 同时也是 serve.py --port 与 hive.py DASH_DEFAULT_PORT 的默认值 —— 三处必须一致，
# 否则会出现「手动跑 8787、服务起 8791」这种看起来像"端口被人改了"的错觉。
PORT="${PORT:-${HIVE_DASHBOARD_PORT:-8787}}"
INTERVAL="${INTERVAL:-20}"
DASH="$DIR/dashboard"
STATUS="$DASH/status.json"
# CI_REPOS 留空 = 不拉 CI 状态（CI 环境下强烈建议留空：ci-check.sh 要访问内网
# Gitea，公网 runner 上连不通会白等一轮超时，把看板启动拖到超时）。
CI_REPOS="${CI_REPOS-skywalk/hive}"
CI_EVERY="${CI_EVERY:-6}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found" >&2
    exit 1
fi

mkdir -p "$DASH"

refresh() {
    # tmp 名带 $$：多个实例（历史上真出现过）共用一个 status.json.tmp 时会互相踩——
    # A 写完、B 的失败分支把它 rm 掉、A 再去 mv 就报「stat 失败: 没有那个文件或目录」，
    # 这种错误会以每秒上百行的速度灌进日志（实测灌到 338MB）。各写各的 tmp 就没有竞争。
    tmp="$STATUS.tmp.$$"
    if python3 "$HIVE/hive.py" --dir "$HIVE" status --json >"$tmp" 2>"$DASH/status.err"; then
        mv -f "$tmp" "$STATUS" 2>/dev/null || rm -f "$tmp"
    else
        rm -f "$tmp"
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
# 先让 serve.py 能起来，再补 CI 状态：ci-check.sh 最长要 15s（python 的 urlopen
# timeout），放在 serve.py 启动之前会把"服务就绪"拖过 dashboard-service.sh 的等待窗口。
[ -n "$CI_REPOS" ] && refresh_ci || true
TICKS=0
(
    while :; do
        sleep "$INTERVAL"
        TICKS=$((TICKS + 1))
        refresh || true
        if [ -n "$CI_REPOS" ] && [ $((TICKS % CI_EVERY)) -eq 0 ]; then
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
