#!/usr/bin/env bash
# 检查 Gitea Actions：runner 状态 + 指定仓库最新 run，写 dashboard/ci.json
#
# 用法：  CI_REPOS="skywalk/hive" ./scripts/ci-check.sh
# 输出：  dashboard/ci.json（面板可读）+ stdout 一行摘要
#
# 环境变量：
#   HIVE_ENV   存放 gitea 凭证的文件（默认 ~/dswork/.env，取 `gitea =` 行）
#   CI_OUT     输出 json 路径（默认 <hive>/dashboard/ci.json）
#   CI_REPOS   要看的仓库（默认 skywalk/hive —— 现阶段只盯 hive 自身）
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HIVE_DIR="${HIVE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
HIVE_ENV="${HIVE_ENV:-$HOME/dswork/.env}"
OUT="${CI_OUT:-$HIVE_DIR/dashboard/ci.json}"
REPOS="${CI_REPOS:-skywalk/hive}"

export HIVE_DIR HIVE_ENV OUT REPOS

python3 - <<'PY'
import base64, datetime, json, os, re, urllib.request
from pathlib import Path
from urllib.parse import urlparse

env = Path(os.environ["HIVE_ENV"])
val = next(
    (l.split("=", 1)[1].strip().strip('"').strip("'")
     for l in env.read_text(encoding="utf-8").splitlines()
     if l.strip().startswith("gitea") and "=" in l),
    None,
)
if not val:
    raise SystemExit(f"ci-check: {env} 里没有 gitea = 行")

u = urlparse(val)
# 注意：netloc 里带着 user:token@，直接拼会把凭证当成主机名
base = f"{u.scheme}://{u.hostname}:{u.port}"
auth = base64.b64encode(f"{u.username}:{u.password}".encode()).decode()


def get(path: str):
    req = urllib.request.Request(base + path, headers={
        "Authorization": "Basic " + auth, "User-Agent": "hive-ci-check"})
    with urllib.request.urlopen(req, timeout=15) as x:
        return x.read()


def get_json(path: str):
    return json.loads(get(path))


def run_rows(repo: str):
    """Gitea 1.24 没有 run 列表 API，直接 parse Actions 页面。"""
    html = get(f"/{repo}/actions").decode("utf-8", "replace")
    cards = re.split(r'<div class="flex-item tw-items-center">', html)[1:]
    rows = []
    for c in cards:
        run = re.search(r'ci\.yml #(\d+)', c)
        if not run:
            continue
        st = re.search(r'data-tooltip-content="([A-Za-z]+)"', c)
        title = re.search(r'class="flex-item-title" title="([^"]+)"', c)
        sha = re.search(r'/commit/([0-9a-f]{8})', c)
        rows.append({
            "run": int(run.group(1)),
            "status": st.group(1) if st else "?",
            "title": (title.group(1) if title else ""),
            "sha": (sha.group(1) if sha else ""),
        })
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in ("Waiting", "Running", "Success", "Failure", "Canceled")}
    return {"latest": rows[0] if rows else None, "counts": counts, "rows": rows[:5]}


out = {
    "checked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "gitea": base,
    "runners": [],
    "repos": {},
}
try:
    out["runners"] = [{"name": r.get("name"), "status": r.get("status"),
                       "busy": r.get("busy")}
                      for r in get_json("/api/v1/admin/actions/runners").get("runners", [])]
except Exception as e:
    out["error_runners"] = f"{type(e).__name__}: {e}"

for repo in [r for r in os.environ["REPOS"].split() if r]:
    try:
        out["repos"][repo] = run_rows(repo)
    except Exception as e:
        out["repos"][repo] = {"error": f"{type(e).__name__}: {e}"}

Path(os.environ["OUT"]).write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

runs = ", ".join(f"{r}:{'/'.join(o['latest']['status'] if o.get('latest') else 'none')}"
                 for r, o in out["repos"].items())
run_state = "/".join(sorted({r.get("status", "?") for r in out["runners"]} or ["?"]))
print(f"ci-check: runners={run_state} ({len(out['runners'])}), repos: {runs}")
PY