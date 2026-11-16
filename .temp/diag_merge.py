"""复现合并窗口测试，打印每条 applied LSA 的明细。"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\skywalk\.hive\wt\teleagent-win\T-0356")
import hive_cluster as hc

CLUSTER_ID = "hive-itest-diag2"
os.environ["HIVE_CONTROL_MULTICAST_GROUP"] = "239.255.42.3"
os.environ["HIVE_CONTROL_MULTICAST_PORT"] = "47956"
os.environ["HIVE_CONTROL_PROJECT_TAG"] = "hive-itest-diag2"

tmp = Path(tempfile.mkdtemp(prefix="hlsa-diag2-"))

applied = []
orig_apply = hc.Cluster._apply_lsa_diffs_locked

def counting(self, lsas):
    for x in lsas or []:
        applied.append((round(time.monotonic() - T0, 2),
                        str(x.get("originator"))[:12], x.get("seq"),
                        sorted((x.get("diff") or {}).keys())))
    return orig_apply(self, lsas)

hc.Cluster._apply_lsa_diffs_locked = counting

def wait_until(fn, timeout=30.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False

from hive_control.config import Limits

def make(name):
    cfg = hc.Config(
        state_dir=tmp / name, name=name, host="127.0.0.1", port=0,
        advertise="127.0.0.1", discover=False, seeds=[],
        hive_dir=tmp / "repo", cluster_id=CLUSTER_ID,
        control_plane=False, heartbeat_interval=1.0,
        idle_heartbeat_interval=1.0,
        peer_timeout=10.0, dead_timeout=20.0,
        election_min=2.0, election_jitter=0.1,
        leader_lease=5.0, membership_ttl=10.0,
    )
    c = hc.Cluster(cfg)
    c.start()
    return c

a = make("ma")
assert wait_until(lambda: a.role == "leader")
b = make("mb", seeds=[a.url]) if False else None
# seeds 传不进去就手动
b_cfg = hc.Config(
    state_dir=tmp / "mb", name="mb", host="127.0.0.1", port=0,
    advertise="127.0.0.1", discover=False, seeds=[a.url],
    hive_dir=tmp / "repo", cluster_id=CLUSTER_ID,
    control_plane=False, heartbeat_interval=1.0,
    idle_heartbeat_interval=1.0,
    peer_timeout=10.0, dead_timeout=20.0,
    election_min=2.0, election_jitter=0.1,
    leader_lease=5.0, membership_ttl=10.0,
)
b = hc.Cluster(b_cfg)
b.start()
assert wait_until(lambda: b.leader_id == a.node_id)

T0 = time.monotonic()
b.publish_progress({"task": "T-IT3", "agent": "b", "state": "running",
                    "progress": 10, "step": "s"})
assert wait_until(lambda: (a.progress.get("T-IT3") or {}).get("progress") == 10)
print("wave1 converged at", round(time.monotonic() - T0, 2))
print("b local rec keys:", sorted(b.progress["T-IT3"].keys()))
applied.clear()

for progress in (20, 25, 30):
    b.publish_progress({"task": "T-IT3", "agent": "b", "state": "running",
                        "progress": progress, "step": "s"})
assert wait_until(lambda: (a.progress.get("T-IT3") or {}).get("progress") == 30)
time.sleep(2.5)
print("wave2 applied LSAs:")
for row in applied:
    print("  ", row)
with b.lock:
    print("b pending:", b._lsa.status().get("pending"))
a.stop(); b.stop()
