"""锁住 run() 的 stdin 隔离（T-0261）。

背景：run() 不传 stdin 时，子进程继承父进程 stdin，git 后代持有管道写端，
communicate() 等 EOF 会永久阻塞。这里用一个「读 stdin 且不主动退出」的子进程复现。
"""
import subprocess
import sys
import time

import pytest

import hive


def test_run_does_not_inherit_parent_stdin():
    """run() 必须把 stdin 给成 DEVNULL，子进程读 stdin 立即得到 EOF。"""
    # 子进程读一行 stdin；若 run() 隔离了 stdin，它会立刻得到 EOF 并退出。
    code = "import sys; sys.stdin.read(); print('eof-ok')"
    p = hive.run([sys.executable, "-c", code], timeout=10, check=False)
    assert "eof-ok" in (p.stdout or "")


@pytest.mark.xfail(
    reason="T-0261 只隔离了 stdin；stdout PIPE 写端仍被孙进程继承，"
           "2026-11-08 Linux 实测同样超时。待 T-0285 根治 fd 继承后改回严格断言",
    strict=False,
)
def test_run_does_not_hang_when_child_spawns_grandchild():
    """子进程再 fork 一个继续持有 stdout 的后代时，run() 不该永久阻塞。

    2026-11-08 主控 Linux 复验：`stdin=DEVNULL` 解决了 watchdog 的挂死（3/3 exit=0），
    但本例挂在 `capture_output=True` 的 stdout 管道上 —— 孙进程 sleep 30 握着写端，
    `communicate()` 等 EOF，10s 超时抛 HiveError。Windows / Linux 表现一致，
    **不是平台差异**。这条先 xfail 留证，别删；根治与恢复严格断言见 T-0285。
    """
    # 孙进程 sleep 后写日志；父进程立刻退出。若不隔离 fd，communicate 会等到孙进程结束。
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('parent-done')\n"
    )
    start = time.time()
    p = hive.run([sys.executable, "-c", code], timeout=10, check=False)
    elapsed = time.time() - start
    assert "parent-done" in (p.stdout or "")
    assert elapsed < 5, f"run() 阻塞了 {elapsed:.1f}s，stdin/fd 隔离失效"
