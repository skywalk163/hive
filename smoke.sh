#!/bin/sh
# 蜂巢 Hive —— 一键冒烟（T-0237）
#
# 只验四件事，全程在一个临时裸仓上跑，**绝不碰真实任务队列**：
#
#   1) hive doctor      必须过，且输出里没有 [ERR ]
#   2) hive task new    建一张卡（真的会 commit + push，所以才要临时裸仓）
#   3) hive task list   必须看得到刚建的卡
#   4) 删掉这张卡       list 必须看不到了（否则前面「看得到」可能是假阳性）
#
# 为什么不在真实队列里建卡：`task new` 会立刻 push 到 origin/main，跑一次就在
# 全世界所有 agent 的看板上多一张脏卡，删卡还得再 push 一次。所以远端用
# `mktemp -d` 里新建的裸仓，跑完整个目录删掉，外面什么痕迹都不留。
#
# 用法：
#   sh smoke.sh              # 全跑，成功自动清理临时目录
#   KEEP=1 sh smoke.sh       # 留着临时目录（排查用；失败时默认就会留）
#
# 依赖：sh、git、Python 3.8+。不需要网络。
# 平台：FreeBSD/Linux/macOS 直接跑；Windows 用 Git Bash 的 sh 跑
#       （就是本项目验收机 0.88 与 Windows worker 的两种口径）。

set -u

# Git Bash 里直接 `sh smoke.sh` 时 PATH 往往只有 Windows 那份，拿不到
# mkdir/rm/mktemp。补上 /usr/bin 就够；FreeBSD 上这两个目录本来就在 PATH 里，
# 重复一次无副作用。
PATH="/usr/bin:/bin:$PATH"

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || {
    echo "smoke: 定位不到脚本所在目录" >&2
    exit 2
}

TID="T-9001"        # 固定 id：万一临时目录没删干净，一眼就知道是冒烟卡
KEEP=${KEEP:-0}
FAIL=0
TMP=""
last_out=""

say() { printf '%s\n' "$*"; }
ok()  { printf '  ok   %s\n' "$*"; }
bad() { printf '  FAIL %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }

# 子串判断用 shell 的 case，不走 grep —— Windows worker 上 grep 不在 PATH。
has() {
    case "$1" in
        *"$2"*) return 0 ;;
    esac
    return 1
}

# 把 MSYS 风格路径（/tmp/x）转成原生 Windows 形式（C:/Users/.../x），否则原生
# python.exe 会把它当成当前盘符下的 \tmp\x。非 Windows 原样返回。
native() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m -- "$1"
    else
        printf '%s\n' "$1"
    fi
}

cleanup() {
    [ -n "$TMP" ] || return 0
    if [ "$KEEP" = "1" ] || [ "$FAIL" -gt 0 ]; then
        say ""
        say "临时目录保留：$TMP"
        say "（确认无用后 rm -rf 掉即可；下次跑会另开一个新的）"
    else
        rm -rf "$TMP" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# --------------------------------------------------------------------------- #
say "== 0/4 环境 =="
if [ ! -f "$DIR/hive.py" ]; then
    say "smoke: $DIR 下没有 hive.py —— smoke.sh 必须放在 hive 仓库根目录" >&2
    exit 2
fi
DIR_N=$(native "$DIR")

if ! command -v git >/dev/null 2>&1; then
    say "smoke: 找不到 git" >&2
    exit 2
fi

# 优先 python3；Windows 上 python3 常是应用商店的占位 stub（能执行但报错），
# 所以拿「版本 >= 3.8」当判据，而不是拿命令存在当判据。
PY=""
for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
            >/dev/null 2>&1; then
        PY=$c
        break
    fi
done
if [ -z "$PY" ]; then
    say "smoke: 找不到 Python 3.8+（hive.py 需要）" >&2
    exit 2
fi
ok "python：$PY（$("$PY" -V 2>&1)）"
ok "git：$(git --version 2>&1)"

TMP=$(mktemp -d 2>/dev/null) || TMP="${TMPDIR:-/tmp}/hive-smoke-$$"
mkdir -p "$TMP" || {
    say "smoke: 建不了临时目录 $TMP" >&2
    exit 2
}
TMP=$(native "$TMP")
BARE="$TMP/hive.git"        # 临时远端：裸仓
HUB="$TMP/coord"            # 临时协调仓工作区
mkdir -p "$TMP/state" "$TMP/cluster" "$TMP/leases" || exit 2

# 本机状态全部关进临时目录：否则会写到 ~/.hive（真实租约留底、集群配置、
# store 路径），冒烟就可能反过来影响这台机器上的真活儿。
HIVE_STATE="$TMP/state"
HIVE_CLUSTER_STATE="$TMP/cluster"
HIVE_LEASE_DIR="$TMP/leases"
HIVE_CLAIM_BACKEND=git          # 明确走 git 后端，别被本机 store 模式带跑
HIVE_CLUSTER_AUTOSTART=0        # 别在冒烟时拉起后台集群
HIVE_DASH_AUTOSTART=0           # 也别顺手拉一个看板进程
PYTHONIOENCODING=utf-8          # Windows 上 stdout 默认 cp936，中文日志会花
PYTHONUTF8=1
export HIVE_STATE HIVE_CLUSTER_STATE HIVE_LEASE_DIR HIVE_CLAIM_BACKEND \
       HIVE_CLUSTER_AUTOSTART HIVE_DASH_AUTOSTART PYTHONIOENCODING PYTHONUTF8

# hive 命令统一出口：输出合并到 last_out，失败时原样打出来当证据。
hive_run() {
    last_out=$("$PY" "$DIR_N/hive.py" --dir "$HUB" "$@" 2>&1)
}

# --------------------------------------------------------------------------- #
say "== 1/4 临时裸仓 =="
if ! git init --bare -q -b main "$BARE" 2>/dev/null; then
    # 老 git 没有 `init -b`，退回到先 init 再改 HEAD
    git init --bare -q "$BARE" 2>/dev/null &&
        git -C "$BARE" symbolic-ref HEAD refs/heads/main 2>/dev/null || {
        bad "git init --bare 失败：$BARE"
        say "smoke: 收工（后面的步骤都依赖这个裸仓）"
        exit 1
    }
fi
ok "裸仓：$BARE"

if hive_run init --remote "$BARE"; then
    ok "hive init --remote（临时仓，不动真实队列）"
else
    bad "hive init 失败"
    say "$last_out"
    say "smoke: 收工"
    exit 1
fi

# --------------------------------------------------------------------------- #
say "== 2/4 hive doctor 无 ERR =="
hive_run doctor
if [ $? -eq 0 ] && has "$last_out" "[ OK ]" && ! has "$last_out" "[ERR"; then
    ok "doctor 通过，输出里没有 [ERR ]"
else
    bad "doctor 不干净（要求：exit 0、有 [ OK ]、没有 [ERR ）"
    say "$last_out"
fi

# --------------------------------------------------------------------------- #
say "== 3/4 建卡 + task list 看得到 =="
if hive_run task new --id "$TID" --title "smoke: temp card, deleted on exit" \
        --repo hive --priority P2 \
        --body "由 smoke.sh 在临时协调仓里创建，冒烟结束即删。"; then
    ok "task new $TID"
else
    bad "task new 失败"
    say "$last_out"
fi

hive_run task list
if [ $? -eq 0 ] && has "$last_out" "$TID"; then
    ok "task list 里看得到 $TID"
else
    bad "task list 里看不到 $TID"
    say "$last_out"
fi

# --------------------------------------------------------------------------- #
say "== 4/4 删卡 + task list 看不到 =="
if git -C "$HUB" rm -q "tasks/$TID.md" >/dev/null 2>&1 &&
   git -C "$HUB" -c user.name=hive-smoke -c user.email=smoke@localhost \
       commit -q -m "smoke: drop $TID" >/dev/null 2>&1 &&
   git -C "$HUB" push -q origin main >/dev/null 2>&1; then
    ok "删卡并推回临时裸仓"
else
    bad "删卡失败（git rm / commit / push 有一环没成）"
fi

hive_run task list
if [ $? -eq 0 ] && ! has "$last_out" "$TID"; then
    ok "task list 里已看不到 $TID"
else
    bad "删卡后 task list 仍能看到 $TID"
    say "$last_out"
fi

# --------------------------------------------------------------------------- #
say ""
if [ "$FAIL" -gt 0 ]; then
    say "smoke: ✗ $FAIL 项未通过"
    exit 1
fi
say "smoke: ✓ 全部通过（4/4）"
