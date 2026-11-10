"""控制面共享骨架：条目模型 / 配置 / JSON 行编解码。"""

from __future__ import annotations

import json
import time

import pytest

from hive_control import config as control_config
from hive_control.model import IncarnationStore, StateEntry, is_newer
from hive_control.wire import decode_payload, dumps, entry_from_message, message_type


def entry(**kw) -> StateEntry:
    base = dict(
        project="proj",
        tid="T-0001",
        version=1,
        incarnation=7,
        owner_agent="agent-a",
        state="claimed",
        dna=False,
        updated_at=1_700_000_000_000,
    )
    base.update(kw)
    return StateEntry(**base)


def test_entry_roundtrip_and_key():
    e = entry(project="中文项目", urgent=True)
    line = e.to_line()
    assert "\n" not in line
    assert "\\u" not in line  # 中文不被转义成 \uXXXX
    back = StateEntry.from_line(line)
    assert back == e
    assert back.key == ("中文项目", "T-0001")
    assert back.transient is True
    assert back.steady is False


def test_version_compare_is_strict_lexicographic():
    old = entry(incarnation=2, version=99)
    new = entry(incarnation=3, version=0)
    assert is_newer(new, old) is True
    assert is_newer(old, new) is False
    assert is_newer(entry(incarnation=3, version=0), new) is False
    assert is_newer(new, None) is True


def test_bumped_increments_version_and_keeps_key():
    e = entry()
    nxt = e.bumped(state="steady", dna=True)
    assert nxt.version == e.version + 1
    assert nxt.updated_at >= e.updated_at
    assert nxt.key == e.key
    assert nxt.dna is True


@pytest.mark.parametrize("bad", [
    {"state": "unknown"},
    {"version": -1},
    {"incarnation": "x"},
    {"dna": "true"},
    {"project": ""},
])
def test_invalid_entry_rejected(bad):
    with pytest.raises(ValueError):
        entry(**bad)


def test_from_dict_requires_all_fields():
    data = entry().to_dict()
    for key in ("project", "tid", "version", "incarnation",
                "owner_agent", "state", "dna", "updated_at"):
        broken = dict(data)
        broken.pop(key)
        with pytest.raises(ValueError):
            StateEntry.from_dict(broken)


def test_incarnation_store_is_strictly_monotonic(tmp_path):
    path = tmp_path / "inc"
    first = IncarnationStore(path).next()
    second = IncarnationStore(path).next()
    third = IncarnationStore(path).next()
    assert second == first + 1
    assert third == second + 1
    assert int(path.read_text(encoding="utf-8")) == third


def test_incarnation_store_falls_back_when_unwritable(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    store = IncarnationStore(blocker / "inc")
    before = int(time.time())
    a = store.next()
    b = store.next()
    assert a >= before
    assert b > a


def test_dynamic_multicast_interval_boundaries():
    f = control_config.dynamic_multicast_interval
    assert f(0) == 2.0
    assert f(500) == 2.0
    assert f(501) == 1.0
    assert f(2000) == 1.0
    assert f(2001) == 0.5


def test_control_config_rejects_out_of_range_priority():
    with pytest.raises(ValueError):
        control_config.ControlConfig(leader_priority=256)
    with pytest.raises(ValueError):
        control_config.ControlConfig(lease_ttl=0)


def test_limits_override_keeps_other_defaults():
    limits = control_config.Limits().overridden(control_port=0, max_packet_bytes=900)
    assert limits.control_port == 0
    assert limits.max_packet_bytes == 900
    assert limits.heartbeat_timeout == 6.0


def test_wire_roundtrip_and_bad_lines_skipped():
    e = entry()
    payload = (dumps(e.to_dict()) + "\n\n{bad json\n").encode("utf-8")
    errors = []
    rows = decode_payload(payload, on_error=lambda line, exc: errors.append(line))
    assert rows == [e.to_dict()]
    assert errors == ["{bad json"]


def test_wire_message_dispatch():
    e = entry()
    assert message_type(e.to_dict()) == "entry"
    assert message_type({"type": "hello"}) == "hello"
    assert entry_from_message({"type": "entry", "entry": e.to_dict()}) == e
    assert entry_from_message({"type": "heartbeat"}) is None


def test_entry_line_is_compact_json():
    raw = entry().to_line()
    assert ", " not in raw and '": ' not in raw
    assert json.loads(raw)["tid"] == "T-0001"