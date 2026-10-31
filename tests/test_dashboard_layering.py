"""T-0207：详情抽屉打开时，顶栏右侧筛选区（仓库 / Owner / 优先级 / 搜索）不能被盖住。

层级关系（从 style.css 解析，不硬编码行号）：

    .topbar   sticky  z 应最高（抽屉打开时筛选仍可点）
    .modal    fixed   设置弹窗，应盖在顶栏之上（遮罩语义不被破坏）
    .drawer   fixed   详情抽屉
    .overlay  fixed   抽屉遮罩

修复（方案 A）要点：
1. `.topbar` z-index 提到 33（> drawer 31 / overlay 30）；
2. `.drawer` 的 `top` 从 0 改成 `var(--topbar-h)`，从顶栏下方滑入；
3. `.overlay` 保持 30；`.modal` 提到 34 以维持「弹窗遮罩盖住背景」的语义。

三层断言：
- **静态层（必跑）**：解析 style.css，断言 z(topbar) > z(drawer) > z(overlay)，
  且 `.drawer` 的 top 不是 0、而是 `var(--topbar-h)`；app.js 会实测顶栏高度回写变量。
  把 z 还原成 20/30/31 时这一层必须变红（反证）。
- **UI 层（有真浏览器才跑，否则 skip）**：真起看板、打开抽屉，算 `.drawer` 与
  `#repo-filter` 的 getBoundingClientRect 交集面积，要求为 0；jsdom 不做布局，
  给不出真实 rect，所以本层只在装了 headless 浏览器驱动时运行。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parents[1] / "dashboard"
STYLE_CSS = DASH / "style.css"
APP_JS = DASH / "app.js"
INDEX_HTML = DASH / "index.html"
REPRO_JS = DASH / "dev" / "layering-repro.js"


# --------------------------------------------------------------------------- #
# 极简 CSS 规则抽取（够用就行，不引入第三方 CSSOM）
# --------------------------------------------------------------------------- #

def _rule_block(css: str, selector: str) -> str | None:
    """取 `<selector> { ... }` 的花括号内容（按花括号配平，跨媒体查询也不怕）。

    selector 写 `.topbar` 时只匹配 `.topbar {`，不会误中 `.topbar-row {`。
    """
    pat = re.escape(selector) + r"\s*\{"
    m = re.search(pat, css)
    if not m:
        return None
    start = m.end() - 1  # 指向 '{'
    depth = 0
    i = start
    while i < len(css):
        ch = css[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:i]
        i += 1
    return None


def _zindex(block: str | None) -> int | None:
    if not block:
        return None
    m = re.search(r"z-index\s*:\s*(-?\d+)", block)
    return int(m.group(1)) if m else None


def _prop(block: str | None, name: str) -> str | None:
    if not block:
        return None
    m = re.search(name + r"\s*:\s*([^;]+);", block)
    return m.group(1).strip() if m else None


@pytest.fixture(scope="module")
def _css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def z(_css) -> dict:
    """三个关键层的 z-index（解析值，不硬编码）。"""
    return {
        "topbar": _zindex(_rule_block(_css, ".topbar")),
        "drawer": _zindex(_rule_block(_css, ".drawer")),
        "overlay": _zindex(_rule_block(_css, ".overlay")),
        "modal": _zindex(_rule_block(_css, ".modal")),
    }


# --------------------------------------------------------------------------- #
# 静态层：z 序 + drawer 让位（必跑，反证靠它）
# --------------------------------------------------------------------------- #

def test_z_order_topbar_above_drawer_above_overlay(z):
    """z(topbar) > z(drawer) > z(overlay)。

    还原成 20/30/31 时：topbar=20 < drawer=31，这条立刻红 —— 即反证。
    """
    assert z["topbar"] is not None, ".topbar 没找到 z-index"
    assert z["drawer"] is not None, ".drawer 没找到 z-index"
    assert z["overlay"] is not None, ".overlay 没找到 z-index"
    assert z["topbar"] > z["drawer"] > z["overlay"], (
        f"z 序错误：{z}。要求 topbar({z['topbar']}) > drawer({z['drawer']}) "
        f"> overlay({z['overlay']})"
    )


def test_drawer_top_is_offset_not_zero(_css):
    """`.drawer` 必须让位顶栏：top 不能是 0，而是 var(--topbar-h)。"""
    block = _rule_block(_css, ".drawer")
    top = _prop(block, "top")
    assert top is not None, ".drawer 没找到 top 声明"
    assert top != "0", f".drawer top 仍是 0，会压住顶栏：{top!r}"
    assert "var(--topbar-h)" in top, f".drawer top 没用 --topbar-h 让位：{top!r}"


def test_topbar_height_variable_defined(_css):
    """:root 里要有 --topbar-h 兜底变量。"""
    root = _rule_block(_css, ":root")
    assert root is not None, "找不到 :root"
    assert "--topbar-h" in root, ":root 没定义 --topbar-h"


def test_app_js_writes_topbar_height_variable():
    """app.js 要实测 .topbar 高度并回写 --topbar-h（顶栏会换行，固定 px 会错位）。"""
    src = APP_JS.read_text(encoding="utf-8")
    assert "--topbar-h" in src, "app.js 没引用 --topbar-h"
    assert "setProperty" in src and "offsetHeight" in src, (
        "app.js 没有用 offsetHeight 实测顶栏高度并 setProperty 回写"
    )


def test_modal_stays_above_topbar(z):
    """设置弹窗仍要高于顶栏，否则居中弹窗顶边会被 sticky 顶栏盖住。"""
    assert z["modal"] is not None
    assert z["modal"] > z["topbar"], (
        f"modal({z['modal']}) 必须 > topbar({z['topbar']})，否则设置弹窗被顶栏压住"
    )


# --------------------------------------------------------------------------- #
# UI 层：真起看板打开抽屉，抽屉与 #repo-filter 交集面积必须为 0
# --------------------------------------------------------------------------- #

def _headless_driver_available() -> bool:
    """有没有能跑真实布局的驱动：node+jsdom 不给真实 rect，只在显式开了
    HIVE_DASH_BROWSER=1 且装了 headless 浏览器驱动时才认为可跑。"""
    import os
    return os.environ.get("HIVE_DASH_BROWSER") == "1"


@pytest.mark.skipif(not _headless_driver_available(),
                    reason="无 headless 浏览器驱动（jsdom 不算布局）；"
                           "设 HIVE_DASH_BROWSER=1 并装好驱动后跑本层")
def test_drawer_does_not_overlap_repo_filter():
    """起看板 → 打开抽屉 → 算 .drawer 与 #repo-filter 交集面积，必须为 0。

    复用 dev/layering-repro.js（与 ui-test.js 同套 DOM 计算），它打印
    `LAYERING_RESULT=<json>`。这里解析并断言交集 <= 0，且点 #repo-filter
    命中的是 select 本体（不是 overlay/drawer）。
    """
    node = shutil.which("node")
    assert node, "需要 node 跑 dev/layering-repro.js"
    assert REPRO_JS.is_file(), f"缺少复现脚本 {REPRO_JS}"

    proc = subprocess.run(
        [node, str(REPRO_JS)], cwd=str(DASH), text=True,
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    line = next((ln for ln in proc.stdout.splitlines()
                 if ln.startswith("LAYERING_RESULT=")), None)
    assert line, f"复现脚本没输出结果：\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(line[len("LAYERING_RESULT="):])

    assert data["intersection_area"] <= 1.0, (
        f"抽屉与 #repo-filter 仍有交集 {data['intersection_area']}px²"
    )
    assert data.get("hit_is_select") is True, (
        f"点 #repo-filter 命中的不是 select 本体：{data.get('hit_tag')}"
    )
