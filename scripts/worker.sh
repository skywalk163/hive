#!/bin/sh
# worker.sh —— 某台机器上的干活进程（手脚）。它从自己 agent 的收件箱取卡，
# 在独立 worktree 里跑 agent，然后把结果推回协调仓。
#
# 用法：
#   scripts/worker.sh start <agent> [--max-concurrency N] [其它 work 参数]
#   scripts/worker.sh stop  <agent>
#   scripts/worker.sh status [agent]        # 不带 agent 就列全部
#   scripts/worker.sh restart <agent> ...
#
# 例：
#   scripts/worker.sh start fb88-dsh
#   scripts/worker.sh start workbuddyai-win --max-concurrency 2
#   INTERVAL=30 scripts/worker.sh start trae-win
#
# start 之后的参数原样透传给 `hive.py work`，除了 INTERVAL（环境变量）：
#   --pr                 推完 task/<tid> 顺手开 PR
#   --keep               保留 worktree（排查现场用）
#   --no-cluster         不起本机集群（默认会顺手拉起，秒级同步用）
#
# 与 master.sh 的分工：主控派活，worker 干活。一台机器可以只跑 worker；
# 跑主控的那台通常也给自己跑一个 worker（主控不占自己的算力）。
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${HIVE_STATE:-$HOME/.hive}"
INTERVAL="${INTERVAL:-30}"

pidfile_of() { echo "$STATE/agents/$1/worker.pid"; }
logfile_of() { echo "$STATE/agents/$1/logs/worker.log"; }

alive() {
    f=$(pidfile_of "$1")
    [ -f "$f" ] || return 1
    p=$(cat "$f" 2>/dev/null)
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

do_start() {
    agent="${1:-}"
    [ -n "$agent" ] || { echo "用法：$0 start <agent> [work 参数...]" >&2; return 2; }
    shift
    pidfile=$(pidfile_of "$agent")
    logfile=$(logfile_of "$agent")
    mkdir -p "$(dirname "$pidfile")" "$(dirname "$logfile")" 2>/dev/null || true

    if alive "$agent"; then
        echo "worker $agent 已在跑（PID $(cat "$pidfile")）"
        return 0
    fi
    # exec 不可省：否则 $! 是中间子 shell，kill 它只会留个孤儿 python 继续领卡。
    # HIVE_DIR 默认就是工具仓（hive 自举）；协调仓分开部署时用 HIVE_DIR 指过去。
    ( exec nohup env INTERVAL="$INTERVAL" HIVE_DIR="${HIVE_DIR:-$DIR}" \
        HIVE_STATE="$STATE" \
        sh "$DIR/scripts/start-worker.sh" "$agent" --interval "$INTERVAL" "$@" \
        >> "$logfile" 2>&1 ) &
    echo $! > "$pidfile"
    sleep 2
    if alive "$agent"; then
        echo "worker $agent 已启动：PID $(cat "$pidfile")"
        echo "  日志：$logfile"
        return 0
    fi
    echo "worker $agent 启动失败，末尾日志：" >&2
    tail -20 "$logfile" 2>/dev/null >&2
    rm -f "$pidfile"
    return 1
}

do_stop() {
    agent="${1:-}"
    [ -n "$agent" ] || { echo "用法：$0 stop <agent>" >&2; return 2; }
    pidfile=$(pidfile_of "$agent")
    if ! alive "$agent"; then
        echo "worker $agent 没在跑"
        rm -f "$pidfile"
        return 0
    fi
    p=$(cat "$pidfile")
    kill "$p" 2>/dev/null || true
    i=0
    while [ $i -lt 20 ] && kill -0 "$p" 2>/dev/null; do
        sleep 0.5
        i=$((i + 1))
    done
    if kill -0 "$p" 2>/dev/null; then
        kill -9 "$p" 2>/dev/null || true
        echo "worker $agent 未响应 TERM，已强杀（PID $p）"
    else
        echo "worker $agent 已停止（PID $p）"
    fi
    rm -f "$pidfile"
}

do_status() {
    agent="${1:-}"
    if [ -n "$agent" ]; then
        if alive "$agent"; then
            echo "worker $agent 运行中：PID $(cat "$(pidfile_of "$agent")")"
            tail -5 "$(logfile_of "$agent")" 2>/dev/null
            return 0
        fi
        echo "worker $agent 未运行"
        return 1
    fi
    found=0
    for d in "$STATE"/agents/*/; do
        [ -d "$d" ] || continue
        n=$(basename "$d")
        [ -f "$d/worker.pid" ] || continue
        found=1
        if alive "$n"; then
            echo "$n  运行中  PID $(cat "$d/worker.pid")"
        else
            echo "$n  已停（PID 文件残留）"
        fi
    done
    [ "$found" = "1" ] || echo "本机还没有启动过任何 worker"
}

action="${1:-status}"
case "$action" in
    start)    shift; do_start "$@" ;;
    stop)     shift; do_stop "$@" ;;
    restart)  shift; do_stop "${1:-}"; do_start "$@" ;;
    status)   shift; do_status "${1:-}" ;;
    -h|--help|help)
        sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)
        # 兼容 `worker.sh fb88-dsh`（省略 start）
        do_start "$@"
        ;;
esac
