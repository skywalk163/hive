# -*- coding: utf-8 -*-
"""后台 verify 的 Python 解释器解析（T-0498 后台 verify 环境假红回归）。

背景：后台 detached job（MCP 异步触发）继承的是 hive 服务进程的环境，
PATH 里没有交互 shell 的 WorkBuddy bin 目录；verify 命令里的裸 `python`
在 cmd.exe 下可能解析到 WindowsApps 存根，而同一命令在交互 shell 里全绿。
修法：Windows 上把作为「可执行解释器」的裸 python/python3 token 换成启动
hive 的 sys.executable 全路径；非 Windows 一个字节都不动。

本文件锁住四件事：
  1. 裸 python/python3（含命令中间位置、&& 之后）都换；
  2. 环境变量前缀（PYTHONPATH= / LIGHT_MERGE= / set X=...&&）不影响替换；
  3. 已是路径形态（C:\\Python314\\python.exe / reference/python/minilog.py）不误伤；
  4. 非 Windows 完全原样（FreeBSD/Linux 口径不变）。
"""
from __future__ import annotations

import os

import pytest

from hive import verify_env, verify_python_exe


EXE = r"C:\WorkBuddy\bin\python.exe"


def test_bare_python_replaced():
    assert verify_python_exe("python -m pytest -q tests/a.py", EXE, is_nt=True) == \
        f'"{EXE}" -m pytest -q tests/a.py'


def test_bare_python3_replaced():
    assert verify_python_exe("python3 -m pytest -q tests/a.py", EXE, is_nt=True) == \
        f'"{EXE}" -m pytest -q tests/a.py'


def test_env_prefix_kept_and_python_replaced():
    assert verify_python_exe(
        "PYTHONPATH=src python3 -m pytest -q tests/a.py", EXE, is_nt=True) == \
        f'PYTHONPATH=src "{EXE}" -m pytest -q tests/a.py'
    assert verify_python_exe(
        "LIGHT_MERGE=/x python -m pytest -q tests/b.py", EXE, is_nt=True) == \
        f'LIGHT_MERGE=/x "{EXE}" -m pytest -q tests/b.py'


def test_python_after_and_chain_replaced():
    # 命令中间位置（&& 之后）的裸 python 也要换
    assert verify_python_exe(
        "test -f a.txt && python3 -m py_compile x.py", EXE, is_nt=True) == \
        f'test -f a.txt && "{EXE}" -m py_compile x.py'


def test_set_var_prefix_python_replaced():
    assert verify_python_exe(
        "set X=1&& python -m pytest -q t.py", EXE, is_nt=True) == \
        f'set X=1&& "{EXE}" -m pytest -q t.py'


def test_path_like_python_not_touched():
    # 绝对路径解释器：已带盘符/后缀，不动（避免重复包裹引号）
    assert verify_python_exe(
        r"C:\Python314\python.exe -m pytest t.py", EXE, is_nt=True) == \
        r"C:\Python314\python.exe -m pytest t.py"
    # 文件名里含 python 的路径：不动
    assert verify_python_exe(
        "pytest reference/python/minilog.py", EXE, is_nt=True) == \
        "pytest reference/python/minilog.py"
    # 含 python 的参数/标志：不动
    assert verify_python_exe(
        "python -m pytest -k python", EXE, is_nt=True) == \
        f'"{EXE}" -m pytest -k python'


def test_no_python_unchanged():
    for cmd in ("git grep -L x -- README.md", "bash smoke.sh",
                "node --check webui/app.js", "test -f x && echo ok"):
        assert verify_python_exe(cmd, EXE, is_nt=True) == cmd


def test_non_windows_untouched():
    for cmd in ("python -m pytest -q t.py", "python3 -m pytest x",
                "PYTHONPATH=src python3 -m pytest t.py"):
        assert verify_python_exe(cmd, EXE, is_nt=False) == cmd


def test_verify_env_windows_sets_utf8():
    env = verify_env(is_nt=True)
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_verify_env_non_windows_empty():
    assert verify_env(is_nt=False) == {}
