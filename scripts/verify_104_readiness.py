#!/usr/bin/env python3
"""hive 集群拓扑体检（T-0288）。

原文件是 T-0283 的一次性验收：写死原 104 节点的 IP，并断言「节点表恰好 2 个 URL」。
那张卡的前提已失效（原机改名换 IP，node id 仍沿用旧值），判据全部过期，于是在
今天的集群上必然三条全红。本文件改成通用体检工具：

- 不写死任何节点地址，目标从 `/api/cluster/health` 与 `state.json` 动态读取，
  可用 `--leader-url` / `--state` 指定来源；两处都读不到就明确报错，不猜。
- 输出结构化 JSON：term / role / peers / quorum / leader / nodes /
  dead_nodes / duplicate_ids / duplicate_urls / ghost_leader / todos。
- 未闭环的治理项**如实列进 todos**，不把没解决的问题写成通过。
- `--self-test` 只用内置离线夹具，不联网、不读本机 `~/.hive/cluster`。

用法：
    python3 scripts/verify_104_readiness.py
    python3 scripts/verify_104_readiness.py --leader-url http://<host>:8788 --state <path>
    python3 scripts/verify_104_readiness.py --roster <ids.txt>   # 只把清单内的 id 当正式节点
    python3 scripts/verify_104_readiness.py --self-test
    python3 -m pytest scripts/verify_104_readiness.py -q -o addopts=   # 只跑离线判据

退出码：0 = 采集成功（哪怕 todos 非空）；1 = 采集失败或 --self-test 判据不自洽；
`--strict` 时 todos 非空也返回 1。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_LEADER_URL = "http://127.0.0.1:8788"
DEFAULT_STATE = Path.home() / ".hive" / "cluster" / "state.json"
DEFAULT_DEAD_AFTER_S = 600.0


# --- 采集 ------------------------------------------------------------------ #

def fetch_json(url: str, timeout: float = 3.0) -> dict:
    """GET 一个 JSON 接口；失败直接抛异常，由调用方决定怎么报错。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 只读探测
        return json.loads(resp.read().decode("utf-8"))


def load_state(path: Path | str) -> dict:
    """读 state.json；贴心地兼容 `{"state": {...}}` 包装。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _unwrap_state(data)


def _unwrap_state(data: dict) -> dict:
    if isinstance(data, dict) and isinstance(data.get("state"), dict):
        return data["state"]
    return data if isinstance(data, dict) else {}


# --- 判据 ------------------------------------------------------------------ #

def parse_ts(value) -> float | None:
    """把 ISO8601 / epoch 转成时间戳；解析不了返回 None（不瞎猜）。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # 无时区按本机时间处理
        dt = dt.astimezone()
    return dt.timestamp()


def _norm_url(url) -> str:
    return str(url or "").strip().rstrip("/")


def normalize_nodes(raw) -> list[dict]:
    """节点表既可能是 id->node 的 dict，也可能是 list，统一成按 id 排序的 list。"""
    if not raw:
        return []
    if isinstance(raw, dict):
        items = list(raw.values())
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError(f"节点表格式不认识：{type(raw).__name__}")
    nodes = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nodes.append({
            "id": str(item.get("id") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "url": _norm_url(item.get("url")),
            "host": str(item.get("host") or "").strip(),
            "hostname": str(item.get("hostname") or "").strip(),
            "status": item.get("status"),
            "last_seen": item.get("last_seen"),
        })
    nodes.sort(key=lambda n: n["id"])
    return nodes


def find_dead_nodes(nodes: list[dict], now: float, max_age_s: float,
                    exclude_ids: set[str] | None = None) -> list[dict]:
    """last_seen 超过阈值的节点。leader 由调用方决定是否排除（见 leader_record_stale）。"""
    exclude = exclude_ids or set()
    dead = []
    for node in nodes:
        ts = parse_ts(node.get("last_seen"))
        if ts is None or node["id"] in exclude:
            continue
        age = now - ts
        if age > max_age_s:
            dead.append({"id": node["id"], "url": node["url"],
                         "last_seen": node["last_seen"], "age_s": round(age, 1)})
    return dead


def find_duplicate_ids(nodes: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        if node["id"]:
            counts[node["id"]] += 1
    return {nid: n for nid, n in counts.items() if n > 1}


def find_duplicate_urls(nodes: list[dict]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if node["url"]:
            groups[node["url"]].append(node["id"])
    return {url: ids for url, ids in groups.items() if len(ids) > 1}


def _short_host(hostname) -> str:
    text = str(hostname or "").strip().rstrip(".")
    return text.split(".")[0].lower()


def find_identity_mismatches(nodes: list[dict]) -> list[dict]:
    """node name 与机器上报的 hostname 不一致 —— 改名/换 IP 后的典型口径残留。"""
    out = []
    for node in nodes:
        short = _short_host(node.get("hostname"))
        name = str(node.get("name") or "").strip().lower()
        if short and name and short != name:
            out.append({
                "id": node["id"],
                "name": node["name"],
                "hostname": node["hostname"],
                "url": node["url"],
                "detail": f"节点名 {node['name']} 与机器名 {node['hostname']} 不一致",
            })
    return out


def check_ghost_leader(leader_id, leader_url, nodes: list[dict],
                       roster: set[str] | None = None) -> dict:
    """幽灵 leader = leader 身份对不上节点表（或对不上正式登记清单）。

    结构性幽灵（id 不在表中 / URL 与表中该 id 不一致）是硬判据；
    `roster` 是「已知真实节点清单」，给了才检查 leader 是否在册。
    """
    by_id = {n["id"]: n for n in nodes}
    in_table = bool(leader_id) and leader_id in by_id
    url_consistent = None
    if in_table:
        url_consistent = _norm_url(leader_url) == _norm_url(by_id[leader_id]["url"])
    registered = None if roster is None else bool(leader_id) and leader_id in roster

    reasons = []
    if not leader_id:
        reasons.append("当前没有 leader")
    elif not in_table:
        reasons.append("leader 不在节点表中")
    elif not url_consistent:
        reasons.append("leader 的 URL 与节点表中的记录不一致")
    if registered is False:
        reasons.append("leader 不在正式登记清单中")

    return {
        "id": leader_id,
        "url": _norm_url(leader_url),
        "in_node_table": in_table,
        "url_consistent": url_consistent,
        "registered": registered,
        "is_ghost": bool(reasons),
        "reasons": reasons,
    }


def build_report(health: dict, state: dict, *, now: float | None = None,
                 dead_after_s: float = DEFAULT_DEAD_AFTER_S,
                 roster: set[str] | None = None,
                 source: dict | None = None) -> dict:
    """把 health + state 折算成一份结构化体检报告（纯函数，可离线测试）。"""
    now = time.time() if now is None else now
    state = _unwrap_state(state)
    nodes = normalize_nodes(state.get("nodes"))

    leader = health.get("leader") if isinstance(health.get("leader"), dict) else {}
    leader_id = (leader.get("id") or health.get("leader_id")
                 or state.get("leader_id") or "")
    leader_url = (leader.get("url") or health.get("leader_url")
                  or state.get("leader_url") or "")

    term = health.get("term", state.get("term"))
    role = health.get("role") or state.get("role") or "unknown"

    peers = health.get("peers")
    quorum = health.get("quorum")
    derived = False
    if peers is None:
        peers = max(len(nodes) - 1, 0)
        derived = True
    if quorum is None:
        quorum = peers // 2 + 1
        derived = True

    enriched = []
    for node in nodes:
        ts = parse_ts(node.get("last_seen"))
        age = None if ts is None else round(now - ts, 1)
        enriched.append({**node, "age_s": age,
                         "alive": (age is not None and age <= dead_after_s)})

    dead = find_dead_nodes(nodes, now, dead_after_s, exclude_ids={leader_id})
    duplicate_ids = find_duplicate_ids(nodes)
    duplicate_urls = find_duplicate_urls(nodes)
    identity_mismatches = find_identity_mismatches(nodes)
    ghost = check_ghost_leader(leader_id, leader_url, nodes, roster)

    leader_node = next((n for n in enriched if n["id"] == leader_id), None)
    leader_stale = None
    if leader_node and leader_node["age_s"] is not None and leader_node["age_s"] > dead_after_s:
        leader_stale = {"id": leader_id, "last_seen": leader_node["last_seen"],
                        "age_s": leader_node["age_s"]}

    todos = []
    if ghost["is_ghost"]:
        todos.append({"kind": "ghost_leader", "target": ghost["id"],
                      "detail": "；".join(ghost["reasons"])})
    for item in dead:
        todos.append({"kind": "dead_node", "target": item["id"],
                      "detail": f"last_seen={item['last_seen']}，已 {item['age_s']}s 未上报"})
    if leader_stale:
        todos.append({"kind": "leader_record_stale", "target": leader_stale["id"],
                      "detail": (f"leader 自身 last_seen={leader_stale['last_seen']}，"
                                 f"已 {leader_stale['age_s']}s 未刷新（需人工确认是否实现细节）")})
    for nid, count in sorted(duplicate_ids.items()):
        todos.append({"kind": "duplicate_node_id", "target": nid,
                      "detail": f"节点表里出现 {count} 次"})
    for url, ids in sorted(duplicate_urls.items()):
        todos.append({"kind": "duplicate_node_url", "target": url,
                      "detail": "同一 URL 被多个身份占用：" + ", ".join(sorted(ids))})
    for item in identity_mismatches:
        todos.append({"kind": "identity_mismatch", "target": item["id"],
                      "detail": item["detail"]})

    return {
        "ok": not todos,
        "generated_at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "source": source or {},
        "thresholds": {"dead_after_s": dead_after_s},
        "term": term,
        "role": role,
        "peers": peers,
        "quorum": quorum,
        "peers_derived": derived,
        "leader_id": leader_id,
        "leader_url": _norm_url(leader_url),
        "nodes": enriched,
        "dead_nodes": dead,
        "leader_record_stale": leader_stale,
        "duplicate_ids": duplicate_ids,
        "duplicate_urls": duplicate_urls,
        "ghost_leader": ghost,
        "identity_mismatches": identity_mismatches,
        "todos": todos,
    }


# --- 离线自检 -------------------------------------------------------------- #

def _node(nid: str, *, name: str | None = None, url: str | None = None,
          hostname: str | None = None, last_seen: float | None = None,
          now: float = 0.0) -> dict:
    seen = (datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
            if last_seen is None else
            datetime.fromtimestamp(last_seen).astimezone().isoformat(timespec="seconds"))
    return {"id": nid, "name": name or nid, "url": url or f"http://10.0.0.1:8788",
            "hostname": hostname or (name or nid), "host": "10.0.0.1",
            "last_seen": seen, "status": "alive"}


def _fixture_nodes(now: float) -> list[dict]:
    return [
        _node("node-a", name="node-a", url="http://10.0.0.1:8788", now=now),
        _node("node-b", name="node-b", url="http://10.0.0.2:8788", now=now),
        _node("node-c", name="node-c", url="http://10.0.0.3:8788", now=now),
        _node("node-d", name="node-d", url="http://10.0.0.4:8788", now=now),
    ]


def _fixture_health(leader_id: str, term: int = 7, peers: int = 3, quorum: int = 2) -> dict:
    return {"ok": True, "term": term, "role": "follower", "peers": peers,
            "quorum": quorum, "leader": {"id": leader_id, "name": leader_id,
                                         "url": "http://10.0.0.1:8788"}}


def run_self_test(verbose: bool = True) -> int:
    """只用内置夹具跑一遍全部判据；退出码 0 表示逻辑自洽。"""
    now = 1_800_000_000.0
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    nodes = _fixture_nodes(now)
    state = {"term": 7, "nodes": nodes}

    report = build_report(_fixture_health("node-a"), state, now=now)
    add("正常 4 节点无 todo", report["ok"] and not report["todos"], str(report["todos"]))
    add("peers/quorum 取 health", (report["peers"], report["quorum"]) == (3, 2))
    add("冗余字段齐全",
        all(k in report for k in ("term", "role", "nodes", "dead_nodes", "duplicate_ids",
                                  "duplicate_urls", "ghost_leader", "todos")))

    ghost = build_report(_fixture_health("ghost-x"), state, now=now)
    add("幽灵 leader 被识别",
        ghost["ghost_leader"]["is_ghost"] and
        any(t["kind"] == "ghost_leader" for t in ghost["todos"]),
        str(ghost["ghost_leader"]["reasons"]))

    stale_nodes = _fixture_nodes(now)
    stale_nodes[1]["last_seen"] = datetime.fromtimestamp(now - 7200).astimezone().isoformat()
    stale = build_report(_fixture_health("node-a"), {"nodes": stale_nodes}, now=now)
    add("dead 节点被识别",
        [d["id"] for d in stale["dead_nodes"]] == ["node-b"],
        str(stale["dead_nodes"]))

    leader_stale_nodes = _fixture_nodes(now)
    leader_stale_nodes[0]["last_seen"] = datetime.fromtimestamp(now - 7200).astimezone().isoformat()
    ls = build_report(_fixture_health("node-a"), {"nodes": leader_stale_nodes}, now=now)
    add("leader 自记录陈旧单独记 todo",
        ls["leader_record_stale"] is not None and
        all(d["id"] != "node-a" for d in ls["dead_nodes"]) and
        any(t["kind"] == "leader_record_stale" for t in ls["todos"]))

    dup_id_nodes = _fixture_nodes(now) + [_node("node-a", url="http://10.0.0.9:8788", now=now)]
    dup_id = build_report({"term": 7, "leader": {"id": "node-a", "url": "http://10.0.0.1:8788"}},
                          {"nodes": dup_id_nodes}, now=now)
    add("重复 node id 被识别", dup_id["duplicate_ids"].get("node-a") == 2,
        str(dup_id["duplicate_ids"]))

    dup_url_nodes = _fixture_nodes(now)
    dup_url_nodes[1]["url"] = dup_url_nodes[0]["url"]
    dup_url = build_report(_fixture_health("node-a"), {"nodes": dup_url_nodes}, now=now)
    add("重复 node url 被识别", bool(dup_url["duplicate_urls"]), str(dup_url["duplicate_urls"]))

    ident_nodes = _fixture_nodes(now)
    ident_nodes[2]["name"] = "DESKTOP-OLD"
    ident_nodes[2]["hostname"] = "win10"
    ident = build_report(_fixture_health("node-a"), {"nodes": ident_nodes}, now=now)
    add("节点名与机器名不一致被识别",
        [m["id"] for m in ident["identity_mismatches"]] == ["node-c"], str(ident["identity_mismatches"]))

    unreg = build_report(_fixture_health("node-a"), state, now=now,
                         roster={"node-b", "node-c", "node-d"})
    add("roster 未登记 leader 被判幽灵",
        unreg["ghost_leader"]["is_ghost"] and unreg["ghost_leader"]["registered"] is False)

    url_mismatch = build_report(_fixture_health("node-a") | {"leader": {
        "id": "node-a", "url": "http://10.0.0.99:8788"}}, state, now=now)
    add("leader URL 与节点表不一致被识别", url_mismatch["ghost_leader"]["is_ghost"])

    list_state = build_report(_fixture_health("node-a"), {"nodes": nodes}, now=now)
    add("list 形态节点表可用", len(list_state["nodes"]) == 4)

    failures = [c for c in checks if not c["ok"]]
    if verbose:
        print(json.dumps({"self_test": True, "total": len(checks),
                          "passed": len(checks) - len(failures),
                          "failed": len(failures), "checks": checks},
                         ensure_ascii=False, indent=2))
    return 1 if failures else 0


def test_self_test_offline() -> None:
    """pytest 收集入口：只跑离线判据，绝不联网、不读真机 state。"""
    assert run_self_test(verbose=False) == 0


# --- CLI ------------------------------------------------------------------- #

def _load_roster(value: str | None) -> set[str] | None:
    if not value:
        return None
    path = Path(value)
    if path.exists():
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
        return {line for line in lines if line and not line.startswith("#")}
    return {part.strip() for part in value.split(",") if part.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hive 集群拓扑体检（只读）")
    parser.add_argument("--leader-url", default=DEFAULT_LEADER_URL,
                        help="集群 HTTP 地址，默认本机 8788")
    parser.add_argument("--state", default=str(DEFAULT_STATE),
                        help="state.json 路径，默认 ~/.hive/cluster/state.json")
    parser.add_argument("--dead-after", type=float, default=DEFAULT_DEAD_AFTER_S,
                        help="last_seen 超过多少秒算 dead（默认 600）")
    parser.add_argument("--roster", default=None,
                        help="正式登记节点 id 清单：逗号分隔，或每行一个 id 的文件")
    parser.add_argument("--strict", action="store_true", help="todos 非空时返回 1")
    parser.add_argument("--self-test", action="store_true", help="只跑离线夹具自检")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test(verbose=True)

    problems: list[str] = []
    health: dict = {}
    url = args.leader_url.rstrip("/") + "/api/cluster/health"
    try:
        health = fetch_json(url)
    except Exception as exc:  # noqa: BLE001 采集失败要如实报，不猜
        problems.append(f"读不到 health（{url}）：{exc}")

    state: dict = {}
    try:
        state = load_state(args.state)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"读不到 state（{args.state}）：{exc}")

    if not health and not state:
        print("错误: 两个来源都读不到，不做猜测 —— " + "；".join(problems), file=sys.stderr)
        return 1

    report = build_report(
        health, state, dead_after_s=args.dead_after,
        roster=_load_roster(args.roster),
        source={"leader_url": args.leader_url, "state": str(args.state),
                "problems": problems},
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if problems:
        print("警告: " + "；".join(problems), file=sys.stderr)
        return 1
    if args.strict and report["todos"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
