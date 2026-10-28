#!/bin/sh
# 蜂巢 Hive 看板 —— 服务控制脚本（start / stop / restart / status）
#
# 定位：把 scripts/serve-dashboard.sh（前台运行 + 自带状态刷新循环）包装成
#       一个像样的常驻服务：PID 文件、日志、幂等启停、可查状态，供开机自启使用。
#
# 用法：
#   scripts/dashboard-service.sh start      # 已在跑则不动（幂等）
#   scripts/dashboard-service.sh stop       # 优雅停（TERM），超时才 KILL
#   scripts/dashboard-service.sh restart
#   scripts/dashboard-service.sh status     # 没在跑时退出码 1（便于脚本判断）
#
# 可调环境变量：
#   PORT=8791        监听端口（默认 8791）
#   INTERVAL=20      状态刷新间隔秒数（默认 20，交给 serve-dashboard.sh 的循环）
#   HIVE_STATE=~/.hive   PID 与日志目录（默认 ~/.hive）
#
# 开机自启（用户级，无需 root）：
#   ( crontab -l 2>/dev/null | grep -v dashboard-service; \
#     echo "@reboot /bin/sh -c 'sleep 15; PORT=8791 $DIR/scripts/dashboard-service.sh start'" ) | crontab -
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVE="$DIR/scripts/serve-dashboard.sh"

PORT="${PORT:-8791}"
INTERVAL="${INTERVAL:-20}"
STATE_DIR="${HIVE_STATE:-$HOME/.hive}"
RUN_DIR="$STATE_DIR/run"
LOG_DIR="$STATE_DIR/logs"
PID_FILE="$RUN_DIR/dashboard.pid"
LOG_FILE="$LOG_DIR/dashboard.log"

if [ ! -f "$SERVE" ]; then
    echo "找不到 $SERVE" >&2
    exit 1
fi

pid_of() {
    [ -f "$PID_FILE" ] || return 1
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "${pid:-}" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

do_start() {
    if pid="$(pid_of)"; then
        echo "看板已在运行（pid=${pid}），端口 $PORT"
        echo "访问：http://127.0.0.1:$PORT/"
        return 0
    fi
    # 端口被别的进程占着就别硬起，避免"起了但连不上"的假象。
    # 注意用 / 探活而非 /healthz —— serve.py 没有 healthz 路由（会 404 造成误判）。
    # 刚 stop 完端口可能还没释放，所以给几秒重试再下定论。
    i=0
    while [ "$i" -lt 10 ]; do
        if ! curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/" 2>/dev/null; then
            break
        fi
        i=$((i + 1))
        sleep 1
    done
    if [ "$i" -ge 10 ]; then
        echo "端口 $PORT 已被占用，且不是本脚本起的实例，请先排查" >&2
        return 1
    fi
    mkdir -p "$RUN_DIR" "$LOG_DIR"
    cd "$DIR" || return 1
    # setsid 不是 POSIX：Linux 有，macOS/FreeBSD 默认没有。有就用来脱离会话
    # （停止时可以整组回收），没有就退回 nohup，靠 stop 里的兜底清理子进程。
    if command -v setsid >/dev/null 2>&1; then
        PORT="$PORT" INTERVAL="$INTERVAL" \
            setsid nohup "$SERVE" >>"$LOG_FILE" 2>&1 &
    else
        PORT="$PORT" INTERVAL="$INTERVAL" \
            nohup "$SERVE" >>"$LOG_FILE" 2>&1 &
    fi
    echo $! >"$PID_FILE"
    # 等一下确认真起来了（serve.py 监听失败会立刻退出）
    i=0
    while [ "$i" -lt 20 ]; do
        if pid_of >/dev/null; then
            if curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/" 2>/dev/null; then
                echo "看板已启动（pid=$(pid_of)），端口 $PORT"
                echo "访问：http://127.0.0.1:$PORT/"
                echo "日志：$LOG_FILE"
                return 0
            fi
        else
            echo "看板启动失败，看日志：$LOG_FILE" >&2
            tail -n 10 "$LOG_FILE" >&2 2>/dev/null
            rm -f "$PID_FILE"
            return 1
        fi
        i=$((i + 1))
        sleep 0.5
    done
    echo "看板已起进程但 $PORT 未就绪，看日志：$LOG_FILE" >&2
    return 1
}

do_stop() {
    if ! pid="$(pid_of)"; then
        rm -f "$PID_FILE"
        echo "看板未在运行"
        return 0
    fi
    # 先整组回收（setsid 起过时 pid 即 pgid）；负 pid 在部分 shell/OS 上不支持，
    # 失败就退回单进程 kill，再由下面的兜底清理残余子进程。
    kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    i=0
    while [ "$i" -lt 20 ]; do
        kill -0 "$pid" 2>/dev/null || break
        i=$((i + 1))
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "TERM 未生效，强制 KILL $pid" >&2
        kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
    # 兜底：serve.py 是子进程，父脚本被杀后它可能变孤儿继续占端口（restart 就必失败）。
    # 按端口匹配清理；用 [s]erve.py 括号技巧，避免 pkill 匹配到本脚本自身。
    if command -v pkill >/dev/null 2>&1; then
        pkill -f "[s]erve.py --status .*--port $PORT" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "看板已停止"
}

do_status() {
    if pid="$(pid_of)"; then
        echo "运行中（pid=${pid}）"
        echo "端口：$PORT"
        echo "地址：http://127.0.0.1:$PORT/"
        if command -v curl >/dev/null 2>&1; then
            code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$PORT/" 2>/dev/null)"
            echo "HTTP 探活：$code"
        fi
        echo "日志：$LOG_FILE"
        return 0
    fi
    echo "未运行"
    return 1
}

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)  do_status ;;
    *)
        echo "用法: $0 {start|stop|restart|status}" >&2
        exit 2
        ;;
esac
