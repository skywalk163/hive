#!/bin/sh
# patch-dsh-linux-bindings.sh — 给 dsh 的 FreeBSD distfile 树补 linux 原生绑定。
#
# 为什么需要（2026-10-23 实测）：
#   fb88 上唯一的 node 是 WorkBuddy 自带的 Linux 构建（linuxulator 下
#   process.platform=linux）。dsh 的 FreeBSD distfile 只带了 freebsd 变体：
#     - node-addon-require-builtin：只有纯 JS 壳，平台二进制走 optionalDependencies，
#       压根没有 freebsd 变体；linuxulator 下去找 -linux-x64-gnu，树上没有 → fatal。
#     - @deepseek-ai/node-addon-system：entry 目录里 linux-x64 变体只有文档，
#       二进制不在 → Cannot find module .../bin/glibc/system.node。
#   两个包在 npm 上都有（0.1.9 / 0.1.2），下载解压进去即可，不动 pnpm store。
#   打补丁后用 scripts/dsh-headless 跑 `--profile headless --help` 验证。
#
# 用法：
#   DSH_DIST=<dist目录> ./scripts/patch-dsh-linux-bindings.sh
#   （默认 dist 目录见下；可重复执行，覆盖式解压，幂等）
set -u

DSH_DIST="${DSH_DIST:-/home/skywalk/dsh-port-test/work/deepseek-harness-0.2.1-rc.2-freebsd-amd64}"

if [ ! -f "$DSH_DIST/lib/bin.js" ]; then
    echo "patch-dsh: $DSH_DIST/lib/bin.js 不存在，先解包 distfile 或用 DSH_DIST 指过去" >&2
    exit 1
fi

TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT
cd "$TMP" || exit 1

echo "patch-dsh: npm pack 两个 linux 绑定包 ..."
npm pack node-addon-require-builtin-linux-x64-gnu@0.1.9 >/dev/null 2>&1 || {
    echo "patch-dsh: npm pack node-addon-require-builtin-linux-x64-gnu 失败（npm 可达？）" >&2
    exit 1
}
npm pack @deepseek-ai/node-addon-system-linux-x64@0.1.2 >/dev/null 2>&1 || {
    echo "patch-dsh: npm pack @deepseek-ai/node-addon-system-linux-x64 失败" >&2
    exit 1
}

# 1) require-builtin：放 dist 顶层 node_modules（加载器从包目录向上走能找到）
DEST1="$DSH_DIST/node_modules/node-addon-require-builtin-linux-x64-gnu"
mkdir -p "$DEST1"
tar xzf node-addon-require-builtin-linux-x64-gnu-0.1.9.tgz -C "$DEST1" --strip-components=1

# 2) node-addon-system：entry 的 linux-x64 变体目录（包在、二进制不在）
ENTRY=$(find "$DSH_DIST/node_modules/.pnpm" -maxdepth 1 -type d \
    -name '@deepseek-ai+node-addon-system@file+native+system+packages+entry' 2>/dev/null | head -1)
if [ -z "$ENTRY" ]; then
    echo "patch-dsh: 找不到 node-addon-system 的 entry 目录（.pnpm 布局变了？）" >&2
    exit 1
fi
DEST2="$ENTRY/node_modules/@deepseek-ai/node-addon-system-linux-x64"
mkdir -p "$DEST2"
tar xzf deepseek-ai-node-addon-system-linux-x64-0.1.2.tgz -C "$DEST2" --strip-components=1

# 校验：两个二进制都要在
ok=1
[ -f "$DEST1/prebuilt/linux-x64-gnu-napi-v9.node" ] || { echo "patch-dsh: require-builtin 二进制缺失" >&2; ok=0; }
[ -f "$DEST2/bin/glibc/system.node" ] || { echo "patch-dsh: system.node 缺失" >&2; ok=0; }
[ "$ok" = 1 ] || exit 1

echo "patch-dsh: OK —— 两个 linux 绑定已就位："
echo "  $DEST1"
echo "  $DEST2"
echo "验证：  DSH_DIST=$DSH_DIST ./scripts/dsh-headless --profile headless --help"
