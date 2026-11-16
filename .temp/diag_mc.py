"""快速诊断：控制面 T1 首拍时刻 + waiting 窗白盒提速是否有效。"""
import os
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\skywalk\.hive\wt\teleagent-win\T-0356")

import hive_cluster as hc

CLUSTER_ID = "hive-itest-diag"
os.environ["HIVE_CONTROL_MULTICAST_GROUP"] = "239.255.42.3"
os.environ["HIVE_CONTROL_MULTICAST_PORT"] = "47955"
os.environ["HIVE_CONTROL_PROJECT_TAG"] = "hive-itest-diag"

tmp = Path(tempfile.mkdtemp(prefix="hlsa-diag-"))
cfg = hc.Config(
    state_dir=tmp / "a", name="a", host="127.0.0.1", port=0,
    advertise="127.0.0.1", discover=False, seeds=[],
    hive_dir=tmp / "repo", cluster_id=CLUSTER_ID,
    control_plane=True, heartbeat_interval=0.3, idle_heartbeat_interval=0.3,
)
c = hc.Cluster(cfg)
c.start()
ad = c.control_adapter
print("adapter:", ad is not None, "plane:", ad.plane is not None,
      "available:", ad.plane_available)

plane = ad.plane
st = plane.status()
print("keys:", sorted(st.keys()))
print("role:", st.get("role"), "is_leader:", st.get("is_leader"))
ele = getattr(plane, "election", None)
print("has election:", ele is not None)
if ele is not None:
    print("election._waiting_until - now:",
          ele._waiting_until - time.monotonic())
    ele._waiting_until = time.monotonic() + 0.5
    print("-> moved to +0.5s")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("", 47955))
mreq = struct.pack("=4sl", socket.inet_aton("239.255.42.3"),
                   socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
sock.settimeout(0.5)
got = []
t0 = time.monotonic()
while time.monotonic() - t0 < 20:
    try:
        data, addr = sock.recvfrom(65535)
    except socket.timeout:
        continue
    got.append((round(time.monotonic() - t0, 1), len(data), addr))
st2 = plane.status()
print("packets:", got[:10])
print("role:", st2.get("role"), "is_leader:", st2.get("is_leader"))
tx = ad.status()["lsa"]["tx"]
print("lsa tx:", tx)
sock.close()
c.stop()
