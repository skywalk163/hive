#!/bin/sh
# 蜂巢 Hive —— 跨平台冒烟脚本（GitHub Actions 用，也可本地跑）
#
#   sh scripts/ci-smoke.sh syntax      # 所有 .sh 过一遍 sh -n
#   sh scripts/ci-smoke.sh service     # 看板服务：start → 探活 → stop → 确认端口释放
#   sh scripts/ci-smoke.sh autostart   # 自启安装器：走对分支、不乱改系统（dry-run）
#   sh scripts/ci-smoke.sh all         # 上面三个都跑
#
# 为什么单独抽成脚本而不是写在 YAML 里：本地也能原样跑一遍，
# 免得「CI 上红、本地没法复现」。
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${SMOKE_PORT:-8799}"     # 别用 8791，避免和本机在跑的真看板撞端口
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok   %s\n' "$*"; }
bad()  { printf '  FAIL %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }

# --------------------------------------------------------------------------- #
do_syntax() {
    say "== 语法检查（sh -n）=="
    n=0
    for f in "$DIR"/scripts/*.sh "$DIR"/scripts/freebsd-host/*.sh; do
        [ -f "$f" ] || continue
        if sh -n "$f" 2>/tmp/smoke.err; then
            n=$((n + 1))
        else
            bad "$(basename "$f")：$(cat /tmp/smoke.err)"
        fi
    done
    ok "$n 个脚本语法通过"
}

# --------------------------------------------------------------------------- #
do_service() {
    say "== 看板服务起停（端口 $PORT）=="
    SVC="$DIR/scripts/dashboard-service.sh"
    if [ ! -x "$SVC" ]; then
        bad "dashboard-service.sh 不可执行"
        return
    fi
    STATE="$(mktemp -d 2>/dev/null || echo /tmp/hive-smoke-$$)"
    export HIVE_STATE="$STATE"
    export PORT INTERVAL=5
    export CI_REPOS=""          # 公网 CI 连不到内网 Gitea，拉 CI 状态只会白等超时
    export CI_DISABLED=1

    dump_logs() {
        say "      --- dashboard.log ---"
        sed -n '$p;1,30p' "$STATE/logs/dashboard.log" 2>/dev/null | sed 's/^/      /'
        say "      --- status.err ---"
        sed -n '1,10p' "$DIR/dashboard/status.err" 2>/dev/null | sed 's/^/      /'
    }

    if ! sh "$SVC" start >/tmp/smoke.start 2>&1; then
        bad "start 失败：$(cat /tmp/smoke.start)"
        dump_logs
        return
    fi
    ok "start"

    i=0
    up=0
    while [ "$i" -lt 20 ]; do
        if curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/"; then up=1; break; fi
        i=$((i + 1))
        sleep 0.5
    done
    if [ "$up" -eq 1 ]; then
        ok "HTTP 探活通过"
    else
        bad "HTTP 探活失败（$PORT 没起来）"
        dump_logs
    fi

    sh "$SVC" status >/tmp/smoke.status 2>&1 && ok "status：运行中" || bad "status 说没在跑"

    sh "$SVC" stop >/tmp/smoke.stop 2>&1 || bad "stop 失败：$(cat /tmp/smoke.stop)"
    sleep 1
    sh "$SVC" status >/dev/null 2>&1 && bad "stop 之后 status 仍说在跑" || ok "stop 后确认已停"

    rm -rf "$STATE" 2>/dev/null || true
}

# --------------------------------------------------------------------------- #
do_autostart() {
    say "== 自启安装器（dry-run，不改系统）=="
    INST="$DIR/scripts/install-autostart.sh"
    [ -f "$INST" ] || { bad "找不到 install-autostart.sh"; return; }

    sh "$INST" --dry-run >/tmp/smoke.auto 2>&1
    rc=$?
    case "$rc" in
        0) ok "选定了安装方式（rc=0）" ;;
        3) ok "环境无服务管理器，按设计拒绝（rc=3）" ;;
        *) bad "install-autostart.sh --dry-run 退出码 $rc：$(tail -3 /tmp/smoke.auto)" ;;
    esac

    # 输出里必须能看出它到底判断成了什么环境，否则等于没测
    if grep -q "方式：" /tmp/smoke.auto || grep -q "没有可用的服务管理器" /tmp/smoke.auto; then
        ok "环境判定有明确输出"
    else
        bad "输出里既没有选定的方式、也没有拒绝理由"
    fi
    sed -n '1,6p' /tmp/smoke.auto | sed 's/^/      /'
}

# --------------------------------------------------------------------------- #
case "${1:-all}" in
    syntax)    do_syntax ;;
    service)   do_service ;;
    autostart) do_autostart ;;
    all)       do_syntax; do_service; do_autostart ;;
    *)         say "用法: $0 {syntax|service|autostart|all}" >&2; exit 2 ;;
esac

say ""
if [ "$FAIL" -gt 0 ]; then
    say "✗ $FAIL 项未通过"
    exit 1
fi
say "✓ 全部通过"
