#!/bin/sh
# join-agent.sh —— 把一台 Linux / macOS / FreeBSD 机器挂到 hive 主控上
#
# 用法（在 hive 仓根执行）：
#   scripts/join-agent.sh fb88-dsh
#   scripts/join-agent.sh my-node --mode exec --command "/usr/local/bin/codebuddy -p -"
#   scripts/join-agent.sh my-mac --mode manual --launch "open -a Trae {worktree}"
#   scripts/join-agent.sh fb88-dsh --status
#   scripts/join-agent.sh fb88-dsh --uninstall
#
# 它做三件事：
#   1. `hive agent add --dispatchable`：登记并允许主控自动派卡
#   2. 把 worker 拉起来（scripts/worker.sh start）
#   3. 打印自启方案（本机有没有 init 差别很大，见脚本末尾）
#
# 为什么要 --dispatchable：dispatch 默认跳过所有 manual agent。没有这个开关，
# 主控永远派不出卡，人只能手工 `hive prompt` + copy。
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
COORD="${HIVE_COORD:-$DIR}"
MODE="auto"
COMMAND=""
LAUNCH=""
WORKTREES=""
REPOS=""
INTERVAL="${INTERVAL:-30}"
MAXC="${MAX_CONCURRENCY:-1}"
DISPATCHABLE=1
NOSTART=0
ACTION="join"
AGENT=""

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

# 第一个非选项参数是 agent 名
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)        MODE="$2"; shift 2 ;;
        --command)     COMMAND="$2"; shift 2 ;;
        --launch)      LAUNCH="$2"; shift 2 ;;
        --worktrees)   WORKTREES="$2"; shift 2 ;;
        --repo)        REPOS="$REPOS $2"; shift 2 ;;
        --coord)       COORD="$2"; shift 2 ;;
        --interval)    INTERVAL="$2"; shift 2 ;;
        --max-concurrency) MAXC="$2"; shift 2 ;;
        --no-dispatchable) DISPATCHABLE=0; shift ;;
        --no-start)    NOSTART=1; shift ;;
        --status)      ACTION="status"; shift ;;
        --uninstall)   ACTION="uninstall"; shift ;;
        --probe)       ACTION="probe"; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             AGENT="$1"; shift ;;
    esac
done

# --probe 不需要 agent 名：它只是看看这台机器能走哪条路，
# 常在「还没决定叫什么名字」的时候先跑一次。
if [ "$ACTION" = "join" ] && [ -z "$AGENT" ]; then
    usage; echo "" >&2; echo "缺少 agent 名" >&2; exit 2
fi

STATE="${HIVE_STATE:-$HOME/.hive}"

case "$ACTION" in
    status)
        sh "$DIR/scripts/worker.sh" status "$AGENT"
        echo "--- agent 登记 ---"
        python3 "$DIR/hive.py" --dir "$COORD" agent list 2>/dev/null | grep -E "^$AGENT " \
            || echo "（没登记）"
        exit 0
        ;;
    uninstall)
        sh "$DIR/scripts/worker.sh" stop "$AGENT"
        echo "已停 worker。agent 登记仍在 agents/$AGENT.json，要彻底删就 git rm 它。"
        exit 0
        ;;
    probe)
        # 自检：只看这台机器能走哪条路，不登记、不写任何文件。
        exec python3 "$DIR/scripts/agent-exec.py" --probe
        ;;
esac

# --- 0.5 自动选模式 -------------------------------------------------------------
# 和 join-agent.ps1 的 -Mode auto 同一个语义：机器的主人不需要知道 exec / manual
# 的区别，脚本看这台机器有什么，选能走通的最自动的那条路。
if [ "$MODE" = "auto" ]; then
    echo "自动探测本机 AI CLI…"
    if python3 "$DIR/scripts/agent-exec.py" --probe >/dev/null 2>&1; then
        MODE="exec"
        # 统一走适配器：换机器、换 CLI、调参数都只改适配器，不动 agent 配置。
        [ -n "$COMMAND" ] || COMMAND="python3 $DIR/scripts/agent-exec.py"
        echo "  找到可用 CLI → exec：主控派卡后全程无人"
    else
        MODE="manual"
        echo "  没找到 headless CLI → manual：worker 建好工作区并等 AI 接手"
        echo "  想变全自动就装 codebuddy / traecli / dsh，然后重跑本脚本"
    fi
fi

# --- 1. 登记 -------------------------------------------------------------------
set -- agent add --name "$AGENT" --mode "$MODE" \
     --worktrees "${WORKTREES:-$STATE/wt/$AGENT}" --max-concurrency "$MAXC"
[ "$DISPATCHABLE" = 1 ] && set -- "$@" --dispatchable
[ -n "$COMMAND" ] && set -- "$@" --command "$COMMAND"
[ -n "$LAUNCH" ] && set -- "$@" --launch "$LAUNCH"
if [ -n "$REPOS" ]; then
    for r in $REPOS; do set -- "$@" --repo "$r"; done
else
    set -- "$@" --repo "hive=$DIR"
fi

echo "登记 agent $AGENT（mode=$MODE）…"
python3 "$DIR/hive.py" --dir "$COORD" "$@" || exit 1

if [ "$MODE" = "exec" ] && [ -z "$COMMAND" ]; then
    echo "警告：exec 模式没给 --command，worker 启动会直接失败" >&2
fi
if [ "$MODE" = "manual" ] && [ -z "$LAUNCH" ]; then
    echo "警告：manual 模式没给 --launch，worker 只会把 prompt 放进剪贴板，不会打开 IDE" >&2
fi

# --- 2. 起 worker ---------------------------------------------------------------
if [ "$NOSTART" = 1 ]; then
    echo "已登记（--no-start，没起 worker）"
    exit 0
fi

INTERVAL="$INTERVAL" HIVE_DIR="$COORD" \
    sh "$DIR/scripts/worker.sh" start "$AGENT" --max-concurrency "$MAXC"

# --- 3. 自启提示 ---------------------------------------------------------------
echo ""
echo "自启（可选）："
if [ -d /run/systemd/system ] || [ -d /etc/systemd/system ]; then
    echo "  systemd 可用，但没有现成的 worker unit；用 cron @reboot 或 tmux 更省事："
    echo "    tmux new -s hive-$AGENT -d \"$DIR/scripts/worker.sh start $AGENT\""
elif [ "$(uname -s)" = "FreeBSD" ] && [ -d /etc/rc.d ]; then
    echo "  FreeBSD 宿主：可写 /usr/local/etc/rc.d/hive_worker_$AGENT（需 root）"
    echo "  当前在 linuxulator 兼容层里的话没有 init，用 tmux："
    echo "    tmux new -s hive-$AGENT -d \"$DIR/scripts/worker.sh start $AGENT\""
else
    echo "  没有 init 的环境（chroot / 容器 / 兼容层）装不了开机自启。两条替代："
    echo "    tmux new -s hive-$AGENT -d \"$DIR/scripts/worker.sh start $AGENT\""
    echo "    或让主控那边的 orchestrate 发现该 agent 长期不接卡后提醒人"
fi
echo ""
echo "主控侧确认："
echo "  python3 hive.py --dir $COORD agent list"
echo "  python3 hive.py --dir $COORD dispatch      # dry-run 看会派给谁"
