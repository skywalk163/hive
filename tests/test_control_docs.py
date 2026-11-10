"""控制面运维手册的防漂移守卫（T-0314）。

`docs/控制面v0.5-运维.md` 是运维真正会照着操作的东西，最容易随代码演进而失真：
端口改了、状态字段改名、pull 接口换了路径，文档却还停在旧版。这里把
「必须有的小节 / 端口 / 接口 / 状态字段 / 互链」钉死，改代码时先让测试红。

只读文件、纯标准库，Windows / macOS / Linux 行为一致（不跑 shell）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "控制面v0.5-运维.md"
CHANGELOG = ROOT / "CHANGELOG.md"
CONTRACT = ROOT / "docs" / "控制面v0.5-实现约定.md"

REQUIRED_SECTIONS = [
    "## 1. 它是什么，和旧 HTTP 面什么关系",
    "## 2. 端口与套接字",
    "## 3. 状态 API",
    "## 4. 回退模式：组播发不出去时",
    "## 5. 灰度、关闭与回滚",
    "## 6. 常见故障",
    "## 7. 安全边界",
    "## 8. 变更记录",
]

# 端口/套接字：任何一项改名都必须同步改文档与测试
REQUIRED_PORTS = [
    "8788",
    "8789",
    "239.255.42.1",
    "47899",
    "/tmp/hive-control.sock",
    "hive-control.sock",
]

REQUIRED_APIS = [
    "/api/cluster/status",
    "/api/cluster/pull",
    "/api/cluster/pull/status",
]

# /api/cluster/status 的 control 段字段：改名 = 运维排障断链
REQUIRED_STATUS_FIELDS = [
    "plane_available",
    "degraded",
    "multicast_mode",
    "control_leader",
    "control_epoch",
    "control_port",
    "queues",
    "counters",
]

# 任务要求的故障条目
REQUIRED_TROUBLES = ["组播", "端口", "TTL", "control_leader", "retry"]

REQUIRED_SECURITY = ["局域网", "token 只保护 HTTP", "不替代 Git", "I1"]

PLACEHOLDERS = ("TODO", "待补", "占位")

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+\.md)\)")


def _text() -> str:
    assert DOC.exists(), f"缺少运维手册：{DOC}"
    return DOC.read_text(encoding="utf-8")


def test_doc_exists_and_nontrivial():
    text = _text()
    assert len(text) > 3000, f"文档过短（{len(text)} 字），可能被抽空"


def test_required_sections_in_order():
    text = _text()
    positions = []
    for section in REQUIRED_SECTIONS:
        index = text.find(section)
        assert index >= 0, f"缺少必备小节：{section}"
        positions.append(index)
    assert positions == sorted(positions), "必备小节顺序被打乱，按编号重排"


def test_ports_and_sockets_documented():
    text = _text()
    for token in REQUIRED_PORTS:
        assert token in text, f"端口/套接字未写入文档：{token}"
    # TCP 上行/拉取 = HTTP 端口 + 1，这条规则必须写清楚
    assert "HTTP 端口 + 1" in text or "HTTP 端口+1" in text, \
        "必须说明上行/拉取 TCP 端口 = HTTP 端口 + 1"


def test_http_api_paths_and_examples():
    text = _text()
    for path in REQUIRED_APIS:
        assert path in text, f"缺少接口说明：{path}"
    assert "curl" in text, "缺少可直接复制的 curl 示例"
    assert "202" in text, "pull 必须说明是异步 202 + request_id"
    assert "request_id" in text
    # token 两种写法都要给，免得运维只试一种
    assert "X-Hive-Cluster-Token" in text
    assert "Authorization: Bearer" in text


def test_status_fields_documented():
    text = _text()
    for field in REQUIRED_STATUS_FIELDS:
        assert field in text, f"status 字段未解释：{field}"
    # control_port 是 UDP 端口，不是 TCP 8789——这是最容易误读的一项
    assert "不是上行 TCP" in text or "不是 TCP" in text, \
        "必须澄清 control_port 不是上行 TCP 端口"


def test_fallback_has_trigger_confirm_recover():
    text = _text()
    section = text[text.find("## 4. 回退模式"):text.find("## 5. 灰度")]
    assert "触发条件" in section
    assert "如何确认" in section
    assert "如何恢复" in section
    assert "fallback" in section
    assert "30" in section, "必须写明 30s 无下行触发的窗口"
    assert "重启" in section, "必须说明回退不会自动切回、需重启"


def test_troubleshooting_covers_required_cases():
    text = _text()
    section = text[text.find("## 6. 常见故障"):text.find("## 7. 安全边界")]
    for keyword in REQUIRED_TROUBLES:
        assert keyword in section, f"常见故障缺少条目：{keyword}"


def test_security_boundaries_stated():
    text = _text()
    section = text[text.find("## 7. 安全边界"):text.find("## 8. 变更记录")]
    for keyword in REQUIRED_SECURITY:
        assert keyword in section, f"安全边界缺少关键表述：{keyword}"


def test_relative_links_exist():
    """互链目标必须真的存在，避免指向已改名的文档。"""
    for target in LINK_RE.findall(_text()):
        if "://" in target or target.startswith("#"):
            continue
        resolved = (DOC.parent / target).resolve()
        assert resolved.exists(), f"文档链接指向不存在的文件：{target}"


def test_no_placeholders_and_balanced_fences():
    text = _text()
    for word in PLACEHOLDERS:
        assert word not in text, f"文档里残留占位标记：{word}"
    fences = len(re.findall(r"^```", text, flags=re.MULTILINE))
    assert fences % 2 == 0, f"代码围栏没有配对（{fences} 个 ```）"


def test_contract_doc_is_linked():
    text = _text()
    assert CONTRACT.name in text, "必须链接到实现约定，便于对照字段与裁定"


def test_changelog_has_added_entry_before_docs():
    text = CHANGELOG.read_text(encoding="utf-8")
    added = text.find("### Added")
    docs = text.find("### Docs")
    assert added >= 0, "CHANGELOG 缺少 Added 段落"
    assert docs >= 0, "CHANGELOG 的 Docs 段落被误删"
    assert added < docs, "Added 段应插在 Unreleased 顶部、Docs 之前"
    assert "T-0314" in text, "CHANGELOG 缺少 T-0314 条目"
    assert "控制面v0.5-运维.md" in text, "CHANGELOG 应指向新增的运维手册"
