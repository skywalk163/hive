#!/usr/bin/env python3
"""P1：hived MCP 的本机 token 管理。

token 只存在本机（默认 ``~/.hive/cluster/mcp_tokens.json``，0600），
不进协调仓，也不随 Git 同步。文件里存 SHA-256，不存明文；一个 agent 一个 token。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path.home() / ".hive" / "cluster" / "mcp_tokens.json"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def token_path(path: str | os.PathLike | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    env = (os.environ.get("HIVE_MCP_TOKENS") or "").strip()
    return Path(env).expanduser() if env else DEFAULT_PATH.expanduser()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load(path: str | os.PathLike | None = None) -> dict:
    p = token_path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"version": 1, "tokens": {}}
    if not isinstance(data, dict):
        return {"version": 1, "tokens": {}}
    data.setdefault("version", 1)
    if not isinstance(data.get("tokens"), dict):
        data["tokens"] = {}
    return data


def save(data: dict, path: str | os.PathLike | None = None) -> None:
    p = token_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)


def enroll(agent: str, role: str = "worker",
           path: str | os.PathLike | None = None) -> str:
    """给 agent 生成新 token；同一 agent 的旧 token 自动吊销。"""
    agent = agent.strip()
    if not agent:
        raise ValueError("agent 不能为空")
    if role not in ("worker", "controller"):
        role = "worker"
    token = secrets.token_urlsafe(32)
    data = load(path)
    tokens = data.setdefault("tokens", {})
    # 一个 agent 只保留一个有效 token：旧的全部删掉
    for key in [k for k, v in tokens.items()
                if isinstance(v, dict) and v.get("agent") == agent]:
        tokens.pop(key, None)
    tokens[_hash(token)] = {"agent": agent, "role": role, "created": _now()}
    save(data, path)
    return token


def revoke(agent: str, path: str | os.PathLike | None = None) -> int:
    agent = agent.strip()
    data = load(path)
    tokens = data.setdefault("tokens", {})
    removed = 0
    for key in [k for k, v in tokens.items()
                if isinstance(v, dict) and v.get("agent") == agent]:
        tokens.pop(key, None)
        removed += 1
    if removed:
        save(data, path)
    return removed


def resolve(token: str, path: str | os.PathLike | None = None) -> dict | None:
    if not token:
        return None
    data = load(path)
    rec = (data.get("tokens") or {}).get(_hash(token))
    if not isinstance(rec, dict) or not rec.get("agent"):
        return None
    return {"agent": str(rec["agent"]),
            "role": str(rec.get("role") or "worker"),
            "created": rec.get("created") or ""}


def config_snippet(token: str, url: str = "http://127.0.0.1:8790/mcp") -> dict:
    return {
        "mcpServers": {
            "hive": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
