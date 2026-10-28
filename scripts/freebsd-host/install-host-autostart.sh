#!/bin/sh
#
# 蜂巢 Hive 看板 —— FreeBSD 宿主侧自启安装器（**必须在宿主上以 root 运行**）
#
#   sudo sh /home/skywalk/dswork/hive/scripts/freebsd-host/install-host-autostart.sh
#   sudo sh ... --dry-run      只打印，不落盘
#   sudo sh ... --uninstall    关掉并移除
#
# 为什么不能从兼容层里跑：兼容层是 chroot，看不见宿主的 /etc /etc/rc.conf
# 与 /usr/local/etc，也没有权限写它们。所以这一步只能你到宿主 shell 执行。
#
# 它会做三件事：
#   1) 把 hive_dashboard 这个 rc 脚本装到 /usr/local/etc/rc.d/
#   2) sysrc hive_dashboard_enable=YES（以及 chroot/dir/user 的默认值，已存在则不覆盖）
#   3) service hive_dashboard start，并回显一次探活结果
set -u

RCD_SRC="$(cd "$(dirname "$0")" && pwd)/hive_dashboard"
RCD_DST="/usr/local/etc/rc.d/hive_dashboard"
CHROOT_DIR="${HIVE_CHROOT:-/compat/ubuntu}"
HIVE_DIR="${HIVE_DIR:-/home/skywalk/dswork/hive}"
HIVE_USER="${HIVE_USER:-skywalk}"
PORT="${PORT:-8791}"
INTERVAL="${INTERVAL:-20}"

DRY=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n)   DRY=1 ;;
        --uninstall|-u) UNINSTALL=1 ;;
        -h|--help)      awk '/^set -u/{exit} NR>1 && /^#/{sub(/^# \{0,1\}/,""); print}' "$0"; exit 0 ;;
        *) echo "未知参数：${arg}（用 -h 看用法）" >&2; exit 2 ;;
    esac
done

run() {
    if [ "$DRY" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

if [ "$(uname -s)" != "FreeBSD" ]; then
    echo "✗ 这个脚本只能在 FreeBSD 宿主上跑（当前 uname -s = $(uname -s)）。" >&2
    echo "  如果你看到的是 Linux，说明你还在 /compat/ubuntu 兼容层里 —— 退出它，到宿主执行。" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "✗ 需要 root：sudo sh $0" >&2
    exit 1
fi

if [ ! -f "$RCD_SRC" ]; then
    echo "✗ 找不到 $RCD_SRC" >&2
    exit 1
fi

if [ "$UNINSTALL" -eq 1 ]; then
    run service hive_dashboard stop
    run sysrc -x hive_dashboard_enable
    run rm -f "$RCD_DST"
    echo "已卸载。重启后不会再自动拉起看板（按需自愈仍然有效）。"
    exit 0
fi

echo "→ 安装 $RCD_DST"
run install -m 755 "$RCD_SRC" "$RCD_DST"

echo "→ 写 rc.conf（已存在的键不覆盖）"
run sysrc -f /etc/rc.conf hive_dashboard_enable=YES
run sysrc -f /etc/rc.conf "hive_dashboard_chroot=$CHROOT_DIR"
run sysrc -f /etc/rc.conf "hive_dashboard_dir=$HIVE_DIR"
run sysrc -f /etc/rc.conf "hive_dashboard_user=$HIVE_USER"
run sysrc -f /etc/rc.conf "hive_dashboard_port=$PORT"
run sysrc -f /etc/rc.conf "hive_dashboard_interval=$INTERVAL"

echo "→ 启动并探活"
run service hive_dashboard start
if [ "$DRY" -eq 0 ]; then
    sleep 2
    if command -v fetch >/dev/null 2>&1; then
        if fetch -q -o /dev/null -T 5 "http://127.0.0.1:$PORT/"; then
            echo "✓ 看板可用：http://127.0.0.1:$PORT/"
        else
            echo "! 启动命令已执行，但 $PORT 还没通，看一眼 ~/.hive/logs/dashboard.log" >&2
        fi
    else
        echo "（没有 fetch，跳过探活）看板地址：http://127.0.0.1:$PORT/"
    fi
fi

cat <<EOF

后续管理：
  service hive_dashboard status|start|stop|restart
  sysrc hive_dashboard_enable=NO     # 关掉开机自启

说明：这套 rc 只是把「开机」这个时机接上；平时即使没开机自启，任何 hive 命令
      也会顺手把看板拉起来（按需自愈），两套机制互不冲突。
EOF
