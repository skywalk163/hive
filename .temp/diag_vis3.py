"""诊断3：溯源 c.progress 的落地调用栈。"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, r"C:\Users\skywalk\.hive\wt\teleagent-win\T-0356")
import hive_cluster as hc

CLUSTER_ID = "hive-itest-diag5"
os.environ["HIVE_CONTROL_MULTICAST_GROUP"] = "239.255.42.3"
os.environ["HIVE_CONTROL_MULTICAST_PORT"] = "47959"
os.environ["HIVE_CONTROL_PROJECT_TAG"] = "hive-itest-diag5"

tmp = Path(tempfile.mkdtemp(prefix="hlsa-diag5-"))

events = []
orig_apply = hc.Cluster._apply_lsa_diffs_locked

def counting(self, lsas):
    for x in lsas or []:
        events.append((round(time.monotonic() - T0, 2), self.config.name,
                       "apply-lsa", str(x.get("originator"))[:10], x.get("seq")))
    return orig_apply(self, lsas)

hc.Cluster._apply_lsa_diffs_locked = counting

orig_ap = hc.Cluster._apply_progress_locked

def ap_spy(self, payload, leader):
    tid = payload.get("task") or payload.get("tid")
    if tid == "T-IT1" and self.config.name == "c":
        stack = [f for f in traceback.extract_stack()
                 if "hive_cluster.py" in f.filename]
        tail = " <- ".join("%s:L%d" % (f.name, f.lineno) for f in stack[-4:])
        events.append((round(time.monotonic() - T0, 2), "c-apply-progress", tail))
    return orig_ap(self, payload, leader)

hc.Cluster._apply_progress_locked = ap_spy


def wait_until(fn, timeout=40.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def make(name, seeds):
    cfg = hc.Config(
        state_dir=tmp / name, name=name, host="127.0.0.1", port=0,
        advertise="127.0.0.1", discover=False, seeds=seeds,
        hive_dir=tmp / "repo", cluster_id=CLUSTER_ID,
        control_plane=False, heartbeat_interval=1.0,
        idle_heartbeat_interval=5.0,
        peer_timeout=10.0, dead_timeout=20.0,
        election_min=2.0, election_jitter=0.1,
        leader_lease=5.0, membership_ttl=10.0,
    )
    c = hc.Cluster(cfg)
    c.start()
    return c


a = make("a", [])
assert wait_until(lambda: a.role == "leader")
b = make("b", [a.url])
c = make("c", [a.url])
assert wait_until(lambda: b.leader_id == a.node_id and c.leader_id == a.node_id)

T0 = time.monotonic()
b.publish_progress({"task": "T-IT1", "agent": "b", "state": "running",
                    "progress": 33, "step": "集成测试"})

t_leader = None
t_c = None
deadline = time.monotonic() + 40
while time.monotonic() < deadline:
    if t_leader is None and (a.progress.get("T-IT1") or {}).get("progress") == 33:
        t_leader = time.monotonic() - T0
    if t_c is None and (c.progress.get("T-IT1") or {}).get("progress") == 33:
        t_c = time.monotonic() - T0
        break
    time.sleep(0.02)

print(f"leader 可见: {t_leader}, c 可见(progress): {t_c}")
print("c.leases T-IT1 keys:", sorted((c.leases.get("T-IT1") or {}).keys()))
print("events:")
for e in events[:30]:
    print("  ", e)
a.stop(); b.stop(); c.stop()
