#!/bin/sh
# 启动蜂巢看板：后台定时把 `hive status --json` 落到 dashboard/status.json，
# 前台运行 dashboard/serve.py。刷新失败时保留上一份状态，不覆盖。
#
#   ./scripts/serve-dashboard.sh            # 默认 127.0.0.1:8787，20 秒刷新
#   PORT=8799 INTERVAL=10 ./scripts/serve-dashboard.sh   # 8799 是测试/冒烟端口
#   HIVE_DIR=/path/to/hive PORT=8787 ./scripts/serve-dashboard.sh
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
HIVE="${HIVE_DIR:-$DIR}"
# 关键：下面 refresh() 会调 `hive status`，而 hive 入口有「按需自愈」——
# 不关掉的话，看板刷新自己会再拉起一个看板（端口打架、进程越滚越多）。
export HIVE_DASH_AUTOSTART=0
# 端口来源只有一个：PORT（服务脚本会传下来），其次 HIVE_DASHBOARD_PORT，最后 8787。
# 默认值与 serve.py / hive.py 一样来自 hive_ports.py；完整规划见
# docs/端口与服务规划.md。不要在这里写第二套默认值。
PORT="${PORT:-${HIVE_DASHBOARD_PORT:-8787}}"
# 绑定地址：默认 0.0.0.0（局域网可访问）。收回本机：HIVE_DASHBOARD_HOST=127.0.0.1
HOST="${HIVE_DASHBOARD_HOST:-0.0.0.0}"
# 20s 节拍保留（T-0354 只在「序号变了」时才真正重写快照，节拍本身不改）。
INTERVAL="${INTERVAL:-20}"
DASH="$DIR/dashboard"
STATUS="$DASH/status.json"
# 刷新循环心跳：每次 tick 都 touch，即使快照内容没变。serve.py 的
# X-Refresh-Mtime 读它，用来区分「集群安静、快照没变」和「刷新循环真的停摆了」。
# 名字带 .tmp 是刻意的：命中 .gitignore 里已有的 `dashboard/status.json.tmp*`，
# 不给协调仓留未跟踪垃圾（.gitignore 不在本卡 paths，不能去加规则）。
STAMP="$STATUS.tmp.refresh"
# CI_REPOS 留空 = 不拉 CI 状态（CI 环境下强烈建议留空：ci-check.sh 要访问内网
# Gitea，公网 runner 上连不通会白等一轮超时，把看板启动拖到超时）。
CI_REPOS="${CI_REPOS-skywalk/hive}"
CI_EVERY="${CI_EVERY:-6}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found" >&2
    exit 1
fi

mkdir -p "$DASH"

# --- THROTTLE FUNCTIONS BEGIN ---
# tests/test_dashboard_hlsa.py 会把这一段原样抠进临时沙箱单独跑（不启服务、不连网），
# 用它证明「序号没变 → status.json 一个字节不写、只更新心跳」。改这一段时保持
# 自包含：只用 $HIVE / $DASH / $STATUS / $STAMP 与 POSIX sh 工具。
# 一份状态 JSON 里的 LSDB 序号（sync_health.lsdb_seq）。
# 旧后端没有 sync_health → 输出空串，调用方据此退回「每拍都写」的旧行为。
# 用 python3 取值而不是 grep/awk：字段顺序、嵌套、转义都不该由 sed 猜。
status_seq() {
    [ -f "$1" ] || return 0
    python3 - "$1" <<'PY' 2>/dev/null
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8-sig") as fh:
        data = json.load(fh)
except Exception:                     # 半截文件 / 非 JSON：当作没有序号
    raise SystemExit(0)
health = data.get("sync_health") if isinstance(data, dict) else None
if isinstance(health, dict) and not health.get("degraded"):
    seq = health.get("lsdb_seq", health.get("seq"))
    if isinstance(seq, int) and seq >= 0:
        print(seq)
PY
}

# 节拍抖动：±20% jitter（H-LSA §4 第 6 道闸）。多机看板同时整点落盘会打满磁盘，
# 抖动把它们的写窗口错开；下限 1s，别让 INTERVAL=1 变成忙等。
jitter_seconds() {
    python3 - "$1" <<'PY' 2>/dev/null || printf '%s' "$1"
import random
import sys

try:
    base = float(sys.argv[1])
except (IndexError, ValueError):
    base = 20.0
spread = base * 0.2
print(max(1.0, base + random.uniform(-spread, spread)))
PY
}

refresh() {
    # tmp 名带 $$：多个实例（历史上真出现过）共用一个 status.json.tmp 时会互相踩——
    # A 写完、B 的失败分支把它 rm 掉、A 再去 mv 就报「stat 失败: 没有那个文件或目录」，
    # 这种错误会以每秒上百行的速度灌进日志（实测灌到 338MB）。各写各的 tmp 就没有竞争。
    tmp="$STATUS.tmp.$$"
    if ! python3 "$HIVE/hive.py" --dir "$HIVE" status --json >"$tmp" 2>"$DASH/status.err"; then
        rm -f "$tmp"
        return 0
    fi
    new_seq="$(status_seq "$tmp")"
    old_seq="$(status_seq "$STATUS")"
    if [ -n "$new_seq" ] && [ -s "$STATUS" ] && [ "$new_seq" = "$old_seq" ]; then
        # 落盘节流（T-0354 §3）：序号没变 = 卡面内容等价，不重写 status.json，
        # 只 touch 心跳。省掉 20s 一次的全量序列化与整份落盘。
        rm -f "$tmp"
    else
        # 序号变了 / 首次 / 旧后端没有序号：照旧整份替换（旧后端宁可多写也不漏变化）。
        mv -f "$tmp" "$STATUS" 2>/dev/null || rm -f "$tmp"
    fi
    # 心跳独立于快照内容：刷新循环活着就要留下证据（serve.py 用它判「停摆」）。
    : >"$STAMP" 2>/dev/null || true
}
# --- THROTTLE FUNCTIONS END ---

# 顺手刷新 CI 状态到 ci.json（需要 Gitea 凭证，失败保留旧文件）
refresh_ci() {
    if CI_OUT="$DASH/ci.json.tmp" CI_REPOS="$CI_REPOS" "$DIR/scripts/ci-check.sh" >/dev/null 2>&1; then
        mv "$DASH/ci.json.tmp" "$DASH/ci.json"
    else
        rm -f "$DASH/ci.json.tmp"
    fi
}

refresh || true
# 先让 serve.py 能起来，再补 CI 状态：ci-check.sh 最长要 15s（python 的 urlopen
# timeout），放在 serve.py 启动之前会把"服务就绪"拖过 dashboard-service.sh 的等待窗口。
[ -n "$CI_REPOS" ] && refresh_ci || true
TICKS=0
(
    while :; do
        sleep "$(jitter_seconds "$INTERVAL")"
        TICKS=$((TICKS + 1))
        refresh || true
        if [ -n "$CI_REPOS" ] && [ $((TICKS % CI_EVERY)) -eq 0 ]; then
            refresh_ci || true
        fi
    done
) &
LOOP=$!

cleanup() {
    kill "$LOOP" 2>/dev/null || true
    wait "$LOOP" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

cd "$DASH" || exit 1
python3 serve.py --status "$STATUS" --port "$PORT" --host "$HOST"
