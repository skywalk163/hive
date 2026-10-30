"""PR 自动化（hive_pr.py）的单元测试：用本机 HTTP 桩验证 Gitea/GitLab 协议。

不联网、不依赖真实 Gitea：请求由 ThreadingHTTPServer 收，断言的是 hive 发出的
方法/路径/鉴权头/JSON 体，以及幂等（第二次不再 POST）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hive_pr  # noqa: E402


class _Stub:
    def __init__(self):
        self.calls = []
        self.pulls = []
        self.merge_requests = []
        self.fail_status = None


def _make_server(stub: _Stub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: A003 - 静音
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            stub.calls.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}))
            if stub.fail_status:
                self._send(stub.fail_status, {"message": "boom"})
            elif self.path.endswith("/pulls") or "/pulls?" in self.path:
                self._send(200, stub.pulls)
            elif "/merge_requests" in self.path:
                self._send(200, stub.merge_requests)
            else:
                self._send(404, {"message": "not found"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            stub.calls.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            if stub.fail_status:
                self._send(stub.fail_status, {"message": "boom"})
                return
            if self.path.endswith("/pulls"):
                pr = {"number": 7, "html_url": "http://x/pr/7", "state": "open",
                      "head": {"ref": body.get("head")},
                      "base": {"ref": body.get("base")}}
                stub.pulls.append(pr)
                self._send(201, pr)
            elif self.path.endswith("/merge_requests"):
                mr = {"iid": 9, "web_url": "http://x/mr/9", "state": "opened",
                      "source_branch": body.get("source_branch"),
                      "target_branch": body.get("target_branch")}
                stub.merge_requests.append(mr)
                self._send(201, mr)
            else:
                self._send(404, {"message": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def stub_server(monkeypatch):
    stub = _Stub()
    srv = _make_server(stub)
    port = srv.server_address[1]
    monkeypatch.setenv("HIVE_PR_API", f"http://127.0.0.1:{port}/api/v1")
    monkeypatch.setenv("HIVE_PR_TOKEN", "secret-token")
    monkeypatch.delenv("HIVE_GITEA_TOKEN", raising=False)
    monkeypatch.delenv("HIVE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("HIVE_PR_PROVIDER", raising=False)
    yield stub, port
    srv.shutdown()


def _repo(tmp_path: Path, remote_url: str) -> Path:
    repo = tmp_path / "light"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote_url],
                   check=True)
    return repo


# --------------------------------------------------------------------------- #
# 远端解析
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url,provider,owner,repo", [
    ("http://192.168.1.5:3000/skywalk/hive.git", "gitea", "skywalk", "hive"),
    ("https://gitlab.com/group/sub/light.git", "gitlab", "group/sub", "light"),
    ("git@github.com:skywalk/hive.git", "github", "skywalk", "hive"),
    ("ssh://git@gitlab.example.com:2222/a/b.git", "gitlab", "a", "b"),
])
def test_parse_remote(url, provider, owner, repo):
    parsed = hive_pr.parse_remote(url)
    assert parsed["provider"] == provider
    assert parsed["owner"] == owner
    assert parsed["repo"] == repo


def test_parse_remote_rejects_no_repo():
    with pytest.raises(hive_pr.PrError):
        hive_pr.parse_remote("http://host/onlyowner")


# --------------------------------------------------------------------------- #
# Gitea
# --------------------------------------------------------------------------- #

def test_gitea_create_then_idempotent(tmp_path, stub_server):
    stub, port = stub_server
    repo = _repo(tmp_path, "http://192.168.1.5:3000/skywalk/light.git")
    res = hive_pr.create_pr(repo, "T-0100", base="main", title="T-0100: x",
                            body="body", provider="gitea")
    assert res["created"] is True
    assert res["number"] == 7
    assert res["url"] == "http://x/pr/7"
    posts = [c for c in stub.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1] == "/api/v1/repos/skywalk/light/pulls"
    assert posts[0][3]["head"] == "task/T-0100"
    assert posts[0][3]["base"] == "main"
    assert posts[0][2].get("authorization") == "token secret-token"

    again = hive_pr.create_pr(repo, "T-0100", base="main", provider="gitea")
    assert again["created"] is False
    assert again["number"] == 7
    assert len([c for c in stub.calls if c[0] == "POST"]) == 1


def test_gitea_dry_run_does_not_talk(tmp_path, stub_server):
    stub, _ = stub_server
    repo = _repo(tmp_path, "http://192.168.1.5:3000/skywalk/light.git")
    res = hive_pr.create_pr(repo, "T-0101", provider="gitea", dry_run=True)
    assert res["dry_run"] is True
    assert stub.calls == []
    assert res["api"].endswith("/api/v1")
    assert res["request"]["head"] == "task/T-0101"


def test_dry_run_needs_no_token(tmp_path, monkeypatch):
    """没配 token 的机器也能 `hive pr --dry-run`（它不校验也不发请求）。"""
    for key in ("HIVE_PR_TOKEN", "HIVE_GITEA_TOKEN", "HIVE_GITLAB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HIVE_PR_ENV", str(tmp_path / "absent.env"))
    monkeypatch.setenv("HIVE_ENV", str(tmp_path / "absent2.env"))
    repo = _repo(tmp_path, "http://192.168.1.5:3000/skywalk/light.git")
    res = hive_pr.create_pr(repo, "T-0102", provider="gitea", dry_run=True)
    assert res["token_source"] == "" and res["dry_run"] is True


# --------------------------------------------------------------------------- #
# GitLab
# --------------------------------------------------------------------------- #

def test_gitlab_create_and_idempotent(tmp_path, stub_server):
    stub, port = stub_server
    repo = _repo(tmp_path, "https://gitlab.example.com/group/light.git")
    res = hive_pr.create_pr(repo, "T-0200", base="main", provider="gitlab")
    assert res["created"] is True
    assert res["number"] == 9
    posts = [c for c in stub.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1] == "/api/v1/projects/group%2Flight/merge_requests"
    assert posts[0][2].get("private-token") == "secret-token"
    assert posts[0][3]["source_branch"] == "task/T-0200"

    again = hive_pr.create_pr(repo, "T-0200", base="main", provider="gitlab")
    assert again["created"] is False
    assert len([c for c in stub.calls if c[0] == "POST"]) == 1


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #

def test_missing_token_is_actionable(tmp_path, monkeypatch, stub_server):
    for key in ("HIVE_PR_TOKEN", "HIVE_GITEA_TOKEN", "HIVE_GITLAB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HIVE_PR_ENV", str(tmp_path / "absent.env"))
    monkeypatch.setenv("HIVE_ENV", str(tmp_path / "absent2.env"))
    repo = _repo(tmp_path, "http://127.0.0.1:1/skywalk/light.git")
    with pytest.raises(hive_pr.PrError) as exc:
        hive_pr.create_pr(repo, "T-0300", provider="gitea")
    assert "HIVE_PR_TOKEN" in str(exc.value)


def test_github_is_rejected(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "git@github.com:skywalk/hive.git")
    with pytest.raises(hive_pr.PrError) as exc:
        hive_pr.create_pr(repo, "T-0400", provider="github")
    assert "gh pr create" in str(exc.value)


def test_http_error_is_redacted(tmp_path, stub_server):
    stub, _ = stub_server
    stub.fail_status = 500
    repo = _repo(tmp_path, "http://192.168.1.5:3000/skywalk/light.git")
    with pytest.raises(hive_pr.PrError) as exc:
        hive_pr.create_pr(repo, "T-0500", provider="gitea")
    text = str(exc.value)
    assert "secret-token" not in text
    assert "HTTP 500" in text


# --------------------------------------------------------------------------- #
# 文案
# --------------------------------------------------------------------------- #

def test_build_body_contains_verify_and_summary():
    text = hive_pr.build_body("T-0600", {"repo": "light", "base": "main"},
                              {"status": "done", "head": "abc123",
                               "duration_s": 1.5,
                               "verify": [{"cmd": "pytest -q", "code": 0}],
                               "summary": "做了 A"})
    assert "T-0600" in text
    assert "pytest -q" in text
    assert "做了 A" in text
