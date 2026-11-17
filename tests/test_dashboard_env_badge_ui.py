"""环境性假红 / 人工判绿的看板行为测试（可选依赖：node + jsdom）。

断言写在 dashboard/dev/env-badge-ui-test.js；这里只负责把 node 跑起来。
没装 jsdom 就跳过（与 shelf-ui / plans-ui 一致，不挡纯 Python 门禁）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"


def test_env_badge_ui_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 jsdom 前端测试")
    probe = subprocess.run([node, "-e", "require('jsdom')"],
                           capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        pytest.skip("没有 jsdom（npm i jsdom 后可选跑），跳过徽标前端测试")

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([node, str(DASH / "dev" / "env-badge-ui-test.js")],
                          cwd=str(DASH), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          timeout=90)
    assert proc.returncode == 0, (
        "环境假红徽标 UI 行为测试失败：\nstdout=%s\nstderr=%s"
        % (proc.stdout, proc.stderr))
    assert "ALL ENV BADGE UI CHECKS PASSED" in proc.stdout
