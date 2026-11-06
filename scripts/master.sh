#!/bin/sh
# master.sh —— 主控（脑）。它只决定「哪张卡给谁」，不碰任何代码仓的工作区。
#
# 用法：
#   scripts/master.sh                 # 前台跑（看日志用）
#   scripts/master.sh start           # 后台常驻（单实例，PID 文件）
#   scripts/master.sh stop
#   scripts/master.sh restart
#   scripts/master.sh status
#
#   INTERVAL=30 scripts/master.sh start                       # 每轮间隔（默认 60s）
#   EXTRA="--dispatch-limit 3 --max-land 2" scripts/master.sh start
#
# 每一轮 tick 做四件事（实现在 hive.py:cmd_orchestrate）：
#   1. watchdog  回收过期租约 / 孤儿卡 / 僵尸卡
#   2. dispatch  按能力、仓库、负载、paths 冲突、依赖把 ready 卡派给 agent
#   3. land      把 verify 全绿、依赖就绪的 done 卡合入主线并 close
#   4. sync      把本机代码仓快进到 origin 主线（只快进，不强制）
#
# 与 worker 的分工：主控派活，`hive work` 干活。一台机器只需要跑其中一种。
#   主控 = 一台（通常是有 git 写权限的那台）
#   worker = 每台要接活的机器各一个，见 scripts/join-agent.sh
#
# 为什么要有单实例保护：两个主控同时派卡会把同一张卡派给两个 agent
# （claim 那层最终只有一个抢到，但另一个会白跑一轮，还会在卡面上留下
#  互相覆盖的 dispatched_to）。PID 文件是最省事的判定。
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${HIVE_STATE:-$HOME/.hive}"
PID="$STATE/master.pid"
LOG_DIR="$STATE/logs"
LOG="$LOG_DIR/master.log"
INTERVAL="${INTERVAL:-60}"
EXTRA="${EXTRA:-}"

mkdir -p "$LOG_DIR" 2>/dev/null || true

pid_of() { cat "$PID" 2>/dev/null || echo ""; }
alive() {
    p=$(pid_of)
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

do_start() {
    if alive; then
        echo "主控已在跑（PID $(pid_of)）"
        return 0
    fi
    # 用 exec 让 nohup 的进程直接成为 python，$! 才是真 PID。
    # 不写 exec 的话 $! 是中间子 shell，kill 它只会留个孤儿 python 继续派卡。
    # $EXTRA 故意不加引号：它本来就是「待分词」的额外参数。
    ( exec nohup python3 "$DIR/hive.py" --dir "$DIR" orchestrate \
        --interval "$INTERVAL" $EXTRA >> "$LOG" 2>&1 ) &
    echo $! > "$PID"
    sleep 2
    if alive; then
        echo "主控已启动：PID $(pid_of)，间隔 ${INTERVAL}s"
        echo "  日志：$LOG"
    else
        echo "主控启动失败，末尾日志：" >&2
        tail -20 "$LOG" 2>/dev/null >&2
        rm -f "$PID"
        return 1
    fi
}

do_stop() {
    if ! alive; then
        echo "主控没在跑"
        rm -f "$PID"
        return 0
    fi
    p=$(pid_of)
    kill "$p" 2>/dev/null || true
    i=0
    while [ $i -lt 20 ] && kill -0 "$p" 2>/dev/null; do
        sleep 0.5
        i=$((i + 1))
    done
    if kill -0 "$p" 2>/dev/null; then
        kill -9 "$p" 2>/dev/null || true
        echo "主控未响应 TERM，已强杀（PID $p）"
    else
        echo "主控已停止（PID $p）"
    fi
    rm -f "$PID"
}

do_status() {
    if alive; then
        echo "主控运行中：PID $(pid_of)，间隔 ${INTERVAL}s"
        echo "--- 日志末尾 ---"
        tail -8 "$LOG" 2>/dev/null
        return 0
    fi
    echo "主控未运行"
    if [ -f "$LOG" ]; then
        echo "--- 上次日志末尾 ---"
        tail -8 "$LOG" 2>/dev/null
    fi
    return 1
}

action="${1:-foreground}"
case "$action" in
    start)    do_start ;;
    stop)     do_stop ;;
    restart)  do_stop; do_start ;;
    status)   do_status ;;
    foreground|run)
        echo "主控前台运行（Ctrl-C 退出），间隔 ${INTERVAL}s"
        # shellcheck disable=SC2086
        exec python3 "$DIR/hive.py" --dir "$DIR" orchestrate \
            --interval "$INTERVAL" $EXTRA
        ;;
    -h|--help|help)
        sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)
        echo "用法：$0 {start|stop|restart|status|foreground}" >&2
        exit 2
        ;;
esac
