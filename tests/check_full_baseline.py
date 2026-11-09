#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量 pytest 基线守卫（T-0291）。

为什么要有它
------------
`tests/run_pytest.sh` 的裸全量判据**从来没有全绿过**：去掉权限噪声后仍有真失败
+ 一批环境 error（逐条归因见 T-0275 / T-0291 卡面）。把整条全量当作「必须 rc=0」
的 verify，结果是这条判据永远红灯，谁也不敢信它。

这里把「全量」拆成两半：
* **定向集合**（test_watchdog / test_dashboard_freshness / test_run_stdin）必须逐条绿，
  由卡自己的 verify 直接跑，不归本脚本管；
* **已知基线**（本脚本）：真跑完整套件，只判「有没有**新增**失败」。

语义（卡面 T-0291 硬性要求）
--------------------------
1. 用 `bash tests/run_pytest.sh` 跑完整套件（保留其中的 `</dev/null` 与硬超时），
   超时上限走 `HIVE_TEST_TIMEOUT`（默认 1500s）；
2. 解析 pytest 摘要，逐个比对 FAILED / ERROR 与 allowlist；
3. allowlist 里的存量失败**消失**不算红（修好了更好，只提示）；
4. 出现任何 allowlist 之外的 FAILED / ERROR → 打印明细并 `exit 1`；
5. 全通过（含仅剩存量）时打印 `baseline-ok new=0 failed=<n> errors=<n>` 并 `exit 0`。

allowlist 只能是**精确 nodeid** 或**精确签名**（`signature:` 前缀），不许目录级通配：
目录级白名单会把同目录里新长出来的失败一起吞掉，那就等于没有判据了。

签名怎么比对（这一点最容易做错）
--------------------------------
`pytest -q` 的 `short test summary info` 会把人名和消息**截断**（`- Assert...`），
拿它做签名匹配必然漏。所以签名一律去 `FAILURES` / `ERRORS` 段里按**用例块**比对：
块头给出用例名，块体里既有完整 traceback、也有 `tests/xxx.py:NN:` 源文件行。
块按「源文件 + 用例名」建索引，签名只对**该用例自己**生效；`signature:tests/x.py::...`
还能把签名再钉死在某个文件里，别的文件出现同一段字面量不算存量。

退出码：0 = 无新增失败（含仅剩存量）；1 = 有 allowlist 之外的失败（或套件没跑成 /
被硬超时杀掉）；2 = 环境跑不起来（没有 bash / 没有 run_pytest.sh，或 allowlist 坏了）。

用法
----
    python3 tests/check_full_baseline.py             # 按上面的语义跑
    python3 tests/check_full_baseline.py --print-unknown   # 只跑并打印没登记的项
    HIVE_TEST_TIMEOUT=1500 python3 tests/check_full_baseline.py
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "run_pytest.sh"
ALLOWLIST = ROOT / "tests" / "baseline_allowlist.txt"

DEFAULT_TIMEOUT = 1500
TIMEOUT_ENV = "HIVE_TEST_TIMEOUT"
# GNU timeout 杀掉子进程时的退出码（124/125 是 timeout 自己的，137/143 是被转发的）。
TIMEOUT_CODES = {124, 125, 137, 143}
# 兜底硬超时的额外宽限：run_pytest.sh 自己没有 timeout 命令时（TIMEOUT_BIN 缺失），
# 由本脚本的 subprocess 超时接管，避免全量挂死变成没有上限的 verify。
WATCHDOG_GRACE = 120

# `pytest -q` 摘要行：`FAILED tests/test_x.py::test_y - AssertionError: ...`
# 参数化 id 里可能有空格/中括号，所以 nodeid 只吃到第一个空白为止。
_SUMMARY = re.compile(r"^(FAILED|ERROR)\s+(?P<nodeid>\S+)(?:\s+-\s+(?P<msg>.*))?$")
# 结尾统计行：`2 failed, 921 passed, 6 skipped, 16 errors in 975.09s (0:16:15)`
# 尾部的 `(0:16:15)` 是可选的，所以不能写成 `$` 收尾。
_TALLY = re.compile(r"^[0-9]+[^\n]*\bin\s+[0-9.]+s\b")
_ITEM = re.compile(r"(\d+)\s+(failed|passed|error|errors|skipped|xfailed|xpassed|"
                   r"warning|warnings|deselected|rerun|reruns)")
# 块体里的源文件：`tests/test_result_head_guard.py:110: in __init__`
_SRC = re.compile(r"(?:^|\s)(tests/[A-Za-z0-9_.\-/]+\.py):(\d+):")
# 段头：pytest 用等号铺满整行（`====== ERRORS ======`），不能写成 `= X =`
_SECTION = re.compile(r"^=+\s+(FAILURES|ERRORS|short test summary info)\s+=+$")
_ERR_HDR = re.compile(r"^ERROR at (?:setup|teardown|call) of ")
# 块头允许出现的字符（用例名、参数化 id、`ERROR at setup of` 前缀）
_HEADER_CHARS = set(
    "_ abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "[]:.()-/\\'\"→|,=")


def _fail_env(msg: str) -> None:
    """环境跑不起来：打印并 exit 2（与「有新增失败」的 rc=1 区分开）。"""
    print(f"[baseline] {msg}", file=sys.stderr)
    raise SystemExit(2)


def _is_block_header(line: str) -> bool:
    """pytest 的用例块头长这样：`___ ERROR at setup of test_x ___`。

    要收紧：整行由下划线/空格/用例名组成、两端被连续下划线包着。traceback 里那条
    `_ _ _ _` 分隔线也是下划线，必须排除——否则它会被当成块头，把上一个用例的正文
    截断，签名就匹配不上了。
    """
    s = line.strip()
    if not s or not s.startswith("_") or not s.endswith("_"):
        return False
    if not set(s) <= _HEADER_CHARS:
        return False
    inner = s.strip("_").strip()
    if not inner:
        return False                       # 纯下划线：分隔线，不是块头
    return not set(inner) <= {"_", " "}    # `_ _ _` 这种同样是分隔线


def _header_name(line: str) -> str:
    """从块头里取出用例名（去掉两端下划线与 `ERROR at setup of` 前缀）。"""
    s = line.strip().strip("_").strip()
    s = _ERR_HDR.sub("", s)
    return s.strip().strip("_").strip()


def parse_blocks(text: str) -> dict[tuple[str, str], str]:
    """把 FAILURES/ERRORS 段解析成 {(源文件, 用例名): 块正文}。

    源文件从块正文里取（块头里没有路径）。取不到时键是 ("", 用例名)。
    没有块头的落单内容丢掉——宁可让签名匹配不上（判红），也不放宽。
    """
    blocks: dict[tuple[str, str], str] = {}
    cur: str | None = None
    buf: list[str] = []
    in_detail = False
    for line in text.splitlines():
        sec = _SECTION.match(line.strip())
        if sec:
            if cur is not None:
                body = "\n".join(buf)
                blocks[(_src_of(body), cur)] = body
                cur, buf = None, []
            in_detail = sec.group(1) in ("FAILURES", "ERRORS")
            continue
        if not in_detail:
            continue
        if _is_block_header(line):
            if cur is not None:
                body = "\n".join(buf)
                blocks[(_src_of(body), cur)] = body
            cur, buf = _header_name(line), []
            continue
        if cur is not None:
            buf.append(line)
    if cur is not None:
        body = "\n".join(buf)
        blocks[(_src_of(body), cur)] = body
    return blocks


def _src_of(block: str) -> str:
    """块正文里出现的**第一个**源文件（`tests/<file>.py`）。

    `E ` 开头的行是断言消息，不是源位置，跳过它免得把消息里的文件名当来源。
    """
    for line in block.splitlines():
        if line.startswith("E "):
            continue
        m = _SRC.search(line)
        if m:
            return m.group(1)
    return ""


def _src_and_name(nodeid: str) -> tuple[str, str]:
    """`tests/test_x.py::test_y[1]` -> (`tests/test_x.py`, `test_y[1]`)。"""
    if "::" in nodeid:
        src, rest = nodeid.split("::", 1)
        return src, rest
    return "", nodeid


def load_allowlist(path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """读 allowlist：返回 (精确 nodeid 列表, [(钉住的源文件, 签名正文)])。

    签名写法：`signature:<正文>` 或 `signature:<tests/x.py>::<正文>`（推荐后者）。
    `#` 开头与空行忽略；` #` 之后算行尾注释，不入判据。
    """
    nodes: list[str] = []
    sigs: list[tuple[str, str]] = []
    if not path.exists():
        _fail_env(f"allowlist 不存在：{path}")
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = line.split(" #", 1)[0].strip()
        if not body:
            continue
        if body.startswith("signature:"):
            sig = body[len("signature:"):].strip()
            if not sig:
                _fail_env(f"{path}:{lineno} signature: 后面是空的")
            if "::" in sig:
                sig_file, sig_text = sig.split("::", 1)
                if not sig_text.strip():
                    _fail_env(f"{path}:{lineno} 文件前缀之后没有签名正文")
                sigs.append((sig_file.strip(), sig_text))
            else:
                sigs.append(("", sig))
        else:
            nodes.append(body)
    return nodes, sigs


def parse_output(text: str) -> tuple[list[dict], str]:
    """从 pytest 输出里抽 (失败/错误列表, 统计行)。"""
    entries: list[dict] = []
    tally = ""
    for line in text.splitlines():
        m = _SUMMARY.match(line.strip())
        if m:
            entries.append({"kind": m.group(1), "nodeid": m.group("nodeid"),
                            "msg": (m.group("msg") or "").strip()})
            continue
        if _TALLY.match(line.strip()):
            tally = line.strip()
    return entries, tally


def counts_from_tally(tally: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for num, word in _ITEM.findall(tally or ""):
        key = {"error": "errors", "warning": "warnings",
               "rerun": "reruns"}.get(word, word)
        out[key] = int(num)
    return out


def classify(entries: list[dict], nodes: list[str],
             sigs: list[tuple[str, str]],
             blocks: dict[tuple[str, str], str]) -> tuple[list[dict], list[dict]]:
    """把失败分成 (已登记存量, 新增)。精确 nodeid 优先，其次是同用例块里的签名。"""
    allowed: list[dict] = []
    new: list[dict] = []
    for e in entries:
        nodeid = e["nodeid"]
        if nodeid in nodes:
            e["matched"] = "nodeid"
            allowed.append(e)
            continue
        src, name = _src_and_name(nodeid)
        body = blocks.get((src, name)) or blocks.get(("", name)) or ""
        hit = ""
        for sig_file, sig_text in sigs:
            if sig_file and sig_file != src:
                continue          # 签名钉在别的文件上，不适用于本例
            if body and sig_text in body:
                hit = f"{sig_file}::{sig_text}" if sig_file else sig_text
                break
        if hit:
            e["matched"] = "signature"
            e["sig"] = hit
            allowed.append(e)
            continue
        e["matched"] = ""
        new.append(e)
    return allowed, new


def run_suite(timeout: int) -> tuple[int, str, bool]:
    """跑完整套件，返回 (退出码, 输出文本, 是否被兜底硬超时杀掉)。

    用 `bash tests/run_pytest.sh`（保留 `</dev/null` 与硬超时），不在这里自己拼
    pytest 调用——入口只有一处，超时/隔离的语义才是同一份。再叠一层本进程的
    硬超时（timeout + 宽限），这样即使机器上没有 `timeout` 命令，也不会无上限。
    """
    if not RUNNER.exists():
        _fail_env(f"测试入口不存在：{RUNNER}")
    env = dict(os.environ)
    env[TIMEOUT_ENV] = str(timeout)
    try:
        p = subprocess.run(
            ["bash", str(RUNNER)], cwd=str(ROOT), env=env, text=True,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
            capture_output=True, timeout=timeout + WATCHDOG_GRACE,
        )
    except FileNotFoundError:
        _fail_env("找不到 bash：本判据要在 POSIX（FreeBSD/Linux）或装了 bash 的机器上跑；"
                  "Windows 上 hive 把 verify 交给 cmd.exe，请换验收机执行。")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return 124, out + err, True
    return p.returncode, (p.stdout or "") + (p.stderr or ""), False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--print-unknown", action="store_true",
                    help="只跑并打印没被 allowlist 登记的失败（不判退出码）")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT)),
                    help=f"全量超时秒数（默认读 {TIMEOUT_ENV}，{DEFAULT_TIMEOUT}）")
    args = ap.parse_args(argv)

    nodes, sigs = load_allowlist(ALLOWLIST)
    print(f"[baseline] 跑完整套件（超时 {args.timeout}s）…", flush=True)
    code, text, watchdog = run_suite(args.timeout)
    entries, tally = parse_output(text)
    blocks = parse_blocks(text)
    allowed, new = classify(entries, nodes, sigs, blocks)
    counts = counts_from_tally(tally)

    failed = counts.get("failed", sum(1 for e in entries if e["kind"] == "FAILED"))
    errors = counts.get("errors", sum(1 for e in entries if e["kind"] == "ERROR"))
    used_nodes = {e["nodeid"] for e in allowed if e["matched"] == "nodeid"}
    used_sigs = sorted({e["sig"] for e in allowed if e["matched"] == "signature"})

    print(f"[baseline] pytest rc={code} 统计: {tally or '（没解析到统计行）'}")
    print(f"[baseline] 失败/错误 {len(entries)} 条，命中 allowlist {len(allowed)} 条"
          f"（nodeid {len(used_nodes)} / 签名 {len(used_sigs)}），新增 {len(new)} 条")
    for sig in used_sigs:
        print(f"[baseline] 命中的签名: {sig}")

    # 存量失败消失不算红（修好了更好），只提示，方便日后删 allowlist 行。
    stale = [f"nodeid {n}" for n in nodes if n not in used_nodes]
    lost = [f"signature {f}::{s}" if f else f"signature {s}"
            for f, s in sigs
            if (f"{f}::{s}" if f else s) not in used_sigs]
    if stale or lost:
        print("[baseline] 以下存量项本次没出现（可能已修好，可从 allowlist 删除）：")
        for s in stale + lost:
            print(f"  - {s}")

    if args.print_unknown:
        for e in new:
            print(f"  UNKNOWN {e['kind']} {e['nodeid']} :: {e['msg']}")
        return 0

    # 硬超时 / 收集中断 / 内部错误 / 没收集到用例：一律判红，不看明细。
    if watchdog or code in TIMEOUT_CODES:
        print(f"[baseline] 套件被硬超时终止（rc={code}），判红。"
              f"（上限 {args.timeout}s，可用 {TIMEOUT_ENV} 调整）")
        return 1
    if code not in (0, 1):
        print(f"[baseline] 套件异常退出 rc={code}（非 0/1），判红。")
        print("\n".join(text.strip().splitlines()[-30:]))
        return 1
    if code != 0 and not entries:
        # 一条明细都没解析到却非零退出：别当成「绿」。
        print("[baseline] 套件非零退出但没解析到 FAILED/ERROR 明细，判红。")
        print("\n".join(text.strip().splitlines()[-30:]))
        return 1

    if new:
        print(f"[baseline] 新增失败 {len(new)} 条（allowlist 之外）：")
        for e in new:
            print(f"  {e['kind']} {e['nodeid']}")
            if e["msg"]:
                print(f"      {e['msg']}")
        return 1

    print(f"baseline-ok new=0 failed={failed} errors={errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
