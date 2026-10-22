#!/bin/sh
# vm-watch.sh —— 在 fb88 上看着 VirtualBox VM，死了就拉起来。
#
# 用法: sh vm-watch.sh [vm名] [轮询秒数]
#   sh vm-watch.sh win10 60
#
# 注意: 这个脚本只在「VM 已经从运行态掉下去」时重启；连续 3 次启动失败就退出，
#       避免 VM 一启动就崩导致无限重启循环。日志在 ~/.hive/vm-watch-<vm>.log。

VM="${1:-win10}"
INTERVAL="${2:-60}"
LOG="${HOME}/.hive/vm-watch-${VM}.log"
FAILS=0

mkdir -p "$(dirname "$LOG")"

while :; do
    STATE=$(VBoxManage showvminfo "$VM" --machinereadable 2>/dev/null \
            | sed -n 's/^VMState="\(.*\)"/\1/p')
    case "$STATE" in
        running|starting|restoring|saving)
            FAILS=0
            ;;
        "")
            echo "$(date '+%F %T') 读不到 $VM 状态" >>"$LOG"
            ;;
        *)
            echo "$(date '+%F %T') $VM 状态=$STATE，尝试启动" >>"$LOG"
            if VBoxManage startvm "$VM" --type headless >>"$LOG" 2>&1; then
                FAILS=0
            else
                FAILS=$((FAILS + 1))
                echo "$(date '+%F %T') 启动失败（连续 $FAILS 次）" >>"$LOG"
                if [ "$FAILS" -ge 3 ]; then
                    echo "$(date '+%F %T') 连续 3 次失败，停止看门狗，等人处理" >>"$LOG"
                    exit 1
                fi
            fi
            ;;
    esac
    sleep "$INTERVAL"
done
