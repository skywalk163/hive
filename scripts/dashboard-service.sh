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
#   PORT=8787        监听端口（默认取 HIVE_DASHBOARD_PORT，再退回 8787）
#   INTERVAL=20      状态刷新间隔秒数（默认 20，交给 serve-dashboard.sh 的循环）
#   HIVE_STATE=~/.hive   PID 与日志目录（默认 ~/.hive）
#
# 端口只有一个来源：HIVE_DASHBOARD_PORT（hive.py 的 DASH_DEFAULT_PORT 由
# hive_ports.py 提供，默认 8787；完整规划见 docs/端口与服务规划.md）。
# 历史上这里默认 8791、而 serve.py/serve-dashboard.sh 默认 8787，导致「手动跑在
# 8787、服务起在 8791」，文档全写 8787，看起来很像是端口被谁改了。现已统一。
#
# ⚠ 改 serve-dashboard.sh 必须 stop → 改 → start：shell 是按字节偏移边读边执行的，
#   原地改一个正在跑的脚本会让它后面的解析整体错位（实测出现过 `esh_ci: not found`
#   和刷新循环空转、日志暴涨几百 MB）。
#
# 开机自启（用户级，无需 root）：
#   ( crontab -l 2>/dev/null | grep -v dashboard-service; \
#     echo "@reboot /bin/sh -c 'sleep 15; PORT=8787 $DIR/scripts/dashboard-service.sh start'" ) | crontab -
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVE="$DIR/scripts/serve-dashboard.sh"

PORT="${PORT:-${HIVE_DASHBOARD_PORT:-8787}}"
# 展示用的绑定地址（探活仍走 127.0.0.1：绑 0.0.0.0 时一样通）
HOST="${HIVE_DASHBOARD_HOST:-0.0.0.0}"

# 给人看的地址：绑 0.0.0.0 时本机/局域网各给一个（具体 IP 用 `hive dash url`）
show_url() {
    if [ "$HOST" = "0.0.0.0" ] || [ -z "$HOST" ]; then
        echo "    本机   : http://127.0.0.1:$PORT/"
        echo "    局域网 : http://<本机IP>:$PORT/（hive dash url 会给具体地址）"
    else
        echo "    地址   : http://$HOST:$PORT/"
    fi
}

INTERVAL="${INTERVAL:-20}"
WAIT_S="${WAIT_S:-30}"   # 等 serve.py 就绪的秒数：大仓首次 `hive status` 可能要几十秒
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
        show_url
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
    # 日志轮转：只在启动时做（运行中 mv 会和正在写的进程抢）。
    # 刷新循环一旦出问题会以每秒上百行的速度灌日志，实测到过 338MB 没人发现。
    if [ -f "$LOG_FILE" ]; then
        size="$(wc -c <"$LOG_FILE" 2>/dev/null | tr -d ' ')"
        if [ "${size:-0}" -gt 10485760 ]; then
            mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
        fi
    fi
    cd "$DIR" || return 1
    # setsid 不是 POSIX：Linux 有，macOS/FreeBSD 默认没有。有就用来脱离会话
    # （停止时可以整组回收），没有就退回 nohup，靠 stop 里的兜底清理子进程。
    if command -v setsid >/dev/null 2>&1; then
        PORT="$PORT" HIVE_DASHBOARD_PORT="$PORT" INTERVAL="$INTERVAL" \
            setsid nohup "$SERVE" >>"$LOG_FILE" 2>&1 &
    else
        PORT="$PORT" HIVE_DASHBOARD_PORT="$PORT" INTERVAL="$INTERVAL" \
            nohup "$SERVE" >>"$LOG_FILE" 2>&1 &
    fi
    echo $! >"$PID_FILE"
    # 等一下确认真起来了（serve.py 监听失败会立刻退出）
    i=0
    tries=$((WAIT_S * 2))
    while [ "$i" -lt "$tries" ]; do
        if pid_of >/dev/null; then
            if curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/" 2>/dev/null; then
                echo "看板已启动（pid=$(pid_of)），端口 $PORT"
                show_url
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
        show_url
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
