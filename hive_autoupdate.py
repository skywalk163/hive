#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive 自愈式自动更新（`hive autoupdate`）。

要解决的问题
------------
集群里的节点经常「本该自动更新，却一直停在一个旧 HEAD 上」，最后只能人工
上去 `git pull`；而人工 pull 又常常撞上

    错误：无法创建 '/path/.git/index.lock'：文件已存在

于是一个被中断的 git（重启看板、断电、SIGKILL）就能把整台机器**永久钉住**：
fetch / ls-remote 照旧成功，merge 每次失败，而 status 里只看到「一直落后」。

这条命令把整条自愈链路收进一个幂等入口：

1. **陈旧锁**：识别并改名归档 ``index.lock.stale-<epoch>``（不删，留证据），
   随后重试一次 —— 判定规则见 ``hive_repos.repair_stale_locks``；
2. **快进**：干净、或「脏但与本次传入文件不重叠」时 ``merge --ff-only``；
   真重叠 / 分叉一律停下并给出 blocked 明细（含文件名），绝不 reset / stash；
3. **重启**：只有看板的运行时代码比进程新时才重启，用独立会话后台执行，
   不打断正在跑的任务；重启命令是 ``dashboard-service.sh restart``（它会
   顺带清理历史遗留的孤儿刷新循环）；
4. **记账**：结论落 ``~/.hive/autoupdate/state.json``，看板 / agent 可直接读；
5. **定时**：``--install-cron`` 装成每 5 分钟一次的用户级 cron 任务。

退出码：0 = 已最新或已同步；2 = blocked（需要人或 agent 介入）；1 = 出错。
这个退出码是给 cron / 外部 agent 用的契约，不要改语义。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import hive_repos
except ImportError:  # pragma: no cover - 残缺部署（只拷了 hive.py）
    hive_repos = None  # type: ignore[assignment]

ENV_STATE = "HIVE_AUTOUPDATE_STATE"     # 覆盖状态文件路径（测试隔离）
ENV_LOCK = "HIVE_AUTOUPDATE_LOCK"       # 覆盖互斥锁路径（测试隔离）
ENV_NET_TIMEOUT = "HIVE_NET_TIMEOUT"    # 与 hive.py 的 git_net 同一口径

CRON_MARKER = "#hive-autoupdate"
DEFAULT_INTERVAL_MIN = 5
LOCK_STALE_S = 1800.0                   # 互斥锁超过 30 分钟视为上一个进程已死
NET_TIMEOUT_DEFAULT_S = 120.0

# 「改了就必须要重启看板」的运行时代码。只比 mtime，不做完整性校验：
# 目的是决定要不要重启，不是校验部署。
RUNTIME_FILES = (
    "dashboard/serve.py",
    "dashboard/plan_store.py",
    "dashboard/app.js",
    "dashboard/index.html",
    "scripts/serve-dashboard.sh",
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _no_window() -> dict:
    try:
        from hive_compat import no_window_kwargs
        return no_window_kwargs()
    except ImportError:  # pragma: no cover
        return {}


def _run(args, cwd, timeout: float | None = None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), text=True,
                              capture_output=True, timeout=timeout, env=env,
                              **_no_window())
    except (OSError, subprocess.SubprocessError) as e:
        class _P:
            returncode = -1
            stdout = ""
            stderr = str(e)
        return _P()


def _rev(repo, ref: str) -> str:
    p = _run(["rev-parse", "--verify", "-q", ref], repo, timeout=10)
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def _iso(ts: float) -> str:
    try:
        from datetime import datetime
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError, OverflowError):
        return ""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def net_timeout() -> float:
    raw = (os.environ.get(ENV_NET_TIMEOUT) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = NET_TIMEOUT_DEFAULT_S
    return value if value > 0 else NET_TIMEOUT_DEFAULT_S


def state_path() -> Path:
    override = (os.environ.get(ENV_STATE) or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".hive" / "autoupdate" / "state.json"


def lock_path() -> Path:
    override = (os.environ.get(ENV_LOCK) or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".hive" / "run" / "autoupdate.lock"


def remote_head(repo, timeout: float = 10.0) -> str:
    """`git ls-remote` 取远端主线 SHA；main 优先，其次 master。失败返回 ""。"""
    p = _run(["ls-remote", "origin", "refs/heads/main", "refs/heads/master"],
             repo, timeout=timeout)
    if p.returncode != 0:
        return ""
    found = {"main": "", "master": ""}
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        for name in found:
            if ref.endswith("refs/heads/" + name):
                found[name] = sha
    return found["main"] or found["master"]


def resolve_remote_ref(repo) -> str:
    if _rev(repo, "origin/main"):
        return "origin/main"
    if _rev(repo, "origin/master"):
        return "origin/master"
    return ""


# --------------------------------------------------------------------------- #
# 单实例互斥
# --------------------------------------------------------------------------- #
def acquire_lock(path=None, *, now: float | None = None) -> tuple[bool, dict]:
    """抢占本机 autoupdate 互斥锁（cron 每 5 分钟一次，必须防重入）。

    被上一个进程遗留的锁（进程已死或超过 30 分钟）会被改名归档后抢占，
    与 git 陈旧锁同一套「留证据、不硬删」的原则。
    """
    p = Path(path or lock_path())
    now = float(time.time() if now is None else now)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, {"error": str(e)}
    for _ in range(2):
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            pid = int(data.get("pid") or 0)
            at = float(data.get("at") or 0.0)
            if _pid_alive(pid) and (now - at) < LOCK_STALE_S:
                return False, {"held_by": pid, "age_s": now - at}
            try:
                os.replace(p, p.with_name("%s.stale-%d" % (p.name, int(now))))
            except OSError:
                try:
                    p.unlink()
                except OSError:
                    return False, {"held_by": pid, "age_s": now - at}
            continue
        except OSError as e:
            return False, {"error": str(e)}
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "at": now}).encode())
        finally:
            os.close(fd)
        return True, {"path": str(p), "pid": os.getpid()}
    return False, {"error": "无法创建互斥锁"}


def release_lock(path=None) -> None:
    p = Path(path or lock_path())
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if int(data.get("pid") or 0) != os.getpid():
            return              # 不是自己的锁，别替别人解锁
    except (OSError, ValueError):
        return
    try:
        p.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 看板新鲜度 + 延迟重启
# --------------------------------------------------------------------------- #
def dashboard_pid_file() -> Path:
    return Path.home() / ".hive" / "run" / "dashboard.pid"


def dashboard_freshness(repo, pid_file=None) -> dict:
    """看板进程是否比它要跑的运行时代码旧。

    ``dashboard-service.sh`` 在拉起脚本后立刻写 PID 文件，所以它的 mtime
    就是「这一代进程的启动时刻」；任何 RUNTIME_FILES 比它新 → 需要重启。
    """
    repo = Path(repo)
    pf = Path(pid_file or dashboard_pid_file())
    out = {"running": False, "pid": 0, "started_at": "", "stale_files": [],
           "needed": False, "pid_file": str(pf)}
    try:
        pid = int((pf.read_text(encoding="utf-8") or "0").strip())
    except (OSError, ValueError):
        pid = 0
    out["pid"] = pid
    out["running"] = _pid_alive(pid)
    if not out["running"]:
        return out
    try:
        started = pf.stat().st_mtime
    except OSError:
        return out
    out["started_at"] = _iso(started)
    stale = []
    for rel in RUNTIME_FILES:
        f = repo / rel
        try:
            if f.stat().st_mtime > started + 1:      # +1s 容忍同一秒
                stale.append(rel)
        except OSError:
            continue
    out["stale_files"] = stale
    out["needed"] = bool(stale)
    return out


def schedule_dashboard_restart(repo, *, action: str = "restart",
                               delay: float = 2.0, log_path=None) -> dict:
    """后台、独立会话地重启看板（不阻塞 autoupdate，也不继承 cron 的进程树）。

    用 ``start_new_session=True``（POSIX）脱离进程组：即使调用方是 cron 或
    autoupdate 自己，重启也不会被「停服务」的逻辑连带杀掉。
    """
    repo = Path(repo)
    script = repo / "scripts" / "dashboard-service.sh"
    if not script.exists():
        return {"scheduled": False, "reason": "找不到 scripts/dashboard-service.sh"}
    log = Path(log_path or (Path.home() / ".hive" / "logs" /
                            "autoupdate-restart.log"))
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    code = (
        "import subprocess, sys, time\n"
        "delay = float(sys.argv[1]); log = sys.argv[2]\n"
        "action = sys.argv[3]; script = sys.argv[4]; repo = sys.argv[5]\n"
        "time.sleep(delay)\n"
        "with open(log, 'a') as fh:\n"
        "    fh.write('--- ' + time.strftime('%F %T')\n"
        "             + ' autoupdate: ' + action + ' ' + repo + ' ---\\n')\n"
        "    subprocess.run(['/bin/sh', script, action], cwd=repo,\n"
        "                   stdin=subprocess.DEVNULL,\n"
        "                   stdout=fh, stderr=subprocess.STDOUT)\n"
    )
    argv = [sys.executable, "-c", code, str(delay), str(log), action,
            str(script), str(repo)]
    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    try:
        p = subprocess.Popen(argv,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             close_fds=True, **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        return {"scheduled": False, "reason": "%s: %s" % (type(e).__name__, e)}
    return {"scheduled": True, "helper_pid": p.pid, "action": action,
            "delay_s": delay, "log": str(log)}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(repo, *, restart: bool = False, dry_run: bool = False,
        now: float | None = None, use_lock: bool = True) -> dict:
    """执行一轮自愈更新，返回结构化结果（同时落 state.json，dry_run 除外）。"""
    repo = Path(repo).expanduser().resolve()
    now = float(time.time() if now is None else now)
    out = {
        "repo": str(repo),
        "checked_at": _iso(now),
        "checked_ts": now,
        "dry_run": bool(dry_run),
        "head_before": "", "head_after": "", "remote_sha": "",
        "state": "", "action": "", "synced": False, "changed": False,
        "locks_repaired": [], "sync_error": "", "blocked": [],
        "dirty_files": [], "overlap_files": [],
        "restart": {"needed": False, "scheduled": False, "stale_files": []},
        "exit_code": EXIT_OK, "error": "",
    }
    if hive_repos is None:
        out["error"] = "缺少 hive_repos.py，无法做自愈同步（残缺部署）"
        out["exit_code"] = EXIT_ERROR
        return _finish(out, dry_run)
    if not (repo / ".git").exists():
        out["error"] = "%s 不是 git 仓库" % repo
        out["exit_code"] = EXIT_ERROR
        return _finish(out, dry_run)

    got = True
    if use_lock and not dry_run:
        got, info = acquire_lock(now=now)
        if not got:
            # 另一个 autoupdate 正在跑：本轮什么都不做，也不算失败
            out["action"] = "skip-concurrent"
            out["error"] = "已有 autoupdate 在跑（pid=%s）" % info.get("held_by", "?")
            return _finish(out, dry_run)
    try:
        # 1) 陈旧锁：先自愈，后面所有写操作才可能成功
        rep = hive_repos.repair_stale_locks(repo, now=now, dry_run=dry_run)
        out["locks_repaired"] = [Path(x["path"]).name
                                 for x in (rep.get("repaired") or [])]
        if rep.get("error"):
            out["error"] = rep["error"]

        # 2) 取远端（ls-remote 只读，能省一次全量 fetch）
        out["head_before"] = _rev(repo, "HEAD")
        ref = resolve_remote_ref(repo)
        if not ref:
            out["error"] = "仓库里没有 origin/main 或 origin/master"
            out["exit_code"] = EXIT_ERROR
            return _finish(out, dry_run)
        known_remote = _rev(repo, ref)
        fresh_remote = remote_head(repo)
        if fresh_remote and fresh_remote != known_remote:
            rc, _o, err = hive_repos.git_write(
                repo, ["fetch", "--prune", "origin"], timeout=net_timeout())
            if rc != 0:
                out["sync_error"] = (err or "").strip()[:600]
                out["error"] = "git fetch 失败"
                out["exit_code"] = EXIT_ERROR
                return _finish(out, dry_run)
            known_remote = _rev(repo, ref)
        out["remote_sha"] = known_remote or ""

        # 3) 三态判定 + 快进。自愈入口**显式**允许「脏但不重叠」的 ff：
        # status 那条路仍然保守（L5 验收口径），策略跟着入口走。
        assess = hive_repos.assess_sync_state(
            repo, remote_ref=ref, allow_dirty_ff=True)
        out["state"] = assess["state"]
        out["action"] = assess["action"]
        out["overlap_files"] = list(assess.get("overlap_files") or [])
        out["dirty_files"] = list(assess.get("dirty_files") or [])

        if assess["state"] in (hive_repos.CLEAN_FF, hive_repos.DIRTY_FF):
            if dry_run:
                out["action"] = "would-ff"
            elif assess["remote_sha"] == out["head_before"]:
                # 远端没前进（只是脏工作区）：没什么可合并的，别报「已快进」
                out["state"] = "up-to-date"
                out["action"] = "none"
            else:
                rc, _o, err = hive_repos.git_write(
                    repo, ["merge", "--ff-only", ref], timeout=120.0)
                if rc == 0:
                    out["synced"] = True
                    out["head_after"] = _rev(repo, "HEAD")
                    out["changed"] = out["head_after"] != out["head_before"]
                else:
                    out["sync_error"] = (err or "").strip()[:600]
                    out["exit_code"] = EXIT_BLOCKED
                    out["blocked"].append({
                        "kind": "merge-failed",
                        "reason": (out["sync_error"].splitlines() or ["未知原因"])[0],
                    })
        elif assess["state"] == hive_repos.DIRTY_WORKTREE:
            out["exit_code"] = EXIT_BLOCKED
            out["blocked"].append({
                "kind": "dirty-overlap",
                "files": out["overlap_files"] or out["dirty_files"],
                "reason": assess["reason"],
            })
        elif assess["state"] == hive_repos.CLEAN_DIVERGED:
            out["exit_code"] = EXIT_BLOCKED
            out["blocked"].append({"kind": "diverged",
                                   "reason": assess["reason"]})
        else:
            out["action"] = "none"

        # 4) 重启：只有真的先执行了重启才可能落到这里
        if restart:
            fresh = dashboard_freshness(repo)
            out["restart"] = {
                "needed": bool(fresh["needed"]),
                "scheduled": False,
                "stale_files": list(fresh["stale_files"]),
                "running": fresh["running"],
                "pid": fresh["pid"],
                "started_at": fresh["started_at"],
            }
            if dry_run:
                out["restart"]["would_schedule"] = bool(
                    fresh["stale_files"] or not fresh["running"])
            elif fresh["stale_files"] or not fresh["running"]:
                # 没在跑就 start；代码比进程新就 restart
                action = "restart" if fresh["running"] else "start"
                out["restart"].update(schedule_dashboard_restart(repo,
                                                                 action=action))
        return _finish(out, dry_run)
    except Exception as e:                       # noqa: BLE001 - cron 不能崩
        out["error"] = "%s: %s" % (type(e).__name__, e)
        out["exit_code"] = EXIT_ERROR
        return _finish(out, dry_run)
    finally:
        if got and not dry_run:
            release_lock()


def _finish(out: dict, dry_run: bool) -> dict:
    if not dry_run:
        write_state(out)
    return out


def write_state(out: dict) -> None:
    p = state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# 定时任务（用户级 cron）
# --------------------------------------------------------------------------- #
def cron_command(repo, *, every_min: int = DEFAULT_INTERVAL_MIN,
                 python: str | None = None) -> str:
    repo = str(Path(repo).expanduser().resolve())
    py = python or sys.executable
    log = str(Path.home() / ".hive" / "logs" / "autoupdate.log")
    # cron 的默认 PATH 里通常没有 /usr/local/bin（FreeBSD 的 git 在那），
    # 不显式给 PATH 会以「git 找不到」失败；解释器用绝对路径，避免依赖 PATH。
    return ("PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin; export PATH; "
            "cd %(repo)s && %(py)s %(repo)s/hive.py --dir %(repo)s autoupdate "
            "--restart >> %(log)s 2>&1 %(marker)s"
            % {"repo": repo, "py": py, "log": log, "marker": CRON_MARKER})


def _crontab_read() -> tuple[int, str]:
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                           timeout=10, **_no_window())
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "%s: %s" % (type(e).__name__, e)
    # 没有 crontab 时 `crontab -l` 返回 1 并打印 "no crontab for user"
    return p.returncode, (p.stdout or "")


def _crontab_write(text: str) -> tuple[bool, str]:
    # 测试进程绝不允许改真实 crontab：一次误写会让 cron 每 5 分钟跑一条指向
    # pytest 临时目录的命令（实测在日志里留下过这种残迹）。
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False, "测试环境（PYTEST_CURRENT_TEST）拒绝写真实 crontab"
    try:
        p = subprocess.run(["crontab", "-"], input=text, capture_output=True,
                           text=True, timeout=10, **_no_window())
    except (OSError, subprocess.SubprocessError) as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return p.returncode == 0, (p.stderr or "").strip()


def install_cron(repo, *, every_min: int = DEFAULT_INTERVAL_MIN,
                 dry_run: bool = False) -> dict:
    """把 autoupdate 装成每 N 分钟一次的用户 cron（幂等，可重复执行）。"""
    every_min = (DEFAULT_INTERVAL_MIN if every_min is None
                 else int(every_min))
    if not 1 <= every_min <= 59:
        return {"ok": False, "changed": False,
                "reason": "every_min 必须在 1..59 之间"}
    line = "*/%d * * * * %s" % (every_min, cron_command(repo))
    rc, cur = _crontab_read()
    if rc < 0:
        return {"ok": False, "changed": False, "reason": cur}
    kept = [ln for ln in cur.splitlines()
            if CRON_MARKER not in ln and ln.strip()]
    new_text = "\n".join(kept + [line]) + "\n"
    changed = line not in cur.splitlines()
    if dry_run:
        return {"ok": True, "changed": changed, "line": line,
                "dry_run": True}
    if not changed:
        return {"ok": True, "changed": False, "line": line}
    ok, err = _crontab_write(new_text)
    return {"ok": ok, "changed": ok, "line": line,
            "reason": "" if ok else err}


def uninstall_cron(*, dry_run: bool = False) -> dict:
    rc, cur = _crontab_read()
    if rc < 0:
        return {"ok": False, "changed": False, "reason": cur}
    lines = cur.splitlines()
    kept = [ln for ln in lines if CRON_MARKER not in ln]
    changed = len(kept) != len(lines)
    if dry_run or not changed:
        return {"ok": True, "changed": changed, "dry_run": dry_run}
    ok, err = _crontab_write("\n".join(kept) + "\n" if kept else "")
    return {"ok": ok, "changed": ok, "reason": "" if ok else err}


# --------------------------------------------------------------------------- #
# 人读输出
# --------------------------------------------------------------------------- #
def summary_text(res: dict) -> str:
    lines = []
    head = (res.get("head_after") or res.get("head_before") or "")[:8]
    remote = (res.get("remote_sha") or "")[:8]
    if res.get("error") and res.get("action") == "skip-concurrent":
        return "[hive] 另一个 autoupdate 正在跑，本轮跳过（%s）" % res["error"]
    if res.get("locks_repaired"):
        lines.append("[hive] 已归档陈旧 git 锁：%s"
                     % "、".join(res["locks_repaired"]))
    if res.get("synced") and res.get("changed"):
        lines.append("[hive] 已快进：%s → %s（%s）"
                     % ((res.get("head_before") or "")[:8],
                        (res.get("head_after") or "")[:8], res.get("state")))
    elif res.get("synced"):
        lines.append("[hive] 已是最新（%s）" % (head or "-"))
    elif res.get("state") == "up-to-date":
        lines.append("[hive] 已是最新（本地 %s，远端 %s）" % (head or "-",
                                                          remote or "-"))
    elif res.get("state"):
        lines.append("[hive] 仓库状态：%s / %s（本地 %s，远端 %s）"
                     % (res["state"], res.get("action") or "-", head or "-",
                        remote or "-"))
    for b in res.get("blocked") or []:
        files = "、".join(b.get("files") or [])
        lines.append("[hive] 需要人工/agent 介入：%s%s"
                     % (b.get("reason", b.get("kind")),
                        ("（%s）" % files) if files else ""))
    rs = res.get("restart") or {}
    if rs.get("scheduled"):
        lines.append("[hive] 看板已安排 %s（独立会话，%ss 后执行）"
                     % (rs.get("action"), int(rs.get("delay_s") or 0)))
    elif rs.get("would_schedule"):
        stale = "、".join(rs.get("stale_files") or []) or "看板未运行"
        lines.append("[hive] dry-run：本应重启看板（%s）" % stale)
    elif rs.get("running") and not rs.get("needed"):
        lines.append("[hive] 看板代码是最新的，无需重启")
    if res.get("error") and res.get("exit_code") == EXIT_ERROR:
        lines.append("[hive] 出错：%s" % res["error"])
    if not lines:
        lines.append("[hive] 已是最新（%s）" % (head or "-"))
    return "\n".join(lines)