"""控制面 JSON 行编解码。

所有控制面报文都是一行一个 JSON 对象，UTF-8，紧凑分隔符。
对应实现约定 §2.2。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Iterator

from .model import StateEntry

__all__ = [
    "dumps",
    "encode_line",
    "parse_line",
    "iter_lines",
    "decode_payload",
    "message_type",
    "entry_from_message",
]


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def encode_line(obj: Any) -> bytes:
    return dumps(obj).encode("utf-8") + b"\n"


def parse_line(line: str | bytes) -> dict:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("控制面报文顶层必须是 JSON 对象")
    return data


def iter_lines(text: str | bytes) -> Iterator[str]:
    """按 ``\\n`` 切行，忽略空行；兼容 CRLF。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            yield line


def decode_payload(
    payload: str | bytes,
    on_error: Callable[[str, Exception], None] | None = None,
) -> list[dict]:
    """解析一整段 JSON 行；坏行交给 ``on_error`` 后跳过。"""
    out: list[dict] = []
    for line in iter_lines(payload):
        try:
            out.append(parse_line(line))
        except (ValueError, json.JSONDecodeError) as exc:
            if on_error is not None:
                on_error(line, exc)
    return out


def message_type(message: dict) -> str:
    """取报文类型；没有 type 但有 project/tid 的按 entry。"""
    kind = message.get("type") or message.get("kind")
    if kind:
        return str(kind)
    if "project" in message and "tid" in message:
        return "entry"
    return ""


def entry_from_message(message: dict) -> StateEntry | None:
    """从 entry 信封 / 裸条目中提取 ``StateEntry``；不是条目返回 None。"""
    if not isinstance(message, dict):
        return None
    kind = message_type(message)
    if kind not in ("", "entry"):
        return None
    body = message.get("entry") if kind == "entry" else message
    if not isinstance(body, dict):
        return None
    try:
        return StateEntry.from_dict(body)
    except ValueError:
        return None