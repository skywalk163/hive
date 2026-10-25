"""T-0143：外部命令出口的编码纪律。

`subprocess.run(text=True)` 的隐含语义是「stdin / stdout 都用 locale 编码」。
Windows 上 locale 是 cp936，于是 `commit_file_to_ref` 用
`git hash-object -w --stdin` 送进去的结果 JSON 会按 **GBK** 落盘
（实测 `results/T-0142.json` 里 `verify[].cmd` 与 `tail[]` 全是 GBK 字节）；
Linux 侧再 `git show` 取回、按 UTF-8 解码就直接 `UnicodeDecodeError`，
`status` / `doctor` 整条命令崩掉、退出码非 0。

修法是在 `run()` 这一个出口钉死 `encoding="utf-8"` + `errors="replace"`，
所以这里的守卫也盯住这一个对象：去掉这两个参数就红。
"""
from __future__ import annotations

import json
import locale
import os
import subprocess

import pytest

import hive

# T-0142 真实事故里的那段 verify 命令，带中文。
SAMPLE = {
    "task": "T-0142",
    "agent": "raccoon-win",
    "status": "failed",
    "verify": [{
        "cmd": "grep -q '^## 11\\. Windows 客户端接入' docs/MCP接入.md",
        "code": 1,
        "tail": ["'grep' 不是内部或外部命令。"],
    }],
}


def _git_env(index_path) -> dict:
    return {
        **os.environ,
        "GIT_INDEX_FILE": str(index_path),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }


def _raw(args, cwd, env=None, input=None):
    """绕过 hive.run 的原始调用：写入侧要用 bytes 才能造出非法数据。"""
    return subprocess.run(args, cwd=str(cwd), env=env, input=input,
                          capture_output=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _raw(["git", "init", "-q", "-b", "main"], repo)
    return repo


def _put_result_blob(repo, tid: str, raw: bytes) -> str:
    """把 <raw> 原样塞成 results/<tid>.json，返回 ref 名。"""
    repo.mkdir(exist_ok=True)
    blob = _raw(["git", "hash-object", "-w", "--stdin"], repo,
                input=raw).stdout.decode().strip()
    idx = repo / ".hive-test-index"
    env = _git_env(idx)
    _raw(["git", "read-tree", "--empty"], repo, env=env)
    _raw(["git", "update-index", "--add", "--cacheinfo",
          f"100644,{blob},results/{tid}.json"], repo, env=env)
    tree = _raw(["git", "write-tree"], repo, env=env).stdout.decode().strip()
    commit = _raw(["git", "commit-tree", tree, "-m", "result"], repo,
                  env=env).stdout.decode().strip()
    ref = f"refs/heads/res/{tid}"
    _raw(["git", "update-ref", ref, commit], repo, env=env)
    idx.unlink(missing_ok=True)
    return f"res/{tid}"


# --- 读取侧：别人的脏数据不许打死 hive ------------------------------------- #

def test_non_utf8_result_blob_is_still_readable(tmp_path):
    """GBK 编码的结果文件：读得出来、字段还在，不是崩掉。

    这是 T-0142 的真实复现——修之前这里直接抛 UnicodeDecodeError，
    status / doctor 整条命令没法用。
    """
    repo = _make_repo(tmp_path)
    raw = json.dumps(SAMPLE, ensure_ascii=False, indent=2).encode("gbk")
    ref = _put_result_blob(repo, "T-0142", raw)

    data = hive.read_ref_file(repo, ref, "results/T-0142.json")

    assert isinstance(data, dict), "脏数据应该被降级读取，而不是让调用方拿到 None"
    assert data["task"] == "T-0142"
    assert data["verify"][0]["code"] == 1


def test_same_bytes_would_have_crashed_without_errors(tmp_path):
    """证明上一条是真守卫：同样的字节，按 UTF-8 严格解码必然炸。"""
    repo = _make_repo(tmp_path)
    raw = json.dumps(SAMPLE, ensure_ascii=False, indent=2).encode("gbk")
    ref = _put_result_blob(repo, "T-0142", raw)

    with pytest.raises(UnicodeDecodeError):
        subprocess.run(["git", "show", f"{ref}:results/T-0142.json"],
                       cwd=str(repo), text=True, encoding="utf-8",
                       capture_output=True, check=True)


def test_unreadable_bytes_are_replaced_not_dropped(tmp_path):
    """替换是局部的：ASCII 部分一字不少，只有非法字节变 U+FFFD。"""
    repo = _make_repo(tmp_path)
    raw = json.dumps(SAMPLE, ensure_ascii=False, indent=2).encode("gbk")
    ref = _put_result_blob(repo, "T-0142", raw)

    data = hive.read_ref_file(repo, ref, "results/T-0142.json")
    # 命令里的 ASCII 骨架必须原样保留，否则卡面信息就丢了。
    assert data["verify"][0]["cmd"].startswith("grep -q")
    assert "T-0142" in json.dumps(data, ensure_ascii=False)


# --- 写入侧：进 git 的字节永远是 UTF-8--------------------------------------- #

def test_run_pins_encoding_and_errors(monkeypatch):
    """钉住 run() 的出口参数：少一个就红。

    不依赖测试机的 locale——本机 locale 恒为 utf-8 时，"不指定 encoding"
    也能碰巧写出 UTF-8，直接断言字节是抓不到回归的，只能盯住参数。
    """
    seen = {}
    real_run = subprocess.run          # 先抓原函数，否则 fake 会调回自己

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(hive.subprocess, "run", fake_run)
    hive.run(["git", "--version"], cwd=None)

    assert seen.get("encoding") == "utf-8", "写入侧必须钉死 utf-8，否则跟着 locale 跑"
    assert seen.get("errors") == "replace", "读取侧必须容错，否则外部脏数据能打死 hive"


def test_chinese_content_reaches_git_as_utf8(tmp_path):
    """端到端：中文内容过一遍 run()，落进 blob 的必须是 UTF-8 字节。"""
    repo = _make_repo(tmp_path)
    content = json.dumps(SAMPLE, ensure_ascii=False, indent=2)

    sha = hive.run(["git", "hash-object", "-w", "--stdin"], repo,
                   input=content).stdout.strip()
    raw = _raw(["git", "cat-file", "-p", sha], repo).stdout

    assert raw == content.encode("utf-8"), "进 git 的字节必须是 UTF-8，不是 locale 编码"
    # 反向确认：这批字节按 GBK 解码会乱码，说明它们确实是 UTF-8 而非 GBK。
    assert raw.decode("utf-8") == content


def test_utf8_written_then_read_back_roundtrips(tmp_path):
    """写进去什么就读回什么（Windows 与 Linux 读出同一份内容）。"""
    repo = _make_repo(tmp_path)
    content = json.dumps(SAMPLE, ensure_ascii=False, indent=2)
    ref = _put_result_blob(repo, "T-0200", content.encode("utf-8"))

    data = hive.read_ref_file(repo, ref, "results/T-0200.json")

    assert data == SAMPLE, "UTF-8 写、UTF-8 读，不该有任何字符损毁"


def test_locale_is_not_the_thing_we_rely_on():
    """记录一个事实：本机 locale 恒为 utf-8，所以字节断言必须由参数断言兜底。

    换台机器这条可能不成立，届时字节断言会自然变成真守卫——留个提醒，
    别让人误以为 test_chinese_content_reaches_git_as_utf8 已经足够。
    """
    assert locale.getpreferredencoding(False).lower().replace("-", "") in (
        "utf8", "ascii", "ansi_x3.4-1968", "cp936", "gbk",
    )
