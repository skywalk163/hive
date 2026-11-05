"""计划面板的 jsdom 行为测试（可选依赖：node + jsdom）。

断言在 dashboard/dev/plans-ui-test.js 里；这里只负责把 node 跑起来，
没装 jsdom 就跳过 —— 和 shelf-ui 一样，它不该挡住纯 Python 的门禁。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"


def test_plans_ui_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 jsdom 前端测试")
    probe = subprocess.run([node, "-e", "require('jsdom')"],
                           capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        pytest.skip("没有 jsdom（npm i jsdom 后可选跑），跳过计划面板前端测试")

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([node, str(DASH / "dev" / "plans-ui-test.js")],
                          cwd=str(DASH), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          timeout=120)
    assert proc.returncode == 0, (
        "计划面板 UI 行为测试失败：\nstdout=%s\nstderr=%s"
        % (proc.stdout, proc.stderr))
    assert "ALL PLAN UI CHECKS PASSED" in proc.stdout
