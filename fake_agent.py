#!/usr/bin/env python3
"""一个假的 agent：睡一会儿、写个文件、写 summary，用来做端到端自测。"""
import os
import pathlib
import time

time.sleep(float(os.environ.get("FAKE_SLEEP", "2")))
tid = os.environ.get("HIVE_TASK", "?")
who = os.environ.get("HIVE_AGENT", "?")

pathlib.Path("feature.txt").write_text(f"made by {who} for {tid}\n", encoding="utf-8")
pathlib.Path(".hive").mkdir(exist_ok=True)
pathlib.Path(".hive/summary.md").write_text(
    f"# {tid} 完成\n\n- 执行者：{who}\n- 新增 feature.txt\n- 验证：文件存在\n",
    encoding="utf-8",
)
print(f"fake agent {who} finished {tid}")
