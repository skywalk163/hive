#!/bin/sh
# 蜂巢 Hive 看板 —— 开机自启安装器（Linux / macOS / FreeBSD）
#
# 用法：
#   scripts/install-autostart.sh                # 按当前系统安装
#   scripts/install-autostart.sh --dry-run      # 只打印将要做的事，不落盘
#   scripts/install-autostart.sh --uninstall    # 卸载自启
#   PORT=8787 INTERVAL=20 scripts/install-autostart.sh   # 端口默认取 HIVE_DASHBOARD_PORT，再退回 8787
#
# 各系统采用的方式（都是该系统下"成熟服务"的常规做法）：
#   Linux   + systemd : ~/.config/systemd/user/hive-dashboard.service + systemctl --user enable
#   Linux  无 systemd : crontab @reboot
#   macOS             : ~/Library/LaunchAgents/com.hive.dashboard.plist + launchctl load
#   FreeBSD           : /usr/local/etc/rc.d/hive_dashboard（daemon(8) 托管）+ rc.conf 开关
#   Windows           : 本脚本不支持，请用 scripts/install-autostart.ps1（计划任务）
#
# 无 init 的环境（chroot / FreeBSD linuxulator 兼容层 / 部分容器）会**直接报错退出**，
# 不再静默去写一张没人执行的 crontab（2026-10-27 踩过：写进去的 @reboot 一次都没触发）。
# 那种环境请用两条替代路线：
#   * 按需自愈：任何 hive 命令都会探活并拉起看板（python3 hive.py dash）
#   * 宿主 FreeBSD 侧：scripts/freebsd-host/install-host-autostart.sh（需 root）
#
# 注意：systemctl --user 只在用户登录后启动；要真·开机启动需 root 执行
#       `loginctl enable-linger $USER`（安装器会提示）。
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVE="$DIR/scripts/serve-dashboard.sh"
PORT="${PORT:-${HIVE_DASHBOARD_PORT:-8787}}"
INTERVAL="${INTERVAL:-20}"
STATE_DIR="${HIVE_STATE:-$HOME/.hive}"
LOG_DIR="$STATE_DIR/logs"
LOG_FILE="$LOG_DIR/dashboard.log"

DRY=0
UNINSTALL=0
FORCE_CRON=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n)   DRY=1 ;;
        --uninstall|-u) UNINSTALL=1 ;;
        --force-cron)   FORCE_CRON=1 ;;
        -h|--help)      awk '/^set -u/{exit} NR>1 && /^#/{sub(/^# \{0,1\}/,""); print}' "$0"; exit 0 ;;
        *) echo "未知参数：${arg}（用 -h 看用法）" >&2; exit 2 ;;
    esac
done

if [ ! -f "$SERVE" ]; then
    echo "找不到 $SERVE" >&2
    exit 1
fi

run() {   # run <命令...>：dry-run 时只打印
    if [ "$DRY" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}
say() { printf '%s\n' "$*"; }

OS="$(uname -s 2>/dev/null || echo unknown)"
mkdir -p "$LOG_DIR" 2>/dev/null || true

# --------------------------------------------------------------------------- #
# Linux + systemd
# --------------------------------------------------------------------------- #
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/hive-dashboard.service"

install_systemd() {
    say "→ 方式：systemd --user 单元"
    if [ "$UNINSTALL" -eq 1 ]; then
        run systemctl --user stop hive-dashboard.service
        run systemctl --user disable hive-dashboard.service
        run rm -f "$UNIT_FILE"
        run systemctl --user daemon-reload
        say "已卸载 systemd 单元"
        return 0
    fi
    if [ "$DRY" -eq 0 ]; then
        mkdir -p "$UNIT_DIR"
        cat >"$UNIT_FILE" <<EOF
[Unit]
Description=蜂巢 Hive 任务看板
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=PORT=$PORT
Environment=INTERVAL=$INTERVAL
ExecStart=$SERVE
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    else
        say "[dry-run] 写入 $UNIT_FILE"
    fi
    run systemctl --user daemon-reload
    run systemctl --user enable hive-dashboard.service
    run systemctl --user restart hive-dashboard.service
    say "已安装并启动：http://${HIVE_DASHBOARD_HOST:-0.0.0.0}:$PORT/"
    say ""
    say "提示：systemctl --user 需登录后才会启动。要开机即启动（不登录也跑），"
    say "      请让 root 执行一次：  sudo loginctl enable-linger $USER"
}

# --------------------------------------------------------------------------- #
# Linux 无 systemd：cron @reboot
# --------------------------------------------------------------------------- #
CRON_TAG='dashboard-service.sh'

install_cron() {
    say "→ 方式：crontab @reboot"
    if [ "$UNINSTALL" -eq 1 ]; then
        if [ "$DRY" -eq 0 ]; then
            crontab -l 2>/dev/null | grep -v "$CRON_TAG" | crontab - 2>/dev/null || crontab -r 2>/dev/null
        else
            say "[dry-run] 从 crontab 移除含 $CRON_TAG 的行"
        fi
        say "已卸载 @reboot 条目"
        return 0
    fi
    ENTRY="@reboot /bin/sh -c 'sleep 15; export PATH=/usr/bin:/bin:\$PATH; PORT=$PORT INTERVAL=$INTERVAL $DIR/scripts/dashboard-service.sh start' >> $LOG_DIR/dashboard-cron.log 2>&1"
    if [ "$DRY" -eq 0 ]; then
        ( crontab -l 2>/dev/null | grep -v "$CRON_TAG"; echo "$ENTRY" ) | crontab -
    else
        printf '[dry-run] crontab 追加：%s\n' "$ENTRY"
    fi
    say "已安装 crontab @reboot：开机 15 秒后拉起看板"
    say "现在手动起一次：$DIR/scripts/dashboard-service.sh start"
}

# --------------------------------------------------------------------------- #
# macOS：launchd
# --------------------------------------------------------------------------- #
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$PLIST_DIR/com.hive.dashboard.plist"
PLIST_LABEL="com.hive.dashboard"

install_launchd() {
    say "→ 方式：launchd LaunchAgent"
    if [ "$UNINSTALL" -eq 1 ]; then
        run launchctl unload "$PLIST_FILE" 2>/dev/null
        run rm -f "$PLIST_FILE"
        say "已卸载 LaunchAgent"
        return 0
    fi
    if [ "$DRY" -eq 0 ]; then
        mkdir -p "$PLIST_DIR" "$LOG_DIR"
        cat >"$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>PORT=$PORT INTERVAL=$INTERVAL exec $SERVE</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_FILE</string>
  <key>StandardErrorPath</key>
  <string>$LOG_FILE</string>
</dict>
</plist>
EOF
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
    else
        say "[dry-run] 写入 $PLIST_FILE"
    fi
    run launchctl load "$PLIST_FILE"
    say "已安装并加载：http://${HIVE_DASHBOARD_HOST:-0.0.0.0}:$PORT/（KeepAlive=true，挂了会自动拉起）"
}

# --------------------------------------------------------------------------- #
# FreeBSD：rc.d
# --------------------------------------------------------------------------- #
RCD_FILE="/usr/local/etc/rc.d/hive_dashboard"
RC_CONF="/etc/rc.conf"

install_rcd() {
    say "→ 方式：FreeBSD rc.d 服务"
    if [ "$UNINSTALL" -eq 1 ]; then
        run service hive_dashboard stop
        run rm -f "$RCD_FILE"
        say "请再手动从 $RC_CONF 删掉 hive_dashboard_enable 一行"
        return 0
    fi
    TMP="$(mktemp 2>/dev/null || echo /tmp/hive_dashboard.rc)"
    cat >"$TMP" <<'EOF'
#!/bin/sh
# PROVIDE: hive_dashboard
# REQUIRE: NETWORKING
# KEYWORD: shutdown

. /etc/rc.subr

name="hive_dashboard"
rcvar="hive_dashboard_enable"
desc="蜂巢 Hive 任务看板"

load_rc_config $name

: ${hive_dashboard_enable:="NO"}
: ${hive_dashboard_port:="8787"}
: ${hive_dashboard_interval:="20"}
: ${hive_dashboard_dir:="__DIR__"}
: ${hive_dashboard_user:="__USER__"}

pidfile="/var/run/${name}.pid"
procname="/usr/sbin/daemon"
command="/usr/sbin/daemon"
command_args="-f -p ${pidfile} /bin/sh -c 'PORT=${hive_dashboard_port} INTERVAL=${hive_dashboard_interval} ${hive_dashboard_dir}/scripts/serve-dashboard.sh'"

start_precmd="hive_dashboard_precmd"
hive_dashboard_precmd()
{
    install -o "${hive_dashboard_user}" /dev/null "${pidfile}"
}

run_rc_command "$1"
EOF
    # 占位符替换（用 sed 而非 shell 变量，避免 here-doc 里的 $ 被吃掉）
    sed -i '' -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER|g" "$TMP" 2>/dev/null \
        || sed -i -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER|g" "$TMP"

    if [ "$DRY" -eq 1 ]; then
        say "[dry-run] 安装 ${RCD_FILE}（需 root）"
        say "[dry-run] 在 $RC_CONF 追加 hive_dashboard_enable=\"YES\""
        rm -f "$TMP"
        return 0
    fi
    if [ "$(id -u)" -eq 0 ]; then
        install -m 755 "$TMP" "$RCD_FILE"
    elif command -v sudo >/dev/null 2>&1; then
        sudo install -m 755 "$TMP" "$RCD_FILE" || {
            say "写入 $RCD_FILE 失败（需要 root）。手动步骤：" >&2
            say "  sudo install -m 755 $TMP $RCD_FILE" >&2
            return 1
        }
    else
        say "需要 root 才能写 ${RCD_FILE}，且本机没有 sudo。手动步骤：" >&2
        say "  sudo install -m 755 $TMP $RCD_FILE" >&2
        return 1
    fi
    rm -f "$TMP"
    if ! grep -q "hive_dashboard_enable" "$RC_CONF" 2>/dev/null; then
        if [ "$(id -u)" -eq 0 ]; then
            echo 'hive_dashboard_enable="YES"' >>"$RC_CONF"
        else
            say "请执行：echo 'hive_dashboard_enable=\"YES\"' | sudo tee -a $RC_CONF"
        fi
    fi
    if command -v service >/dev/null 2>&1; then
        service hive_dashboard start 2>/dev/null || say "启动：service hive_dashboard start"
    fi
    say "已安装 FreeBSD 服务：http://${HIVE_DASHBOARD_HOST:-0.0.0.0}:$PORT/"
    say "管理：service hive_dashboard {start|stop|status|restart}"
}

# --------------------------------------------------------------------------- #
# systemd --user 是否真的可用。
# 不能只看 `systemctl --user list-unit-files`：用户总线不通时它仍可能返回 0（假阳性），
# 但 daemon-reload / enable / start 全会报 "Process org.freedesktop.systemd1 exited
# with status 1"。必须实测到 ManagerState=running 才算数。
systemd_user_ok() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user show --property=ManagerState 2>/dev/null | grep -q 'ManagerState=running'
}

# 我们是不是跑在 FreeBSD 的 Linux 兼容层（linuxulator + chroot）里？
# 判据：uname 说 Linux，但 /proc/version 里露着 FreeBSD 字样。
in_freebsd_linux_compat() {
    [ "$OS" = "Linux" ] || return 1
    grep -qi 'freebsd' /proc/version 2>/dev/null
}

# 有没有一个**属于我们这个 root** 的 cron 守护进程。
#
# 注意这里必须比对 /proc/<pid>/root：FreeBSD 上各 jail 共享 PID 空间，直接
# `pgrep -x cron` 会看到别的 jail 的 cron，得出"有 cron"的错误结论（本机踩过：
# 11 个 /usr/sbin/cron 里 10 个属于 /usr/jails/lims-*，剩下那个 root=/ 的是宿主
# 自己的 FreeBSD cron —— 从 chroot 里根本无法区分它和"我们的 cron"）。
# 所以这个函数只在**非兼容层**的普通 Linux 上可信；兼容层一律走 report_no_init。
cron_serves_us() {
    mine="$(readlink /proc/self/root 2>/dev/null || echo /)"
    for p in $(pgrep -x cron 2>/dev/null) $(pgrep -x crond 2>/dev/null); do
        r="$(readlink "/proc/$p/root" 2>/dev/null || echo "$mine")"
        [ "$r" = "$mine" ] && return 0
    done
    return 1
}

# 环境里没有任何能被我们驱动起来的服务管理器时，别假装成功。
report_no_init() {
    say ""
    say "✗ 本环境没有可用的服务管理器，装自启没有落点（已中止，未做任何改动）。" >&2
    say "" >&2
    if in_freebsd_linux_compat; then
        say "  诊断：当前是 FreeBSD 的 Linux 兼容层（linuxulator chroot）。" >&2
        say "        uname 报 Linux，但 PID 1 是 FreeBSD 的 init，本层内没有 systemd。" >&2
        say "        层里能看到 cron 进程，但实测它们并不服务我们的 crontab ——" >&2
        say "        @reboot 写进去一次都不会触发（本机 2026-10-27 实测）。" >&2
        say "" >&2
        say "  两个可行办法：" >&2
        say "    1) 按需自愈（推荐，已在 hive 里内置，什么都不用装）：" >&2
        say "         任何 hive 命令都会顺手把看板拉起来；也可显式：" >&2
        say "         python3 $DIR/hive.py dash" >&2
        say "    2) 真要开机自启：到 FreeBSD 宿主上用 root 跑" >&2
        say "         sudo sh $DIR/scripts/freebsd-host/install-host-autostart.sh" >&2
    else
        say "  诊断：找不到 running 的 systemd，也没有 cron 守护进程为本 root 服务。" >&2
        say "  可用 \`python3 hive.py dash\` 按需拉起，或手动把下面这条加进你自己系统的自启：" >&2
    fi
    say "" >&2
    say "  备选命令：PORT=$PORT INTERVAL=$INTERVAL $SERVE" >&2
    say "  （若你确信本环境的 cron 真的会执行你的 crontab，可加 --force-cron 强行装）" >&2
    exit 3
}

say "系统：$OS    端口：$PORT    刷新间隔：${INTERVAL}s"
case "$OS" in
    Linux)
        if systemd_user_ok; then
            install_systemd
        elif in_freebsd_linux_compat && [ "$FORCE_CRON" -eq 0 ]; then
            report_no_init
        elif [ "$FORCE_CRON" -eq 1 ] || cron_serves_us; then
            install_cron
        else
            report_no_init
        fi
        ;;
    Darwin)
        install_launchd
        ;;
    FreeBSD)
        install_rcd
        ;;
    CYGWIN*|MINGW*|MSYS*|Windows_NT)
        say "Windows 请用 PowerShell 安装器：" >&2
        say "  powershell -ExecutionPolicy Bypass -File $DIR/scripts/install-autostart.ps1" >&2
        exit 1
        ;;
    *)
        say "未识别的系统：$OS" >&2
        say "可手动把这条命令加进系统自启：" >&2
        say "  PORT=$PORT INTERVAL=$INTERVAL $SERVE" >&2
        exit 1
        ;;
esac
