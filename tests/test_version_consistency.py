"""锁住版本号唯一真源（T-0272）。

背景：hive.py 的 VERSION 是 0.15.2，pyproject.toml 曾写 0.1.0，
导致 pip 装的版本和 hive --version 打印的不一致，用户报 bug 时说不清版本。
"""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _hive_py_version() -> str:
    text = (ROOT / "hive.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "hive.py 里找不到 VERSION = \"...\""
    return m.group(1)


def test_pyproject_version_matches_hive_py():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_version = data["project"]["version"]
    hive_version = _hive_py_version()
    assert pyproject_version == hive_version, (
        f"版本号不一致：pyproject.toml={pyproject_version}，"
        f"hive.py VERSION={hive_version}。真源是 hive.py，请改 pyproject.toml。"
    )


def test_version_looks_like_semver():
    v = _hive_py_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"VERSION={v!r} 不是 X.Y.Z"
