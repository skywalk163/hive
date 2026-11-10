#!/bin/sh
# hive 开发集群（隔离环境）—— 与生产集群完全分开，见 scripts/dev-cluster.sh
#
# 为什么需要它：fb98 这台机器同时是生产研发机，生产 hive 集群跑在本机
# （~/.hive/cluster + 8788/8790 端口）。改 hive 代码时会重启/调试集群，
# 一旦和生产混在一起，整个团队的任务分发、看板、训练调度就全停了。
#
# 本脚本起的是一套**完全隔离**的集群：不同的 state_dir、端口、cluster_id、
# 数据库、MCP 目录，并且关闭 UDP 广播发现与仓库同步，保证：
#   1. 开发集群永远不会连上生产集群（cluster_id 不同 + 不广播）；
#   2. 开发集群的读写不会碰到生产 store / status.json / git 工作区；
#   3. 生产集群完全不知道开发集群存在。
#
# 用法：
#   scripts/dev-cluster.sh start     # 启动（默认后台）
#   scripts/dev-cluster.sh stop
#   scripts/dev-cluster.sh restart
#   scripts/dev-cluster.sh status
#   scripts/dev-cluster.sh logs      # tail -f 开发集群日志
#   scripts/dev-cluster.sh start --foreground   # 前台跑，方便看 traceback
#
# 常用覆盖（环境变量）：
#   HIVE_DEV_PORT=8888  HIVE_DEV_STATE=~/.hive/dev-cluster  HIVE_DEV_NAME=dev

set -eu

# 仓库根目录 = 本脚本所在目录的上一级
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

# ---- 隔离参数（与生产集群逐项错开，务必成对修改） ---------------------- #
DEV_PORT="${HIVE_DEV_PORT:-8888}"              # 生产 8788
DEV_DISCOVERY_PORT="${HIVE_DEV_DISCOVERY_PORT:-8889}"  # 生产 8789
DEV_NAME="${HIVE_DEV_NAME:-dev}"
DEV_STATE="${HIVE_DEV_STATE:-$HOME/.hive/dev-cluster}"
DEV_CLUSTER_ID="${HIVE_DEV_CLUSTER_ID:-hive-dev}"      # 生产 hive
# 故意不启用 store / MCP：开发时用不上，也避免抢生产的 sqlite 与 8790 端口。
DEV_LOG="$DEV_STATE/cluster.log"
DEV_PID="$DEV_STATE/cluster.pid"

PYTHON="${PYTHON:-python3}"
HIVE="$REPO_DIR/hive.py"

[ -f "$HIVE" ] || { echo "找不到 $HIVE（脚本应位于仓库的 scripts/ 下）" >&2; exit 1; }

# 统一的环境：把这些变量固定下来，子进程不会继承到生产的任何设置。
dev_env() {
    export HIVE_CLUSTER_STATE="$DEV_STATE"
    export HIVE_CLUSTER_ID="$DEV_CLUSTER_ID"
    export HIVE_CLUSTER_PORT="$DEV_PORT"
    export HIVE_CLUSTER_DISCOVERY_PORT="$DEV_DISCOVERY_PORT"
    # 关闭广播发现：否则两台集群会互相 announce（虽因 cluster_id 不同被拒，
    # 但广播本身会让日志与 CPU 白跑）。
    export HIVE_CLUSTER_DISCOVER=false
    export HIVE_CLUSTER_SYNC=false
    export HIVE_CLUSTER_MCP=false
    # 单机开发：本机就是主，不等选举窗口，也不与生产争 priority。
    export HIVE_CLUSTER_PRIORITY="${HIVE_DEV_PRIORITY:-1}"
}

is_running() {
    [ -f "$DEV_PID" ] || return 1
    pid=$(cat "$DEV_PID" 2>/dev/null || true)
    [ -n "${pid:-}" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

case "${1:-status}" in
start)
    shift || true
    if is_running; then
        echo "开发集群已在运行（pid=$(cat "$DEV_PID")）"
        exit 0
    fi
    mkdir -p "$DEV_STATE"
    dev_env
    if [ "${1:-}" = "--foreground" ]; then
        echo "前台启动：state=$DEV_STATE port=$DEV_PORT cluster_id=$DEV_CLUSTER_ID"
        exec "$PYTHON" "$HIVE" --dir "$REPO_DIR" cluster serve \
            --name "$DEV_NAME" --port "$DEV_PORT" --host 127.0.0.1 \
            --discovery-port "$DEV_DISCOVERY_PORT" --state-dir "$DEV_STATE" \
            --no-discover --no-sync --no-mcp --no-store
    fi
    echo "启动开发集群：state=$DEV_STATE port=$DEV_PORT cluster_id=$DEV_CLUSTER_ID"
    nohup "$PYTHON" "$HIVE" --dir "$REPO_DIR" cluster serve \
        --name "$DEV_NAME" --port "$DEV_PORT" --host 127.0.0.1 \
        --discovery-port "$DEV_DISCOVERY_PORT" --state-dir "$DEV_STATE" \
        --no-discover --no-sync --no-mcp --no-store \
        >>"$DEV_LOG" 2>&1 &
    echo $! >"$DEV_PID"
    sleep 2
    if is_running; then
        echo "已启动（pid=$(cat "$DEV_PID")）。日志：$DEV_LOG"
        echo "地址：http://127.0.0.1:$DEV_PORT/api/cluster/health"
    else
        echo "启动失败，日志末尾：" >&2
        tail -n 20 "$DEV_LOG" 2>/dev/null || true
        exit 1
    fi
    ;;
stop)
    if is_running; then
        pid=$(cat "$DEV_PID")
        kill "$pid" 2>/dev/null || true
        # 给优雅停机 10s，再强杀
        i=0
        while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 50 ]; do
            i=$((i + 1)); sleep 0.2
        done
        kill -9 "$pid" 2>/dev/null || true
        echo "开发集群已停止（pid=$pid）"
    else
        echo "开发集群未在运行"
    fi
    rm -f "$DEV_PID"
    ;;
restart)
    "$0" stop
    "$0" start "$@"
    ;;
status)
    if is_running; then
        echo "开发集群：运行中（pid=$(cat "$DEV_PID")）"
        curl -s -m 3 "http://127.0.0.1:$DEV_PORT/api/cluster/health" 2>/dev/null \
            && echo || echo "（端口 $DEV_PORT 暂未响应）"
    else
        echo "开发集群：未运行"
    fi
    echo "state=$DEV_STATE  port=$DEV_PORT  cluster_id=$DEV_CLUSTER_ID"
    ;;
logs)
    exec tail -n 50 -f "$DEV_LOG"
    ;;
*)
    echo "用法：$0 {start|stop|restart|status|logs} [--foreground]" >&2
    exit 2
    ;;
esac
