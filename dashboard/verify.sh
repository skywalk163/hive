#!/usr/bin/env bash
# hive-dashboard 一键复验脚本
# 用途：补齐 REPORT.md 第 3 节里因 shell 不可用而「未实跑」的验收 1/2/3，
#       并顺带覆盖静态资源 MIME、目录穿越防护、缺失数据文件 503 三项。
# 用法：bash verify.sh      （结束后自动清理服务进程与 __pycache__）
# 退出码：0 = 全通过；非 0 = 有失败项（失败项会打印 [FAIL]）

set -u
cd "$(dirname "$0")" || exit 1

# 自动挑一个可用的 python3：优先托管版本，退回 PATH 里的
PY=""
for cand in /root/.workbuddy/binaries/python/versions/3.13.12/bin/python3 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then echo "[FAIL] 找不到 python3，无法复验"; exit 1; fi
echo "python3 = $PY"

FAIL=0
pass() { echo "  [PASS] $1"; }
fail() { echo "  [FAIL] $1"; FAIL=1; }
cleanup() {
  pkill -f "serve.py.*--port 8787" 2>/dev/null
  pkill -f "serve.py.*--port 8799" 2>/dev/null
  rm -rf __pycache__
}
trap cleanup EXIT

echo
echo "=== 验收 1: py_compile serve.py ==="
if "$PY" -m py_compile serve.py 2>/tmp/hive_compile.log; then
  pass "语法编译通过"
else
  fail "编译失败，输出如下："; cat /tmp/hive_compile.log
fi

echo
echo "=== 验收 2: 服务能启动并打印 URL ==="
"$PY" serve.py --status sample-status.json --port 8787 >/tmp/hive_8787.log 2>&1 &
SRV_PID=$!
sleep 2
if kill -0 "$SRV_PID" 2>/dev/null; then
  pass "进程存活 (pid=$SRV_PID)，启动日志：$(cat /tmp/hive_8787.log)"
else
  fail "进程已退出，日志：$(cat /tmp/hive_8787.log)"
fi

echo
echo "=== 验收 3: /api/status 返回 6 条 ==="
N=$(curl -s --max-time 5 http://127.0.0.1:8787/api/status | "$PY" -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null)
if [ "$N" = "6" ]; then pass "元素个数 = 6"; else fail "元素个数 = '${N:-空}'，期望 6"; fi

echo
echo "=== 附加: 静态资源状态码与 MIME ==="
check() { # $1=路径 $2=期望状态 $3=期望 content-type 片段
  local out code ct
  out=$(curl -s -o /dev/null -w "%{http_code} %{content_type}" --max-time 5 "http://127.0.0.1:8787$1")
  code=${out%% *}; ct=${out#* }
  if [ "$code" = "$2" ] && printf '%s' "$ct" | grep -q "$3"; then
    pass "$1 -> $code $ct"
  else
    fail "$1 -> $code $ct（期望 $2 $3）"
  fi
}
check "/"           200 "text/html"
check "/app.js"     200 "javascript"
check "/style.css"  200 "text/css"

echo
echo "=== 附加: 目录穿越应被白名单挡掉（期望 404）==="
TRAV=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:8787/../serve.py")
if [ "$TRAV" = "404" ]; then pass "/../serve.py -> 404"; else fail "/../serve.py -> ${TRAV:-空}（期望 404）"; fi

echo
echo "=== 附加: status 文件缺失应返回 503 + JSON error ==="
"$PY" serve.py --status /tmp/hive_no_such_status.json --port 8799 >/tmp/hive_8799.log 2>&1 &
sleep 1.5
OUT=$(curl -s -w "|%{http_code}" --max-time 5 http://127.0.0.1:8799/api/status)
CODE=${OUT##*|}; BODY=${OUT%|*}
if [ "$CODE" = "503" ] && printf '%s' "$BODY" | grep -q '"error"'; then
  pass "503 + $BODY"
else
  fail "code=${CODE:-空} body=${BODY:-空}（期望 503 且含 \"error\"）"
fi

echo
if [ "$FAIL" -eq 0 ]; then echo "===== 全部通过 ====="; else echo "===== 存在失败项，见上 [FAIL] ====="; fi
exit "$FAIL"
