T-0205 结果闸门：修复「head 没推上来照样记 done」的漏洞。

核心修改：
1. `remote_branch_head()` (L897)：git ls-remote 取远端 sha，网络失败重试一次，两次失败按「未推送」处理
2. `guard_result_head()` (L920)：零改动 + head 未推送双重校验；allow_empty_diff=True 放行纯调查卡
3. `guard_land_self_consistency()` (L955)：land 前置校验，结果 head 必须等于远端分支头
4. `Worker.run_task()` (L3870-3886)：push 失败时显式判 failed 并点名「head 未推送」
5. `Worker.cmd_submit()` (L2610-2623)：手动 submit 路径同样加闸门

测试：
- tests/test_result_head_guard.py: 16 个测试全部通过
- 验收标准：① 本地有提交但没推 → failed 点名「未推送」✓ ② changed_files=[] 的 done → failed ✓ ③ 正常卡零影响 ✓ ④ 补 tests/test_result_head_guard.py ✓