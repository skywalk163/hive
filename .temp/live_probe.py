"""活体取证：dev 实例 (8888) 的 T1 摘要内容是否随 T0 变更变化。

被动加入 dev 组播组 239.255.42.2:47900（只收不发），先抓一个基线摘要，
再向 dev 实例 POST /api/cluster/push 注入一次 T0 进度变更，
等下一个 T1 摘要（60s 节拍）后比对 n_changes / digest。
"""
import json
import socket
import struct
import sys
import time
import urllib.request

sys.path.insert(0, r"C:\Users\skywalk\.hive\wt\teleagent-win\T-0356")

GROUP, PORT = "239.255.42.2", 47900
BASE = "http://127.0.0.1:8888"


def listen(seconds: float) -> list[dict]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", PORT))
    mreq = struct.pack("=4sl", socket.inet_aton(GROUP), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(0.5)
    digests = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            continue
        try:
            msg = json.loads(data.decode("utf-8").strip())
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(msg, dict) and msg.get("type") == "lsa_digest":
            digests.append({"at": round(time.monotonic() - t0, 1),
                            "len": len(data),
                            "n_changes": msg.get("n_changes"),
                            "digest": str(msg.get("digest"))[:16],
                            "head_sha": str(msg.get("head_sha"))[:12],
                            "seq": msg.get("seq"),
                            "area": msg.get("area"),
                            "project_tag": msg.get("project_tag"),
                            "cluster_id": msg.get("cluster_id")})
    s.close()
    return digests


print("== 阶段1：抓基线摘要（最多 75s，等 1~2 个 T1 拍） ==")
base = listen(75)
for d in base:
    print("  ", d)

print("== 注入 T0 变更：POST /api/cluster/push ==")
payload = json.dumps({
    "task": "T-0356-LIVE-PROBE", "agent": "teleagent-win",
    "state": "running", "progress": 50, "step": "活体验收探针",
}).encode("utf-8")
req = urllib.request.Request(BASE + "/api/cluster/push", data=payload,
                              headers={"Content-Type": "application/json"},
                              method="POST")
with urllib.request.urlopen(req, timeout=5) as resp:
    print("  push ->", resp.status, resp.read().decode("utf-8")[:120])

print("== 阶段2：等下一个 T1 摘要（75s） ==")
after = listen(75)
for d in after:
    print("  ", d)

base_nc = [d["n_changes"] for d in base]
after_nc = [d["n_changes"] for d in after]
print(f"== 结论：基线 n_changes={base_nc}；注入后 n_changes={after_nc} ==")
if after_nc and all(n == 0 for n in after_nc):
    print("D2 证据成立：T0 变更未反映到 T1 组播摘要（摘要源是未被喂过的模块单例）")
