#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive PR 自动化：把「推送 task/<tid> 分支」接上 Gitea / GitLab 的 PR/MR。

为什么单独一个模块
------------------
`hive.py` 是零依赖的单文件工具，PR 只是**可选**的最后一公里：没有 token、没有
网络、代码仓不是 Gitea/GitLab 时，hive 的领取/执行/验收全都不受影响。因此：

* 只用 Python 标准库（urllib），不引入 requests；
* hive.py 里按需 `import hive_pr`，缺模块只让 `--pr` 报错；
* **绝不把 token 写进协调仓库、日志或异常信息**（异常统一 redact）。

设计口径
--------
* **幂等**：先查同 head/base 的 open PR，存在就复用，不重复创建；
* **只写 PR，不合并**：合并仍由人 review 后做（`hive land` 是另一条路）；
* **失败不影响任务**：worker 的 `--pr` 是 best-effort，结果里记 `pr.error`。

环境变量 / 配置
----------------
* `HIVE_PR_PROVIDER`      gitea | gitlab | none（默认按远端域名推断）
* `HIVE_PR_TOKEN`         统一 token（优先）
* `HIVE_GITEA_TOKEN`      Gitea token（`Authorization: token ...`）
* `HIVE_GITLAB_TOKEN`     GitLab token（`PRIVATE-TOKEN: ...`）
* `HIVE_PR_API`           覆盖 API 根地址（测试/自建反代用）
* `HIVE_PR_TIMEOUT`       HTTP 超时秒数（默认 20）
* `HIVE_PR_ENV`           指定凭据文件（默认找 ~/.config/hive/gitea.env、
                          ~/.config/hive/pr.env，以及仓库/工作目录向上的 .env）

凭据文件格式（与 `docs/Gitea接入.md` 兼容）::

    gitea = http://<user>:<token>@192.168.1.5:3000
    # 或
    token = <token>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

DEFAULT_TIMEOUT = 20.0
SUPPORTED = ("gitea", "gitlab")
ENV_KEYS = ("HIVE_PR_TOKEN", "HIVE_GITEA_TOKEN", "HIVE_GITLAB_TOKEN")


class PrError(Exception):
    """PR 自动化的可预期失败（缺 token、远端不支持、HTTP 4xx/5xx）。"""


# --------------------------------------------------------------------------- #
# 远端解析
# --------------------------------------------------------------------------- #

def parse_remote(url: str) -> dict:
    """把 git remote URL 解析成 {scheme, host, path, owner, repo, provider}。

    支持 http(s)、ssh://、scp 风格（``git@host:owner/repo.git``），也支持嵌套
    GitLab 组（``group/sub/repo``，owner 保留 ``group/sub``）。
    """
    raw = (url or "").strip()
    if not raw:
        raise PrError("远端 URL 为空")
    scheme, host, path = "", "", ""
    if "://" in raw:
        u = urlparse.urlparse(raw)
        scheme = u.scheme or "https"
        host = u.hostname or ""
        if u.port:
            host = f"{host}:{u.port}"
        path = u.path or ""
    else:
        m = re.match(r"^(?:[^@/]+@)?([^:/]+)[:/](.+)$", raw)
        if not m:
            raise PrError(f"不认识的 git 远端: {raw}")
        host, path = m.group(1), m.group(2)
        scheme = "ssh"
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise PrError(f"远端 URL 里没有 owner/repo: {raw}")
    owner = "/".join(parts[:-1])
    repo = parts[-1]
    low = host.lower()
    if "gitlab" in low:
        provider = "gitlab"
    elif "github" in low:
        provider = "github"
    else:
        provider = "gitea"
    return {"scheme": scheme, "host": host, "path": path,
            "owner": owner, "repo": repo, "provider": provider}


def git_remote(repo: Path, name: str = "origin") -> str:
    p = subprocess.run(["git", "-C", str(repo), "remote", "get-url", name],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0 or not p.stdout.strip():
        raise PrError(f"{repo} 没有配置 {name} 远端")
    return p.stdout.strip()


def _api_base(remote: dict, provider: str) -> str:
    override = (os.environ.get("HIVE_PR_API") or "").strip()
    if override:
        return override.rstrip("/")
    if remote["scheme"] == "ssh":
        scheme = "https"   # ssh 远端没有 HTTP API；按同主机 https 处理
    else:
        scheme = remote["scheme"]
    suffix = "api/v1" if provider == "gitea" else "api/v4"
    return f"{scheme}://{remote['host']}/{suffix}"


# --------------------------------------------------------------------------- #
# 凭据：只读本机文件/环境，绝不回写
# --------------------------------------------------------------------------- #

def _read_env_file(path: Path) -> dict:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip().lower()] = value.strip().strip('"').strip("'")
    return out


def _token_from_value(value: str) -> str:
    """从 ``http://user:token@host`` 或裸 token 里取 token。"""
    text = (value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if "://" in text:
        try:
            u = urlparse.urlparse(text)
            if u.password:
                return u.password
            if u.username and len(u.username) > 16:
                # 无密码时 Gitea 常见写法是 http://<token>@host
                return u.username
        except ValueError:
            pass
    return text


def _candidate_env_files(repo: Path | None) -> list[Path]:
    files: list[Path] = []
    explicit = (os.environ.get("HIVE_PR_ENV") or "").strip()
    if explicit:
        files.append(Path(explicit).expanduser())
    legacy = (os.environ.get("HIVE_ENV") or "").strip()
    if legacy:
        files.append(Path(legacy).expanduser())
    home_cfg = Path.home() / ".config" / "hive"
    files += [home_cfg / "gitea.env", home_cfg / "pr.env"]
    roots = []
    if repo is not None:
        roots.append(Path(repo).resolve())
    roots.append(Path.cwd())
    for start in roots:
        for parent in [start, *list(start.parents)[:4]]:
            files.append(parent / ".env")
    seen: set[str] = set()
    out = []
    for p in files:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_token(remote: dict, repo: Path | None = None,
               remote_url: str = "") -> tuple[str, str]:
    """返回 (token, 来源说明)；找不到抛 PrError（消息里不含敏感值）。"""
    for key in ENV_KEYS:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value, f"env:{key}"
    for path in _candidate_env_files(repo):
        data = _read_env_file(path)
        for key in ("pr_token", "token", "gitea", "gitlab",
                    "gitea_token", "gitlab_token"):
            if key in data:
                token = _token_from_value(data[key])
                if token:
                    return token, f"file:{path}"
    # 远端 URL 内嵌 http://user:token@host 时兜底（不落盘、不进日志）
    if remote_url and "://" in remote_url:
        token = _token_from_value(remote_url)
        if token and token != remote_url:
            return token, "remote:origin"
    raise PrError(
        "没有可用的 PR token。设置 HIVE_PR_TOKEN（或 HIVE_GITEA_TOKEN / "
        "HIVE_GITLAB_TOKEN），或在 ~/.config/hive/gitea.env 写 token=...；"
        "token 只需要目标仓库的 read+PR 权限")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _redact(text: str, token: str) -> str:
    text = str(text)
    if token:
        text = text.replace(token, "***")
    return re.sub(r"https?://[^/\s:@]+:[^/\s@]+@", "https://***@", text)


def _request(method: str, url: str, token: str, provider: str,
             payload: dict | None = None, timeout: float | None = None):
    data = None
    headers = {"Accept": "application/json", "User-Agent": "hive-pr/1.0"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        if provider == "gitlab":
            headers["PRIVATE-TOKEN"] = token
        else:
            headers["Authorization"] = f"token {token}"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout or _timeout()) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(raw) if raw.strip() else None
            except ValueError:
                return raw
    except urlerror.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        detail = raw.strip()[:300]
        try:
            body = json.loads(raw)
            detail = str(body.get("message") or body.get("error") or detail)[:300]
        except (ValueError, AttributeError):
            pass
        raise PrError(_redact(
            f"{provider} HTTP {exc.code}（{method} {url}）: {detail}", token)) from None
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise PrError(_redact(
            f"连不上 {provider}（{method} {url}）: {exc}", token)) from None


def _timeout() -> float:
    try:
        v = float(os.environ.get("HIVE_PR_TIMEOUT") or DEFAULT_TIMEOUT)
        return v if v > 0 else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


# --------------------------------------------------------------------------- #
# 各平台的查询 / 创建
# --------------------------------------------------------------------------- #

def _gitea_find(api: str, owner: str, repo: str, head: str, base: str,
                token: str) -> dict | None:
    url = (f"{api}/repos/{urlparse.quote(owner)}/{urlparse.quote(repo)}"
           "/pulls?state=open&limit=50")
    data = _request("GET", url, token, "gitea")
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None
    for pr in rows:
        if not isinstance(pr, dict):
            continue
        h = (pr.get("head") or {}).get("ref") if isinstance(pr.get("head"), dict) else None
        b = (pr.get("base") or {}).get("ref") if isinstance(pr.get("base"), dict) else None
        if h == head and (base is None or b == base):
            return _normalize(pr, "gitea", created=False)
    return None


def _gitea_create(api: str, owner: str, repo: str, head: str, base: str,
                  title: str, body: str, token: str) -> dict:
    url = (f"{api}/repos/{urlparse.quote(owner)}/{urlparse.quote(repo)}/pulls")
    pr = _request("POST", url, token, "gitea",
                  {"head": head, "base": base, "title": title, "body": body})
    return _normalize(pr if isinstance(pr, dict) else {}, "gitea", created=True)


def _gitlab_find(api: str, project: str, head: str, base: str,
                 token: str) -> dict | None:
    q = urlparse.urlencode({"state": "opened", "source_branch": head,
                            "target_branch": base, "per_page": 50})
    url = f"{api}/projects/{urlparse.quote(project, safe='')}/merge_requests?{q}"
    data = _request("GET", url, token, "gitlab")
    if not isinstance(data, list):
        return None
    for mr in data:
        if isinstance(mr, dict) and mr.get("source_branch") == head:
            return _normalize(mr, "gitlab", created=False)
    return None


def _gitlab_create(api: str, project: str, head: str, base: str,
                   title: str, body: str, token: str) -> dict:
    url = f"{api}/projects/{urlparse.quote(project, safe='')}/merge_requests"
    mr = _request("POST", url, token, "gitlab",
                  {"source_branch": head, "target_branch": base,
                   "title": title, "description": body,
                   "remove_source_branch": True})
    return _normalize(mr if isinstance(mr, dict) else {}, "gitlab", created=True)


def _normalize(item: dict, provider: str, created: bool) -> dict:
    if provider == "gitlab":
        url = item.get("web_url") or item.get("url") or ""
        number = item.get("iid") or item.get("id")
        state = item.get("state") or ""
    else:
        url = item.get("html_url") or item.get("url") or ""
        number = item.get("number") or item.get("id")
        state = item.get("state") or ""
    return {"provider": provider, "url": url, "number": number,
            "state": state, "created": bool(created)}


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #

def create_pr(repo: Path | str, tid: str, head: str | None = None,
              base: str = "main", title: str = "", body: str = "",
              provider: str | None = None, dry_run: bool = False) -> dict:
    """给 ``repo`` 的 ``head`` 分支创建（或复用）一个 PR。

    返回归一化结果：{provider, url, number, state, created, head, base, ...}。
    ``dry_run=True`` 不创建，只返回将要提交的请求（用于 --dry-run 演示）。
    """
    repo = Path(repo).expanduser()
    if not (repo / ".git").exists():
        raise PrError(f"不是 git 仓库: {repo}")
    if not tid:
        raise PrError("缺少任务号")
    remote_url = git_remote(repo)
    remote = parse_remote(remote_url)
    want = (provider or os.environ.get("HIVE_PR_PROVIDER") or "").strip().lower()
    if want in ("", "auto"):
        want = remote["provider"]
    if want in ("none", "off", "false"):
        raise PrError("HIVE_PR_PROVIDER=none：已关闭 PR 自动化")
    if want == "github":
        raise PrError("GitHub 仓库请用 `gh pr create`；hive 内置只支持 Gitea/GitLab")
    if want not in SUPPORTED:
        raise PrError(f"不支持的 PR 平台: {want}（支持 gitea/gitlab）")

    head = (head or f"task/{tid}").strip()
    base = (base or "main").strip()
    title = (title or f"{tid}: 任务成果").strip()[:255]
    body = body or ""
    api = _api_base(remote, want)

    # --dry-run 不校验也不读取 token：它只是「给我看看会发什么」，
    # 没配 token 的机器也应该能看到将要创建的 PR。
    result = {"provider": want, "head": head, "base": base,
              "owner": remote["owner"], "repo": remote["repo"],
              "api": api, "token_source": "", "created": False}
    if dry_run:
        result.update({"dry_run": True, "url": "", "number": None,
                       "state": "dry-run",
                       "request": {"method": "POST", "url": api,
                                   "head": head, "base": base,
                                   "title": title}})
        return result

    token, source = find_token(remote, repo, remote_url)
    result["token_source"] = source

    if want == "gitea":
        found = _gitea_find(api, remote["owner"], remote["repo"], head, base, token)
        if found:
            found.update({"head": head, "base": base, "owner": remote["owner"],
                          "repo": remote["repo"]})
            return found
        created = _gitea_create(api, remote["owner"], remote["repo"], head, base,
                                title, body, token)
    else:
        project = f"{remote['owner']}/{remote['repo']}"
        found = _gitlab_find(api, project, head, base, token)
        if found:
            found.update({"head": head, "base": base, "owner": remote["owner"],
                          "repo": remote["repo"]})
            return found
        created = _gitlab_create(api, project, head, base, title, body, token)
    created.update({"head": head, "base": base, "owner": remote["owner"],
                    "repo": remote["repo"]})
    return created


# --------------------------------------------------------------------------- #
# 文案
# --------------------------------------------------------------------------- #

def build_title(tid: str, card: dict | None) -> str:
    card = card or {}
    return f"{tid}: {card.get('title') or '任务成果'}"


def build_body(tid: str, card: dict | None, result: dict | None = None) -> str:
    card, result = card or {}, result or {}
    lines = [f"任务卡：`{tid}`",
             f"仓库：`{card.get('repo') or ''}`　基线：`{card.get('base') or 'main'}`",
             ""]
    if result:
        lines += [f"- 状态：`{result.get('status') or '?'}`",
                  f"- 分支 head：`{str(result.get('head') or '')[:12]}`",
                  f"- 耗时：{result.get('duration_s') or 0}s"]
        verify = result.get("verify") or []
        if verify:
            lines += ["", "## verify", ""]
            for row in verify:
                lines.append(f"- `{row.get('cmd')}` → 退出码 {row.get('code')}")
        if result.get("summary"):
            lines += ["", "## 说明", "", str(result["summary"])[:4000]]
    lines += ["", "---",
              "由 hive 在推送任务分支后自动创建；合并前请按 verify 与 diff review。"]
    return "\n".join(lines)


def format_result(res: dict, tid: str) -> str:
    if res.get("dry_run"):
        return (f"{tid} [dry-run] 将向 {res.get('provider')} 创建 PR："
                f"{res.get('head')} -> {res.get('base')}")
    verb = "已创建" if res.get("created") else "已存在"
    return (f"{tid} PR {verb}：{res.get('state') or ''} "
            f"#{res.get('number') or '?'} {res.get('url') or ''}").strip()


def main(argv: list[str] | None = None) -> int:
    """独立运行入口（便于调试）：hive_pr.py <repo> <tid> [--base main]。"""
    import argparse
    p = argparse.ArgumentParser(description="hive PR 自动化（也可用 hive pr create）")
    p.add_argument("repo")
    p.add_argument("task")
    p.add_argument("--head")
    p.add_argument("--base", default="main")
    p.add_argument("--title", default="")
    p.add_argument("--body", default="")
    p.add_argument("--provider", default=None)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    try:
        res = create_pr(a.repo, a.task, head=a.head, base=a.base, title=a.title,
                        body=a.body, provider=a.provider, dry_run=a.dry_run)
    except PrError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
