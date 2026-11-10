#!/bin/sh
# hive-cluster-run.sh -- 生产集群（hived）的启动包装，由 hive_cluster rc 脚本调用。
#
# 为什么要有这一层：daemon(8) 通过 rc.subr 的 su -m 降权时会继承环境，
# 但 cluster 依赖的 HOME / PATH 必须在受控范围内，否则会读到 root 的 HOME、
# 或者用错 python。把参数与环境集中在这里，rc 脚本只负责托管与重启策略。
#
# 所有参数均可由 rc.conf 通过环境变量覆盖（rc.subr 会把 hive_cluster_* 注入）。

set -eu

CHDIR="${hive_cluster_chdir:-/home/skywalk/dswork/hive}"
STATE="${hive_cluster_state:-$HOME/.hive/cluster}"
PORT="${hive_cluster_port:-8788}"
DISC_PORT="${hive_cluster_discovery_port:-8789}"
NAME="${hive_cluster_name:-$(hostname -s)}"
MCP_PORT="${hive_cluster_mcp_port:-8790}"
EXTRA="${hive_cluster_extra_args:-}"

PYTHON="${hive_cluster_python:-/usr/local/bin/python3}"

mkdir -p "$STATE"
cd "$CHDIR"

# MCP / store 是可选项：默认都开，与历史行为一致
set -- cluster serve \
    --port "$PORT" --host 0.0.0.0 \
    --discovery-port "$DISC_PORT" \
    --state-dir "$STATE" \
    --name "$NAME"

case "${hive_cluster_mcp:-YES}" in
[Nn][Oo]) set -- "$@" --no-mcp ;;
*) set -- "$@" --mcp --mcp-port "$MCP_PORT" --mcp-bind 127.0.0.1 ;;
esac

case "${hive_cluster_store:-YES}" in
[Nn][Oo]) set -- "$@" --no-store ;;
*) set -- "$@" --store --claim-backend store ;;
esac

# EXTRA 故意不加引号：允许 rc.conf 里写 "--secret xyz" 这样的多参数。
# shellcheck disable=SC2086
exec "$PYTHON" "$CHDIR/hive.py" --dir "$CHDIR" "$@" $EXTRA
