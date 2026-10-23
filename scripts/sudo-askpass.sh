#!/bin/sh
# sudo 的 askpass：把 ~/dswork/.env 里的 SUDO_PASS 喂给 `sudo -A`。
#
# 为什么需要：dsh-exec 用 `sudo chroot /compat/ubuntu ...` 把整条命令挪进 linuxulator
# （FreeBSD 原生 node 起不来 dsh，见 dsh-exec 顶部注释）。交互终端里 sudo 直接问密码、
# 或者票据缓存还没过期时都没问题；但 worker 是 setsid 起的常驻进程，没有 TTY，
# 缓存一过就报「sudo: a terminal is required to read the password」，任务直接失败。
#
# `sudo -S`（从 stdin 读密码）不能用：dsh-exec 的 stdin 是给 agent 的任务提示词，
# 拿去读密码就把提示词吃掉了。所以走 SUDO_ASKPASS —— sudo 从 askpass 的 stdout 取密码，
# 真正的 stdin 原样留给命令。
#
# 用法：SUDO_ASKPASS=本脚本 sudo -A <命令>
set -u

ENV_FILE="${HIVE_ENV:-$HOME/dswork/.env}"
[ -f "$ENV_FILE" ] || exit 1

grep -E '^SUDO_PASS' "$ENV_FILE" | head -1 | cut -d= -f2- \
    | tr -d '\r' | sed 's/^"//;s/"$//'
