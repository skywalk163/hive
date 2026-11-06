#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-exec — hive exec worker 的通用 AI CLI 适配器。

为什么需要它
------------
hive 的 exec 契约只有四条：cwd 是 worktree、prompt 走 stdin、回答走 stdout、
退出码非 0 算失败。但每台机器上的 AI 入口长得都不一样：

    Windows + WorkBuddy   codebuddy -p "..." -y
    Windows + Trae        traecli  -p "..." -y --allowed-tool ...
    Linux/FreeBSD         scripts/dsh-headless --profile headless -

如果为每台机器手写一条 `command`，配置会散成一地，换台机器就得重来一次。
这个适配器把差异收在一处：`agents/*.json` 里统一写

    "command": "python <hive>/scripts/agent-exec.py"

它自己探测本机有哪个 CLI、用合适的参数把任务喂进去。

怎么喂 prompt（关键设计）
------------------------
**不把 prompt 拼进命令行**。Windows 的 cmd 命令行上限 8191 字符，一张正常任务卡
（含验收命令、上下文）很容易超；而且中文要过 cp936 编码，引号嵌套也容易出事。

hive 已经把渲染好的任务写到 `<worktree>/.hive/task.md`（环境变量 HIVE_PROMPT 就是它），
所以这里只给 CLI 一句固定的短指令，让它自己去读文件。既绕开长度限制，
也保证 AI 拿到的是和卡面完全一致的内容。

用法
----
    # 正常执行（hive worker 就是这样调）
    agent-exec.py                 # prompt 从 stdin 读（读到后丢弃，只用于兜底写 task.md）

    # 自检：这台机器能走哪条路
    agent-exec.py --probe

环境变量
--------
    HIVE_AGENT_CLI   强制指定 CLI：codebuddy / traecli / dsh / <绝对路径>
    HIVE_AGENT_ARGS  追加参数（空格分隔），例如 "--model gpt-5"
    HIVE_PROMPT      任务卡路径（hive 自动注入）
    HIVE_AGENT_MODEL 可选，传给 --model
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 给 AI 的固定短指令。故意写得短、硬、无歧义：
# - 明确读哪个文件（不靠猜）
# - 明确边界（只改 task.md 允许的路径）
# - 明确禁止 git push（统一由 worker 提交，见 docs/主控与分发.md）
# - 明确收尾动作（写 summary，worker 会读它当结果摘要）
INSTRUCTION = (
    "你是 hive 协同开发集群中的一个 worker。当前目录是一张任务卡的独立 git worktree。\n"
    "请先完整读取 .hive/task.md，然后严格按它完成任务。\n"
    "硬约束：\n"
    "1. 只修改 task.md 中「允许修改」列出的路径；没列出就做最小改动。\n"
    "2. 绝对不要执行 git commit / git push / git checkout，提交由 worker 负责。\n"
    "3. 不要修改 .hive/ 目录下已有文件（task.md、state.json 等）。\n"
    "4. 完成后把改动摘要写进 .hive/summary.md：做了什么、改了哪些文件、怎么验证的。\n"
    "5. 若无法完成，也要写 .hive/summary.md 说明卡在哪，不要静默退出。\n"
    "现在开始，不要反问，不要等确认。"
)


def log(msg: str) -> None:
    print(f"[agent-exec] {msg}", file=sys.stderr, flush=True)


def find_cli(explicit: str | None) -> tuple[str, list[str], str] | None:
    """返回 (可执行文件绝对路径, 基础参数, 名字)。探测不到返回 None。"""
    if explicit:
        explicit = explicit.strip()
        if os.path.sep in explicit or "/" in explicit:
            p = Path(explicit)
            if p.exists():
                name = p.name.lower()
                return str(p), _base_args(_kind_of(name)), _kind_of(name)
            log(f"HIVE_AGENT_CLI 指定的路径不存在：{explicit}")
            return None
        # 允许只写种类名，落到下面的表
        forced = explicit.lower()
    else:
        forced = ""

    # 顺序有讲究：codebuddy 的 headless 最成熟（官方文档完整、-y 语义明确），
    # 其次 traecli（非交互模式有 -p，但企业版限定的套餐要注意），
    # 最后 dsh（本项目自研，只在有能力跑的机器上存在）。
    candidates = ["codebuddy", "cbc", "traecli", "dsh-exec", "dsh-headless", "dsh"]
    if forced:
        # 显式指定就**只**认它。指定了却回落到别的 CLI，等于让「我以为在跑 A，
        # 其实是 B」这种事故有个温床 —— 宁可明确失败。
        candidates = [forced]

    for name in candidates:
        path = _which(name)
        if path:
            kind = _kind_of(name)
            return path, _base_args(kind), kind
    return None


def _which(name: str) -> str | None:
    """先查 PATH，再查脚本同目录。

    脚本同目录这一条是必需的：hive 自带的 dsh-exec / dsh-headless 就躺在这里，
    从来不进 PATH；只看 PATH 会得出「本机没有 CLI」的错误结论。
    """
    hit = shutil.which(name)
    if hit:
        return hit
    here = Path(__file__).resolve().parent
    for suffix in ("", ".exe", ".cmd", ".bat"):
        p = here / (name + suffix)
        if p.is_file():
            return str(p)
    return None


def _kind_of(name: str) -> str:
    n = name.lower()
    if n.endswith(".exe") or n.endswith(".cmd") or n.endswith(".bat"):
        n = n.rsplit(".", 1)[0]
    if "codebuddy" in n or n == "cbc":
        return "codebuddy"
    if "traecli" in n or n == "trae":
        return "traecli"
    if n.startswith("dsh-exec"):
        return "dsh-exec"
    if "dsh" in n:
        return "dsh"
    return n


def _base_args(kind: str) -> list[str]:
    if kind == "codebuddy":
        # -y / --dangerously-skip-permissions 在 -p 模式下是**必需**的，
        # 否则文件读写、命令执行全被权限检查拦掉（官方文档明写）。
        return ["-p", INSTRUCTION, "-y", "--output-format", "text"]
    if kind == "traecli":
        # --allowed-tool 预先授权，避免 CI 里卡在确认环节；不加 -w，
        # 因为 worker 已经把我们放在隔离 worktree 里了。
        return ["-p", INSTRUCTION, "-y",
                "--allowed-tool", "Bash,Edit,MultiEdit,Write,Read",
                "--output-format", "text"]
    if kind == "dsh-exec":
        # dsh-exec 自己就是「prompt 走 stdin」的完整包装（含 chroot/node 处理），
        # 不接受额外参数。
        return []
    if kind == "dsh":
        return ["--profile", "headless", "-"]
    return ["-p", INSTRUCTION]


def run(cli: tuple[str, list[str], str], stdin_text: str) -> int:
    path, args, kind = cli

    extra = os.environ.get("HIVE_AGENT_ARGS", "").strip()
    if extra:
        args = args + extra.split()
    model = os.environ.get("HIVE_AGENT_MODEL", "").strip()
    if model and kind in ("codebuddy", "traecli"):
        args += ["--model", model]

    # .cmd/.bat 在 Windows 上必须经 cmd.exe 启动，直接 Popen 会 PermissionError
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", path] + args
    else:
        argv = [path] + args

    env = os.environ.copy()
    # 中文全链路：CLI 本身就是 node/python 写的，强制 UTF-8 才不会在 cp936 控制台下花屏，
    # 也不会让「已写 summary」这类中文输出变成乱码再污染 result。
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("LANG", "C.UTF-8")

    log(f"使用 {kind}：{path}")

    stdin_data = stdin_text if kind in ("dsh", "dsh-exec") else None

    p = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    out, _ = p.communicate(stdin_data)
    if out:
        sys.stdout.write(out)
        sys.stdout.flush()

    code = p.returncode or 0
    if code != 0:
        log(f"{kind} 退出码 {code}")
    return code


def probe() -> int:
    """自检：报告这台机器能走哪条路。给 join-agent 装完机时用。"""
    print("== agent-exec 自检 ==")
    print(f"平台: {sys.platform}  Python: {sys.version.split()[0]}")
    print(f"cwd : {os.getcwd()}")
    print(f"HIVE_PROMPT: {os.environ.get('HIVE_PROMPT') or '（未设置，正常；worker 里才有）'}")
    print()
    found = None
    for name in ("codebuddy", "cbc", "traecli", "dsh-exec", "dsh-headless", "dsh"):
        path = _which(name)
        print(f"  {name:<12} {'✓ ' + path if path else '· 未找到'}")
        if path and found is None:
            found = (path, _base_args(_kind_of(name)), _kind_of(name))
    print()
    if found:
        print(f"结论：可用，将使用 {found[2]}（{found[0]}）")
        print(f"  agents 配置里的 command 写：python {Path(__file__).resolve()}")
        return 0

    print("结论：**没有找到任何可用的 AI CLI，这台机器只能走 manual 模式**")
    print()
    print("补装任意一个即可：")
    print("  WorkBuddy/CodeBuddy : 装好后确认 `codebuddy -V` 可用")
    print("  TraeCode CLI        : "
          "powershell -c \"irm https://trae.cn/trae-cli/install.ps1 | iex\"")
    print("                        （macOS/Linux: "
          "sh -c \"$(curl -L https://trae.cn/trae-cli/install.sh)\"）")
    print("  dsh                 : 见 scripts/dsh-exec")
    return 1


def main() -> int:
    if "--probe" in sys.argv:
        return probe()

    # hive 会把渲染好的 prompt 写进 stdin。这里读掉它有两个用处：
    # 1) 避免上游 write 到已关闭的半截管道；
    # 2) HIVE_PROMPT 指向的文件万一不存在（老版本 worker / 手工调试），
    #    用它兜底落盘成 .hive/task.md，AI 照样有东西可读。
    try:
        stdin_text = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        stdin_text = ""

    task_path = Path(os.environ.get("HIVE_PROMPT") or (Path.cwd() / ".hive" / "task.md"))
    if stdin_text.strip() and not (task_path.exists() and task_path.stat().st_size > 0):
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(stdin_text, encoding="utf-8")
        log(f"task.md 缺失，已用 stdin 兜底写入 {task_path}")

    if not task_path.exists() or task_path.stat().st_size == 0:
        log(f"读不到任务卡：{task_path}")
        log("既没有 HIVE_PROMPT 指向的文件，stdin 也是空的 —— 这张卡没有内容可做。")
        return 2

    cli = find_cli(os.environ.get("HIVE_AGENT_CLI"))
    if cli is None:
        log("本机没有可用的 AI CLI，无法执行这张卡。")
        log("跑 `agent-exec.py --probe` 看该装什么。")
        return 127

    # codebuddy / traecli 走「短指令 + 让 AI 自己读 .hive/task.md」，不碰 stdin；
    # dsh 类是从 stdin 整段吃 prompt 的，必须把卡面内容喂进去 —— 否则它会报
    # 「a task is required」，看起来像没派任务，实际是没喂进去。
    # 优先用 worker 传进来的 stdin，它为空（手工调试、老版本 worker）时退回读 task.md。
    feed = stdin_text
    if cli[2] in ("dsh", "dsh-exec") and not feed.strip():
        feed = task_path.read_text(encoding="utf-8", errors="replace")
        log(f"stdin 为空，改用 {task_path} 的内容作为任务")

    return run(cli, feed)


if __name__ == "__main__":
    sys.exit(main())
