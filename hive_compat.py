#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify 命令的跨平台垫片（Windows 上用）。

为什么需要
----------
任务卡里的 verify 常写成 POSIX 风格：`true`、`test -f x`、`! grep -q ...`。
在 FreeBSD/Linux 上这些是 shell 内建或 /usr/bin 里的命令；在 Windows 上
`cmd.exe` 没有 `true`，于是同一张卡在验收机上**必然**红灯——这不是业务失败，
是平台口径不一致。hive 在这里补一个最小映射，只覆盖高频、语义明确的写法，
未知写法原样交给 shell（绝不"猜一个成功"）。

覆盖范围（**整条命令就是这一种写法**才生效）：`true` / `false` / `:`、
`test -e/-f/-d/-r/-s/-x`（含 `! test ...`）、整数比较 `-eq/-ne/-lt/-gt/-le/-ge`，
以及**解释器前缀** `bash <script> [args]` / `python3 <args>`（T-0292）。
`-x` 是 T-0237 补的：Windows 上没有 POSIX 执行位，语义退化为「存在即可执行」。

`bash` / `python3` 为什么也要垫
------------------------------
任务卡的 verify 常直接写 `bash tests/run_pytest.sh ...`、`python3 -V`。Windows 的
`cmd.exe` 里既没有 `bash`（Git for Windows 只把 git.exe 放进了 PATH，bash.exe 还在
安装目录里）也没有 `python3.exe`（只有 python.exe），于是**产物合格也会 rc≠0**——
T-0291 / T-0261 / T-0237 都栽在这个签名上，主控已人工改判过。

这两类前缀不做「猜一个成功」的语义映射，而是**真的把子进程跑起来**：
`bash` 依次用 `HIVE_BASH`（覆盖，显式指定）→ `shutil.which("bash")` →
Git for Windows 常见安装位置；`python3` 直接映射到 `sys.executable`（正在跑 hive 的
解释器），不去 PATH 里碰运气。参数按 POSIX shlex 拆成 argv 后**直接 exec**，
不经过 cmd.exe 二次拼接引号（T-0287 的坑）；退出码原样透传，找不到解释器时
明确报错并返回非零（127），**绝不返回 0**。

用法（hive.py 内部的 run_verify 会调）::

    should_wrap(cmd)     -> bool   是否需要垫片
    rewrite(cmd)         -> str    `cmd` 或 `python hive_compat.py run "<cmd>"`
    run(cmd, cwd)         -> 退出码
    interpreter_command(cmd) -> ("bash"|"python3", rest) | None
    resolve_bash() / resolve_python3() -> 可执行文件绝对路径 | None

另外提供子进程「无窗」出口（T-0206）：Windows 上不给 `creationflags`，console
子进程（git 是 console 程序）在父进程没有控制台时会**自己新建一个可见控制台**
——看板每 30 秒刷新一次 `hive status --json` 就闪一次黑窗。::

    no_window_kwargs()   -> dict   Windows: {"creationflags": CREATE_NO_WINDOW}

单独调试::

    python hive_compat.py run "test -f docs/ok.md"
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys

MARK = "hive_compat.py run"

# `HIVE_BASH` 显式指定 bash.exe（PATH 里没有、又不在下面几个默认位置时装）。
BASH_ENV = "HIVE_BASH"

# Git for Windows 的常见安装位置。注意 PATH 里通常只有 git.exe，**没有** bash.exe，
# 所以 `shutil.which("bash")` 在没装 Git 的机器上会直接落空。
GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

MISSING_INTERPRETER_RC = 127      # POSIX 的「command not found」惯例，非零、可区分

# 逃生阀：怀疑「隐藏窗口」影响了某个子进程（例如它自己要弹窗）时，
# `HIVE_NO_WINDOW=0` 让全仓的 no_window_kwargs() 立刻退回空 dict，不用改代码。
NO_WINDOW_ENV = "HIVE_NO_WINDOW"


def no_window_kwargs() -> dict:
    """子进程无窗的统一出口：Windows 返回 `{"creationflags": CREATE_NO_WINDOW}`。

    为什么需要：Windows 上 console 程序（git / tasklist / python …）在**父进程
    没有控制台**时，CreateProcess 会为它新建一个可见控制台窗口——这就是看板每
    30 秒刷新一次就闪一下黑窗的来源。`CREATE_NO_WINDOW` 让子进程完全不分配控制台，
    输出照旧走管道，不影响任何 `capture_output` 调用方。

    与 `DETACHED_PROCESS` **互斥**（两者都管控制台，混用会起不来），所以后台
    daemon 拉起那几处（`_dashboard_spawn` / `hive_cluster._spawn_background`）
    刻意不走这个出口，见 tests/test_no_window.py 的白名单。

    非 Windows 一律返回 `{}`：POSIX 上没有这个概念，也绝不改变既有行为。
    `HIVE_NO_WINDOW=0`（或 false/no/off）时同样返回 `{}`。
    """
    if os.name != "nt":
        return {}
    if str(os.environ.get(NO_WINDOW_ENV, "1")).strip().lower() in (
            "0", "false", "no", "off"):
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not flag:  # pragma: no cover - 只有残缺的 subprocess 才会走到
        return {}
    return {"creationflags": flag}

# 只处理「整条命令就是这一种 POSIX 惯用法」的卡，避免误伤真实脚本。
_PLAIN = re.compile(r"^\s*(true|false|:)\s*$")
_NEG_PLAIN = re.compile(r"^\s*!\s*(true|false|:)\s*$")
_TEST_SUB = re.compile(r"^\s*(?:!\s*)?test\s+-(?P<flag>[efdrsx])\s+(?P<arg>.+?)\s*$")
_NEG_TEST_SUB = re.compile(r"^\s*!\s*test\s+-(?P<flag>[efdrsx])\s+(?P<arg>.+?)\s*$")
_COMPARE = re.compile(r'^\s*(?:test\s+)?"?(?P<a>[^"\s]+)"?\s*(?P<op>-eq|-ne|-lt|-gt|-le|-ge)\s*"?(?P<b>[^"\s]+)"?\s*$')

# 解释器前缀：整条命令必须以 `bash` / `python3` 开头，且**后面还有东西**。
# `bash` 单独一条不算（那是在找交互 shell，不是卡面 verify）。
_BASH_CMD = re.compile(r"^\s*bash\b(?:\s+(?P<rest>\S.*?))?\s*$")
_PY3_CMD = re.compile(r"^\s*python3\b(?:\s+(?P<rest>\S.*?))?\s*$")


def interpreter_command(cmd: str) -> tuple[str, str] | None:
    """认出 `bash <script> [args]` / `python3 <args>`，返回 `(kind, rest)`。

    只认「整条命令以该前缀开头且还有后续参数」，别的写法一律返回 None —— 由调用方
    原样交给 shell 或判成未知，**不猜成功**。
    """
    text = str(cmd or "").strip()
    if not text:
        return None
    for kind, rx in (("bash", _BASH_CMD), ("python3", _PY3_CMD)):
        m = rx.match(text)
        rest = (m.group("rest") or "").strip() if m else ""
        if rest:
            return kind, rest
    return None


def _is_executable_file(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)
    except OSError:
        return False


_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def bash_path_arg(token: str) -> str:
    """把（**已按 POSIX 语义加过引号**的）`C:\\a\\b` 换成 `C:/a/b`。

    为什么需要：Git Bash 是 MSYS 程序，反斜杠在它那儿既不是分隔符也是转义符，
    `bash "C:\\tmp\\x.sh"` 原样传会变成 `C:tmpx.sh`（实测：
    `/usr/bin/bash: C:tmpx.sh: No such file or directory`）。

    注意：未加引号的 `C:\\a\\b` 在**拆 argv 阶段**就被 POSIX shlex 吃掉反斜杠了
    （bash 自己也一样），所以卡面写 Windows 路径要加引号 —— 垫片不替人猜转义语义。

    只认「整个 argv 元素是一条盘符绝对路径」，不动 `-c "..."` 里的脚本内容
    （那种 token 以 `echo`/`import` 之类开头，不会匹配盘符前缀）。
    """
    if os.name == "nt" and _DRIVE_PATH.match(token):
        return token.replace("\\", "/")
    return token


def resolve_bash() -> str | None:
    """定位 bash：`HIVE_BASH` 覆盖 → PATH → Git for Windows 常见位置。

    返回 None 表示「确实没有」；调用方据此明确失败，绝不静默放行。
    """
    override = (os.environ.get(BASH_ENV) or "").strip()
    if override:
        # 显式指定就当权威：指定错了要立刻暴露，别偷偷退回别的 bash。
        return override if _is_executable_file(override) else None
    found = shutil.which("bash")
    if found:
        return found
    for cand in GIT_BASH_CANDIDATES:
        if _is_executable_file(cand):
            return cand
    return None


def resolve_python3() -> str | None:
    """`python3` 映射到当前跑 hive 的解释器 `sys.executable`，不去 PATH 里碰运气。"""
    exe = getattr(sys, "executable", "") or ""
    return exe if _is_executable_file(exe) else None


def missing_interpreter_message(kind: str) -> str:
    """找不到解释器时给人看的话：说清去哪儿配，而不是丢一句「不支持」。"""
    if kind != "bash":
        return ("hive_compat: 找不到 python3 —— 当前解释器 sys.executable "
                f"({sys.executable or '空'!r}) 不可执行；检查 Python 安装")
    override = (os.environ.get(BASH_ENV) or "").strip()
    if override:
        return (f"hive_compat: {BASH_ENV}={override} 指向的 bash 不存在或不可执行；"
                f"改成真实路径，或清掉 {BASH_ENV} 让它自动探测")
    return ("hive_compat: 找不到 bash —— 装了 Git for Windows 仍报这个，说明不在常见"
            "安装位置；请设 HIVE_BASH=<bash.exe 绝对路径>（常见："
            "C:\\Program Files\\Git\\bin\\bash.exe）")


def _run_argv(argv: list[str], cwd: str | None = None) -> int:
    """直接 exec argv（不经 cmd.exe），stdin 隔离、stdout/stderr 捕获后转发。"""
    try:
        proc = subprocess.run(
            argv, cwd=cwd,
            stdin=subprocess.DEVNULL,      # 与 hive.run 的口径一致：别继承父 stdin
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **no_window_kwargs())
    except OSError as exc:
        print(f"hive_compat: 无法执行 {argv[0]!r}：{exc}", file=sys.stderr)
        return MISSING_INTERPRETER_RC
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def run_interpreter(cmd: str, cwd: str | None = None) -> int:
    """把 `bash ...` / `python3 ...` 真正跑起来，退出码原样透传。"""
    hit = interpreter_command(cmd)
    if not hit:
        print(f"hive_compat: 不是 bash/python3 命令 {cmd!r}", file=sys.stderr)
        return 2
    kind, rest = hit
    try:
        args = shlex.split(rest, posix=True)
    except ValueError as exc:
        print(f"hive_compat: 参数引号不成对（{exc}）：{rest}", file=sys.stderr)
        return 2
    exe = resolve_bash() if kind == "bash" else resolve_python3()
    if not exe:
        print(missing_interpreter_message(kind), file=sys.stderr)
        return MISSING_INTERPRETER_RC
    if kind == "bash":
        # Git Bash 的解释器是 MSYS 程序：盘符路径要先转成 /c/... 风格才认
        args = [bash_path_arg(a) for a in args]
    return _run_argv([exe, *args], cwd=cwd)


def should_wrap(cmd: str) -> bool:
    """这条命令是否需要垫片（只在 Windows 上返回 True）。"""
    if os.name != "nt":
        return False
    text = str(cmd or "")
    # 已经包过了（同一张卡重复执行时别套娃）。脚本路径是带引号的绝对路径，
    # 不能用 MARK 整串去匹配，只认文件名。
    if "hive_compat.py" in text:
        return False
    return bool(_PLAIN.match(text) or _NEG_PLAIN.match(text)
                or _TEST_SUB.match(text) or _NEG_TEST_SUB.match(text)
                or _COMPARE.match(text)
                or interpreter_command(text) is not None)


def _split_arg(arg: str) -> str:
    try:
        parts = shlex.split(arg, posix=False)
    except ValueError:
        parts = [arg]
    return parts[0] if parts else arg


def rewrite(cmd: str) -> str:
    """把单条 POSIX 惯用法改写成 `python hive_compat.py run "<cmd>"`。"""
    text = str(cmd or "")
    quote = "'" if "'" not in text else '"'
    # sys.executable 必须与脚本路径一样带引号：用户名带空格的 Windows 机器上
    # （`C:\Users\PCbeta Tester\...\python.exe`），cmd.exe 会在空格处截断，
    # 把 `C:\Users\PCbeta` 当可执行程序去找，verify 于是无条件假红（T-0287）。
    return f'"{sys.executable}" "{_self_path()}" run {quote}{text}{quote}'


def _self_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "hive_compat.py")


def _unquote(text: str) -> str:
    """去掉一层包裹引号。

    Windows 的 cmd.exe 不会剥单引号：`run 'true'` 传到 Python 是 `'true'`
    （带引号），直接匹配正则永远失败。只剥一层，且要求首尾同一种引号。
    """
    t = (text or "").strip()
    for q in ("'", '"'):
        if len(t) >= 2 and t.startswith(q) and t.endswith(q):
            inner = t[1:-1]
            if q not in inner:
                return inner.strip()
    return t


def evaluate(cmd: str, cwd: str | None = None) -> int:
    """真正求值：返回子进程/判断的退出码。只支持 :func:`should_wrap` 认得的语法。

    `bash` / `python3` 前缀不是「语义映射」而是**真执行**（见 :func:`run_interpreter`），
    所以这里可能真的起子进程；`cwd` 缺省就继承当前工作目录。
    """
    text = _unquote(str(cmd or ""))
    if _PLAIN.match(text):
        return 0 if text.strip() in ("true", ":") else 1
    if _NEG_PLAIN.match(text):
        inner = text.strip().lstrip("!").strip()
        return 0 if inner == "false" else 1

    m = _NEG_TEST_SUB.match(text) or _TEST_SUB.match(text)
    if m:
        negated = bool(_NEG_TEST_SUB.match(text))
        flag = m.group("flag")
        arg = _split_arg(m.group("arg"))
        try:
            if flag == "f":
                ok = os.path.isfile(arg)
            elif flag == "e":
                ok = os.path.exists(arg)
            elif flag == "d":
                ok = os.path.isdir(arg)
            elif flag == "s":
                ok = os.path.getsize(arg) > 0 if os.path.exists(arg) else False
            elif flag == "r":
                ok = os.access(arg, os.R_OK)
            elif flag == "x":
                # T-0237：`test -x` 以前不在映射表里，于是整条命令原样丢给 cmd.exe，
                # 变成「'test' 不是内部或外部命令」——产物合格却被记 failed。
                # Windows 的 NTFS 没有 POSIX 执行位，CPython 的 os.access(X_OK) 在
                # Windows 上对文件等价于「存在」（目录也算），所以这里就是「存在即可执行」。
                # 想要「提交里记录的是 100755」这种更强口径得查 git 索引，不归垫片管。
                ok = os.access(arg, os.X_OK)
            else:  # pragma: no cover - 正则里只有这几个
                return 2
        except OSError:
            ok = False
        if negated:
            ok = not ok
        return 0 if ok else 1

    m = _COMPARE.match(text)
    if m:
        a, b = _split_arg(m.group("a")), _split_arg(m.group("b"))
        try:
            ai, bi = int(a), int(b)
        except ValueError:
            ai, bi = a, b
        op = m.group("op")
        table = {"-eq": ai == bi, "-ne": ai != bi, "-lt": ai < bi,
                 "-gt": ai > bi, "-le": ai <= bi, "-ge": ai >= bi}
        return 0 if table.get(op) else 1

    # `bash <script>` / `python3 <args>`：真执行，退出码原样透传
    if interpreter_command(text) is not None:
        return run_interpreter(text, cwd=cwd)

    # 没有映射到的写法：判为「未知」，让调用方回退到原命令
    return 2


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] != "run":
        print(f"用法: {MARK} \"<POSIX 命令>\"", file=sys.stderr)
        return 2
    cmd = " ".join(args[1:])
    rc = evaluate(cmd)
    # 解释器前缀自己会打印更具体的错（引号不成对等），别在这里补一句误导的「不支持」
    if rc == 2 and interpreter_command(cmd) is None:
        print(f"hive_compat: 不支持的写法 {cmd!r}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
