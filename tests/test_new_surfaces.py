"""新增能力的静态守卫（T-0209）：基准脚本、init 复制、文档链接。

跑得很快（不起 worker、不联网），作用是防止「文档写了、脚本没跟上」或反过来。
端到端那部分在 tests/test_fence.py / test_pr.py / test_compat.py / test_dashboard_auth.py。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import World  # noqa: E402

PS1 = ROOT / "windows" / "verify-smoke.ps1"
DOC = ROOT / "docs" / "windows-baseline.md"


# --------------------------------------------------------------------------- #
# Windows 基准：脚本判据与文档一一对应
# --------------------------------------------------------------------------- #

def test_windows_smoke_script_exists_and_is_ps51_safe():
    assert PS1.is_file(), PS1
    raw = PS1.read_bytes()
    # .gitattributes 把 *.ps1 钉成 crlf；BOM 在 PS 5.1 上是中文安全的来源
    assert raw.startswith(b"\xef\xbb\xbf"), "PS 5.1 需要 UTF-8 BOM 才能正确解析中文"
    assert b"\r\n" in raw, "*.ps1 必须是 CRLF（见 .gitattributes）"
    text = raw.decode("utf-8-sig")
    # 已知的 PS 5.1 陷阱：$Args 自动变量、param 默认值里的 $PSScriptRoot
    assert "[Parameter(ValueFromRemainingArguments = $true)]$Args" not in text
    assert "$PSScriptRoot" in text and "param(" in text
    assert "param(\r\n    [string]$Repo = (Split-Path" not in text


@pytest.mark.parametrize("needle", [
    "Invoke-Native",           # stderr 重定向 + LASTEXITCODE
    "@argList",                # 参数展开（不能把数组当字符串）
    "FENCED",                  # 过期租约写入被拒
    "UTF-8 round-trip",        # 中文往返
    "no code branch",          # verify 只读
    "close + archive",
    "cmd.exe /c ver",          # cmd /c 的引号陷阱
])
def test_windows_smoke_covers_known_traps(needle):
    assert needle in PS1.read_text(encoding="utf-8-sig"), needle


def test_windows_baseline_doc_lists_the_judges():
    text = DOC.read_text(encoding="utf-8")
    for word in ("verify-smoke.ps1", "17", "UTF-8", "fencing", "不覆盖"):
        assert word in text, f"windows-baseline.md 缺 {word!r}"


# --------------------------------------------------------------------------- #
# init：新模块随仓库走
# --------------------------------------------------------------------------- #

def test_init_copies_new_modules(tmp_path):
    from conftest import sh, HIVE  # noqa: WPS433
    target = tmp_path / "hive-new"
    sh([sys.executable, HIVE, "--dir", target, "init"])
    for name in ("hive.py", "hive_pr.py", "hive_compat.py"):
        assert (target / name).is_file(), f"init 没有带上 {name}"


def test_work_has_pr_and_cluster_switches():
    from hive import build_parser
    args = build_parser().parse_args(["work", "--agent", "a", "--pr", "--no-cluster"])
    assert args.pr is True and args.no_cluster is True
    args = build_parser().parse_args(["init", "--cluster"])
    assert args.cluster is True
    args = build_parser().parse_args(["lease", "list"])
    assert args.func.__name__ == "cmd_lease"
    args = build_parser().parse_args(["pr", "T-0001", "--dry-run"])
    assert args.func.__name__ == "cmd_pr" and args.dry_run is True


# --------------------------------------------------------------------------- #
# 文档链接
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("doc", [
    "docs/租约与fencing.md",
    "docs/PR自动化.md",
    "docs/看板写操作与鉴权.md",
    "docs/windows-baseline.md",
])
def test_new_docs_exist_and_have_no_placeholders(doc):
    p = ROOT / doc
    assert p.is_file(), p
    text = p.read_text(encoding="utf-8")
    assert len(text) > 800, f"{doc} 太短，是不是只写了个标题？"
    for bad in ("TODO", "待补", "占位"):
        assert bad not in text, f"{doc} 含占位标记 {bad!r}"


def test_readme_links_new_docs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("租约与fencing", "PR自动化", "看板写操作与鉴权", "windows-baseline"):
        assert name in readme, f"README 没有指向 {name}"
