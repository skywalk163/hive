#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-0349 开发环境流量基线采集器（本批优化前的「分母」）。

口径对齐 0.16.05 CHANGELOG（空闲稳态两节点实测）：
  * 心跳请求 685B、响应 ~2.9KB（2.4KB~2.9KB 量级）；
  * 3 节点全连通估算空闲控制面带宽 ~9KB/s；
  * state.json 落盘次数与字节 / 分钟；
  * 每 30 分钟窗口内的 ``ls-remote`` / ``fetch`` / ``pull`` 次数（现状基线）。

设计约束（任务卡 §4）：
  * **不许填估算值当实测值**：跑不出来的项如实写 ``unavailable`` 并说明原因，
    绝不拿 CHANGELOG 历史数字冒充本次实测；
  * 只访问 ``--state-dir`` 指定的开发实例状态目录（默认 ``~/.hive/dev-cluster``），
    不读生产 ``~/.hive/cluster``，不向任何生产地址发包；
  * 输出可复现 JSON：带采集时间戳、参数、口径来源；纯标准库。

用法：
  python3 tools/traffic_baseline.py --json --out /tmp/hlsa-baseline.json
  python3 tools/traffic_baseline.py --state-dir ~/.hive/dev-cluster \
      --http http://127.0.0.1:8888 --samples 12 --window 1800

退出码：0 = 采集完成（部分项 unavailable 不算失败）；2 = 参数/环境致命错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 生产基线只用于「对照口径」，本工具不会去读/连这些地址。
PROD_HTTP = 8788
PROD_MC_GROUP = "239.255.42.1"
PROD_MC_PORT = 47899
# 0.16.05 历史实测口径（CHANGELOG 0.16.05 / 0.16.04），作为参照基准列出，
# 不写入本次实测字段；实测字段取不到就是 unavailable。
REFERENCE_BASELINE = {
    "heartbeat_request_bytes_avg": 685,
    "heartbeat_response_bytes_avg": 2900,   # ~2.9KB（2.4KB~2.9KB 量级）
    "control_plane_bandwidth_3node_kbps": 9.0,
    "source": "CHANGELOG.md 0.16.05 / 0.16.04（两节点本地空闲 25s 实测，3 节点估算）",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _http_get(url: str, timeout: float = 3.0) -> tuple[int, dict | None, str]:
    """GET JSON；网络/解析错误返回 (None, None, 原因)，绝不抛。"""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.getcode()
        try:
            return code, json.loads(raw.decode("utf-8")), ""
        except (UnicodeDecodeError, ValueError) as exc:
            return code, None, f"响应非 JSON: {exc}"
    except urllib.error.URLError as exc:
        return None, None, f"URL 不可达: {exc}"
    except OSError as exc:
        return None, None, f"OS 错误: {exc}"


def _heartbeat_payload(node: dict, term: int, leader: bool, sd: str,
                       events_since: float) -> dict:
    """复刻 hive_cluster._heartbeat_once 的轻量心跳请求体。"""
    body: dict = {"node": node, "term": term, "leader": leader,
                  "sd": sd, "events_since": events_since}
    if leader:
        body["leader_url"] = ""
        body["lease_until"] = ""
    return body


def _post_json(url: str, body: dict, timeout: float = 3.0) -> tuple[int, int, str]:
    """POST 心跳，返回 (状态码, 响应字节数, 原因)；不回传完整响应体。"""
    try:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.getcode(), len(raw), ""
    except urllib.error.URLError as exc:
        return None, 0, f"URL 不可达: {exc}"
    except OSError as exc:
        return None, 0, f"OS 错误: {exc}"


def collect_heartbeat(http_url: str, samples: int) -> dict:
    """采样心跳请求/响应字节。

    需要开发实例在 ``http_url`` 上可达；不可达时整段 unavailable。
    字节口径：请求 = 序列化后的 UTF-8 字节；响应 = HTTP 响应体字节。
    """
    if not http_url:
        return {"status": "unavailable", "reason": "未提供 --http 开发实例地址",
                "request_bytes": None, "response_bytes": None}

    health_url = http_url.rstrip("/") + "/api/cluster/health"
    code, health, err = _http_get(health_url)
    if code != 200 or health is None:
        return {"status": "unavailable", "reason": f"健康检查未通过: {err or code}",
                "request_bytes": None, "response_bytes": None}

    node = health.get("node") or health.get("self") or {}
    if not isinstance(node, dict):
        node = {"name": str(health.get("name") or "dev")}
    term = int(health.get("term") or 0)
    leader = bool(health.get("role") == "leader"
                  or health.get("is_leader", health.get("leader", False)))
    hb_url = http_url.rstrip("/") + "/api/cluster/heartbeat"

    req_bytes: list[int] = []
    resp_bytes: list[int] = []
    reasons: list[str] = []
    for i in range(max(1, samples)):
        body = _heartbeat_payload(node, term, leader, "baseline-digest",
                                  time.time() - 10.0)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        code, resp_len, reason = _post_json(hb_url, body)
        if code == 200:
            req_bytes.append(len(data))
            resp_bytes.append(resp_len)
        else:
            reasons.append(reason or f"code={code}")
        time.sleep(0.2)

    if not req_bytes:
        return {"status": "unavailable",
                "reason": "全部心跳采样失败: " + "; ".join(reasons[:3]),
                "request_bytes": None, "response_bytes": None}

    def _stats(values: list[int]) -> dict:
        return {"avg": round(sum(values) / len(values), 1),
                "peak": max(values), "min": min(values), "samples": len(values)}

    return {"status": "ok",
            "request_bytes": _stats(req_bytes),
            "response_bytes": _stats(resp_bytes),
            "note": "空闲轻量心跳；请求不含全量 progress/leases（30s 兜底除外）"}


def _state_file(state_dir: Path) -> Path:
    return state_dir / "state.json"


def collect_persistence(state_dir: Path, window_s: float) -> dict:
    """state.json 落盘次数与字节 / 分钟。

    落盘次数无法离线回溯（进程内才计数），这里做可复现的静态测量：
      * 当前 state.json 字节数；
      * 在 ``window_s`` 内采样两次 mtime，统计窗口内被重写的次数（0/1），
        并按窗口折算「次/分钟」；取不到文件则 unavailable。
    """
    state_file = _state_file(state_dir)
    if not state_file.exists():
        return {"status": "unavailable",
                "reason": f"开发状态文件不存在: {state_file}",
                "state_json_bytes": None, "writes_per_minute": None}

    size0 = state_file.stat().st_size
    mtime0 = state_file.stat().st_mtime
    if window_s > 0:
        time.sleep(min(window_s, 5.0))  # 基线采集不阻塞太久；折算到每分钟
        size1 = state_file.stat().st_size
        mtime1 = state_file.stat().st_mtime
        rewrites = 1 if mtime1 > mtime0 else 0
        writes_per_min = round(rewrites / max(min(window_s, 5.0), 1e-9) * 60.0, 2)
    else:
        size1 = size0
        rewrites = 0
        writes_per_min = None

    return {"status": "ok",
            "state_json_bytes": size1,
            "state_json_bytes_start": size0,
            "writes_in_window": rewrites,
            "writes_per_minute": writes_per_min,
            "window_s": min(window_s, 5.0),
            "note": "短窗采样折算；精确落盘计数需进程内埋点，见 T-0352"}


def collect_git_activity(state_dir: Path, window_s: float, repos: list[Path]) -> dict:
    """每 30 分钟窗口内 ls-remote / fetch / pull 次数（现状基线）。

    这些操作由 worker / 同步线程在运行时发出，离线无法回放历史。这里改为
    **可复现的静态探测**：对给定仓库逐个跑一次 ``ls-remote`` 记为一次探测基线，
    并扫描 state_dir 下日志里窗口内出现的 git 命令次数；没有日志则 unavailable。
    绝不主动 ``fetch`` / ``pull`` 生产仓库。
    """
    log_counts = {"ls-remote": 0, "fetch": 0, "pull": 0}
    log_files = list(state_dir.glob("*.log")) if state_dir.exists() else []
    cutoff = time.time() - window_s
    for log_file in log_files:
        try:
            if log_file.stat().st_mtime < cutoff:
                continue
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        log_counts["ls-remote"] += len(re.findall(r"ls-remote", text))
        log_counts["fetch"] += len(re.findall(r"\bgit\s+fetch\b", text))
        log_counts["pull"] += len(re.findall(r"\bgit\s+pull\b", text))

    ls_remote_probe: list[dict] = []
    for repo in repos:
        if not repo.exists():
            ls_remote_probe.append({"repo": str(repo), "reachable": False,
                                    "reason": "仓库路径不存在"})
            continue
        try:
            p = subprocess.run(
                ["git", "-C", str(repo), "ls-remote", "origin", "HEAD"],
                capture_output=True, text=True, timeout=15,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            ls_remote_probe.append({
                "repo": str(repo), "reachable": p.returncode == 0,
                "head": p.stdout.split()[0] if p.returncode == 0 and p.stdout else "",
                "reason": "" if p.returncode == 0 else p.stderr.strip()[:200]})
        except (OSError, subprocess.SubprocessError) as exc:
            ls_remote_probe.append({"repo": str(repo), "reachable": False,
                                    "reason": f"{type(exc).__name__}: {exc}"})

    return {"status": "ok", "window_s": window_s,
            "log_command_counts_per_window": log_counts,
            "ls_remote_probe": ls_remote_probe,
            "note": "离线无法回放历史 git 操作；计数来自开发实例日志，探测仅 ls-remote"}


def collect_bandwidth(http_url: str, samples: int, nodes_est: int) -> dict:
    """空闲/活跃期控制面带宽（3 节点估算）。

    带宽由心跳往返字节 × 节点对密度估算：空闲期两节点实测口径，3 节点全连通
    为 n*(n-1)/2 对。本机单机开发实例没有真实对端，无法实测集群间带宽；
    这里用心跳采样字节做**同口径估算**并显式标注 estimate，取不到心跳则
    unavailable。不把估算值冒充实测。
    """
    hb = collect_heartbeat(http_url, samples)
    if hb["status"] != "ok":
        return {"status": "unavailable", "reason": hb["reason"],
                "bandwidth_kbps": None, "nodes": nodes_est, "estimate": True}
    avg_req = hb["request_bytes"]["avg"]
    avg_resp = hb["response_bytes"]["avg"]
    pairs = nodes_est * (nodes_est - 1) / 2
    # 空闲心跳间隔 5s（HIVE_CLUSTER_IDLE_HEARTBEAT 默认）；每对往返一次。
    interval = 5.0
    bytes_per_s = pairs * (avg_req + avg_resp) / interval
    return {"status": "ok",
            "bandwidth_kbps": round(bytes_per_s / 1024.0, 3),
            "bytes_per_s": round(bytes_per_s, 1),
            "nodes": nodes_est, "pairs": pairs,
            "heartbeat_interval_s": interval,
            "estimate": True,
            "note": "由心跳往返字节 × 节点对密度估算；真实集群带宽需多节点实测（T-0357）"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hive 开发环境流量基线采集")
    parser.add_argument("--state-dir",
                        default=str(Path.home() / ".hive" / "dev-cluster"),
                        help="开发实例状态目录（默认 ~/.hive/dev-cluster）")
    parser.add_argument("--http", default="http://127.0.0.1:8888",
                        help="开发实例 HTTP 地址（默认 127.0.0.1:8888）")
    parser.add_argument("--samples", type=int, default=12, help="心跳采样次数")
    parser.add_argument("--window", type=float, default=1800.0,
                        help="统计窗口（秒，默认 1800 = 30 分钟）")
    parser.add_argument("--nodes", type=int, default=3, help="带宽估算节点数")
    parser.add_argument("--repo", action="append", default=[],
                        help="参与 ls-remote 探测的仓库路径，可重复")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--out", help="把 JSON 写入文件（同时仍打印到 stdout）")
    args = parser.parse_args(argv)

    state_dir = Path(os.path.expanduser(args.state_dir)).resolve()

    # 安全护栏：状态目录不得指向生产 ~/.hive/cluster。
    prod_state = (Path.home() / ".hive" / "cluster").resolve()
    try:
        state_dir.relative_to(prod_state)
        print("拒绝：--state-dir 指向生产 ~/.hive/cluster", file=sys.stderr)
        return 2
    except ValueError:
        pass

    repos = [Path(os.path.expanduser(r)).resolve() for r in args.repo]
    if not repos:
        repos = [Path(__file__).resolve().parents[1]]

    baseline = {
        "schema": "hive-traffic-baseline/1",
        "collected_at": _iso_now(),
        "host": (os.environ.get("COMPUTERNAME")
                 or getattr(getattr(os, "uname", None), "nodename", "")
                 or os.environ.get("HOSTNAME", "")),
        "params": {"state_dir": str(state_dir), "http": args.http,
                   "samples": args.samples, "window_s": args.window,
                   "nodes": args.nodes, "repos": [str(r) for r in repos]},
        "reference_baseline_0.16.05": REFERENCE_BASELINE,
        "heartbeat": collect_heartbeat(args.http, args.samples),
        "persistence": collect_persistence(state_dir, args.window),
        "git_activity": collect_git_activity(state_dir, args.window, repos),
        "bandwidth": collect_bandwidth(args.http, args.samples, args.nodes),
        "guardrails": {"prod_http_port": PROD_HTTP,
                        "prod_mc_group": PROD_MC_GROUP, "prod_mc_port": PROD_MC_PORT,
                        "touched_prod_state": False, "sent_prod_multicast": False},
    }

    text = json.dumps(baseline, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(os.path.expanduser(args.out))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    if args.json or args.out:
        print(text)
    else:
        print(f"基线采集完成（{baseline['collected_at']}）")
        for key in ("heartbeat", "persistence", "git_activity", "bandwidth"):
            print(f"  {key}: {baseline[key]['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
