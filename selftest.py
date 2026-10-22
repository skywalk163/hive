#!/usr/bin/env python3
"""hive 端到端自测：两个 worker 抢同一张任务卡，只能有一个赢。

跑法：python3 selftest.py
需要：git、python3；不联网。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HIVE = HERE / "hive.py"
FAKE = HERE / "fake_agent.py"
PY = sys.executable


def sh(cmd, cwd=None, check=True, env=None):
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True,
                       capture_output=True, shell=isinstance(cmd, str), env=env)
    if check and p.returncode != 0:
        raise SystemExit(f"FAIL: {cmd}\n{p.stdout}\n{p.stderr}")
    return p


def hive(root, *args, **kw):
    return sh([PY, str(HIVE), "--dir", str(root), *args], **kw)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hive-selftest-"))
    print(f"临时目录: {tmp}")
    git_env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    # 1. 代码仓库（模拟 light）
    code_origin = tmp / "light.git"
    sh(["git", "init", "--bare", "-b", "main", str(code_origin)])
    code_seed = tmp / "light-seed"
    sh(["git", "init", "-b", "main", str(code_seed)])
    (code_seed / "README.md").write_text("# light\n", encoding="utf-8")
    sh(["git", "add", "-A"], cwd=code_seed)
    sh(["git", "commit", "-m", "init"], cwd=code_seed, env={**os.environ, **git_env})
    sh(["git", "remote", "add", "origin", str(code_origin)], cwd=code_seed)
    sh(["git", "push", "-u", "origin", "main"], cwd=code_seed)

    # 2. 协调仓库
    hive_origin = tmp / "hive.git"
    sh(["git", "init", "--bare", "-b", "main", str(hive_origin)])
    hive1 = tmp / "hive-a1"
    hive2 = tmp / "hive-a2"
    hive(hive1, "init", "--remote", str(hive_origin))
    sh(["git", "clone", "-q", str(hive_origin), str(hive2)])

    # 3. 两个 agent 各自一份代码克隆
    code = {}
    for name in ("a1", "a2"):
        code[name] = tmp / f"light-{name}"
        sh(["git", "clone", "-q", str(code_origin), str(code[name])])
        hive(hive1, "agent", "add", "--name", name,
             "--command", f"{PY} {FAKE}",
             "--repo", f"light={code[name]}")

    # 4. 一张任务卡，同时丢进两个收件箱（制造争抢）
    verify = f'{PY} -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\'feature.txt\').exists() else 1)"'
    hive(hive1, "task", "new", "--id", "T-0001", "--title", "给 light 加个特性",
         "--repo", "light", "--paths", "src/", "--verify", verify,
         "--body", "写一个 feature.txt，内容随便。")
    hive(hive1, "assign", "T-0001", "--agent", "a1")
    hive(hive1, "assign", "T-0001", "--agent", "a2")

    # 5. 两个 worker 同时跑
    env = {**os.environ, "FAKE_SLEEP": "2"}
    procs = []
    for name, root in (("a1", hive1), ("a2", hive2)):
        procs.append(subprocess.Popen(
            [PY, str(HIVE), "--dir", str(root), "work", "--agent", name, "--once",
             "--state-dir", str(tmp / f"state-{name}")],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env))
    outs = [p.communicate()[0] for p in procs]
    for name, out in zip(("a1", "a2"), outs):
        print(f"--- worker {name} ---\n{out}")

    # 6. 断言
    claims = sh(["git", "--git-dir", str(hive_origin), "for-each-ref",
                 "--format=%(refname)", "refs/heads/claim/"]).stdout.split()
    results = sh(["git", "--git-dir", str(hive_origin), "for-each-ref",
                  "--format=%(refname)", "refs/heads/res/"]).stdout.split()
    assert len(claims) == 1, f"claim 分支应恰好 1 个，实际 {claims}"
    assert len(results) == 1, f"result 分支应恰好 1 个，实际 {results}"

    status = json.loads(hive(hive1, "status", "--json").stdout)
    assert status[0]["result"] == "done", status
    assert status[0]["state"] == "assigned", status

    code_refs = sh(["git", "--git-dir", str(code_origin), "for-each-ref",
                    "--format=%(refname)", "refs/heads/task/"]).stdout.split()
    assert code_refs == ["refs/heads/task/T-0001"], code_refs
    feature = sh(["git", "--git-dir", str(code_origin), "show",
                  "task/T-0001:feature.txt"]).stdout
    assert "made by" in feature, feature

    winner = json.loads(sh(["git", "--git-dir", str(hive_origin), "show",
                            "res/T-0001:results/T-0001.json"]).stdout)
    assert winner["status"] == "done" and winner["verify"][0]["code"] == 0, winner
    print(f"\n胜者: {winner['agent']}；失败方干净退出（没有重复执行）")

    # 7. 直接对撞 claim：两个进程同时抢 T-0002，只能有一个 exit 0
    hive(hive1, "task", "new", "--id", "T-0002", "--title", "抢锁测试",
         "--repo", "light", "--body", "仅测试领取。")
    procs = [subprocess.Popen(
        [PY, str(HIVE), "--dir", str(root), "claim", "T-0002", "--agent", name],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for name, root in (("a1", hive1), ("a2", hive2))]
    codes = [p.wait() for p in procs]
    assert sum(1 for c in codes if c == 0) == 1, f"claim 应恰好一个成功: {codes}"
    claimer = json.loads(sh(["git", "--git-dir", str(hive_origin), "show",
                             "claim/T-0002:claims/T-0002.json"]).stdout)["agent"]
    print(f"claim 对撞：{codes}，胜者 {claimer}")

    # 8. verify-only 模式：不调用 agent，只跑验收，也不产出代码分支
    code3 = tmp / "light-a3"
    sh(["git", "clone", "-q", str(code_origin), str(code3)])
    hive(hive1, "agent", "add", "--name", "a3", "--mode", "verify",
         "--repo", f"light={code3}")
    ok_cmd = f'{PY} -c "print(\'win-leg-ok\')"'
    hive(hive1, "task", "new", "--id", "T-0004", "--title", "Windows 腿验收",
         "--repo", "light", "--verify", ok_cmd, "--body", "只验证，不改代码。")
    hive(hive1, "assign", "T-0004", "--agent", "a3")
    subprocess.run([PY, str(HIVE), "--dir", str(hive1), "work", "--agent", "a3",
                    "--once", "--state-dir", str(tmp / "state-a3")],
                   text=True, capture_output=True)
    vres = json.loads(sh(["git", "--git-dir", str(hive_origin), "show",
                          "res/T-0004:results/T-0004.json"]).stdout)
    assert vres["status"] == "done" and vres["mode"] == "verify", vres
    assert "verify-only" in vres["summary"], vres["summary"]
    verify_branch = sh(["git", "--git-dir", str(code_origin), "for-each-ref",
                        "--format=%(refname)", "refs/heads/task/T-0004"]).stdout.strip()
    assert verify_branch == "", f"verify 模式不应产出 task 分支: {verify_branch}"
    print("verify-only 模式：无 agent 调用、无代码分支、结果 done")

    # 8b. verify 卡验证另一张卡的分支（base=task/T-0001）
    branch_cmd = "test -f feature.txt"
    hive(hive1, "task", "new", "--id", "T-0005", "--title", "验收 T-0001 的分支",
         "--repo", "light", "--base", "task/T-0001", "--verify", branch_cmd,
         "--body", "在另一台机器上验修复分支。")
    hive(hive1, "assign", "T-0005", "--agent", "a3")
    subprocess.run([PY, str(HIVE), "--dir", str(hive1), "work", "--agent", "a3",
                    "--once", "--state-dir", str(tmp / "state-a3")],
                   text=True, capture_output=True)
    follow = json.loads(sh(["git", "--git-dir", str(hive_origin), "show",
                            "res/T-0005:results/T-0005.json"]).stdout)
    assert follow["status"] == "done" and follow["base"] == "task/T-0001", follow
    assert follow["head"] == sh(["git", "--git-dir", str(code_origin), "rev-parse",
                                 "task/T-0001"]).stdout.strip(), follow
    print("verify 卡能直接验另一张卡的 task 分支")

    # 8c. manual 模式 + launch：GUI 干完写 summary，worker 收尾提交
    code4 = tmp / "light-a4"
    sh(["git", "clone", "-q", str(code_origin), str(code4)])
    launch = (f'{PY} -c "import pathlib,time; time.sleep(1); '
              f'pathlib.Path(\'.hive/summary.md\').write_text(\'manual ok\')"')
    hive(hive1, "agent", "add", "--name", "a4", "--mode", "manual",
         "--launch", launch, "--repo", f"light={code4}")
    hive(hive1, "task", "new", "--id", "T-0006", "--title", "GUI 手动任务",
         "--repo", "light", "--body", "模拟 GUI agent 写 summary。")
    hive(hive1, "assign", "T-0006", "--agent", "a4")
    subprocess.run([PY, str(HIVE), "--dir", str(hive1), "work", "--agent", "a4",
                    "--once", "--state-dir", str(tmp / "state-a4")],
                   text=True, capture_output=True)
    mres = json.loads(sh(["git", "--git-dir", str(hive_origin), "show",
                          "res/T-0006:results/T-0006.json"]).stdout)
    assert mres["status"] == "done" and mres["mode"] == "manual", mres
    assert "manual ok" in mres["summary"], mres["summary"]
    print("manual + launch 模式：summary 回收、提交、结果 done")

    # 9. retry：清掉 T-0002 的 claim 分支
    hive(hive1, "retry", "T-0002")
    left = sh(["git", "--git-dir", str(hive_origin), "for-each-ref",
               "--format=%(refname)", "refs/heads/claim/T-0002"]).stdout.strip()
    assert left == "", f"retry 后 claim 应消失，仍有 {left}"

    # 9. 失败路径：verify 红灯 → result 必须是 failed
    hive(hive1, "task", "new", "--id", "T-0003", "--title", "故意失败",
         "--repo", "light", "--verify", "exit 1", "--body", "测试失败上报。")
    hive(hive1, "assign", "T-0003", "--agent", "a1")
    p = subprocess.run([PY, str(HIVE), "--dir", str(hive1), "work", "--agent", "a1",
                        "--once", "--state-dir", str(tmp / "state-a1")],
                       text=True, capture_output=True, env={**os.environ, "FAKE_SLEEP": "0"})
    failed = json.loads(sh(["git", "--git-dir", str(hive_origin), "show",
                            "res/T-0003:results/T-0003.json"]).stdout)
    assert failed["status"] == "failed", failed
    assert failed["verify"][0]["code"] == 1, failed
    print("失败路径：result.status=failed，verify 退出码已记录")

    # 10. 收尾：close 归档结果并删分支，gc 冒烟
    hive(hive1, "close", "T-0001", "--note", "自测通过")
    archived = json.loads(sh(["git", "--git-dir", str(hive_origin), "show",
                              "main:results/T-0001.json"]).stdout)
    assert archived["status"] == "done", archived
    left = sh(["git", "--git-dir", str(hive_origin), "for-each-ref",
               "--format=%(refname)",
               "refs/heads/claim/T-0001", "refs/heads/res/T-0001"]).stdout.strip()
    assert left == "", f"close 后分支应清掉，仍有 {left}"
    after = json.loads(hive(hive1, "status", "--json").stdout)
    row = [r for r in after if r["id"] == "T-0001"][0]
    assert row["result"] == "done", row
    hive(hive1, "gc", "--minutes", "0")
    print("\nPASS ✅")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
