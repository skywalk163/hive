# 看板验收记录（2026-10-23 11:50，fb88 实跑）

WorkBuddy 交付时它的 shell 不可用（见交付目录里的 `BLOCKED.md`，未纳入本仓库），
只做了静态推演。这份记录是**在 fb88 上实际跑出来的结果**。

## 1. 服务端（`bash verify.sh`）

```text
[PASS] py_compile serve.py
[PASS] 服务能启动并打印 URL
[PASS] /api/status 返回 6 条
[PASS] /            -> 200 text/html
[PASS] /app.js      -> 200 application/javascript
[PASS] /style.css   -> 200 text/css
[PASS] /../serve.py -> 404（目录穿越被白名单挡掉）
[PASS] 缺失 status 文件 -> 503 + {"error": ...}
===== 全部通过 =====
```

## 2. 前端行为（jsdom 实跑，20 项断言）

因为 fb88 没有浏览器，用 jsdom 加载 `index.html` + `app.js`，stub `fetch` 返回
`sample-status.json`，把时钟固定在 `2026-10-23T11:00+08:00` 后断言 DOM：

```text
ok  - 进行中 1 张（T-0101）
ok  - 异常 2 张（T-0102/T-0104）
ok  - 待合并 1 张（T-0103）
ok  - 待领取 1 张（T-0105）
ok  - 已关闭列计数 1
ok  - 已关闭列默认折叠
ok  - 已关闭列可展开
ok  - 展开后出现 T-0106
ok  - 总共 6 张卡
ok  - T-0102 有 is-stale 红边框类
ok  - T-0102 文案是「已过期」
ok  - T-0104 显示 0/1 通过
ok  - T-0105 显示依赖
ok  - 搜索 lightmem 只剩 T-0103
ok  - 仓库筛选 light 得到 4 张（T-0104 属于 lightharness）
ok  - 点卡片打开详情抽屉
ok  - 抽屉里有失败用例名
ok  - Esc 关闭抽屉
ok  - 没有外链资源
ok  - 没有 JS 运行错误（jsdomError=0）
ALL UI CHECKS PASSED
```

复现：

```sh
cd dashboard
npm install jsdom            # 仅测试用；运行看板不需要 node
NODE_PATH=./node_modules node dev/ui-test.js
```

## 3. 接真实 hive 数据

```sh
cd ~/dswork/hive
PORT=8789 INTERVAL=5 ./scripts/serve-dashboard.sh
curl -s http://127.0.0.1:8789/api/status | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
```

实测返回 T-0001（真实冒烟任务，`state=done` / `result_status=done`），
`dashboard/status.json` 由脚本每 5 秒重新生成；停掉脚本后状态文件保留。

## 4. 备注：样例时间

`sample-status.json` 里 T-0101 的租约原本到 `2026-10-23T03:12`，白天打开会被
「渲染时重算 stale」正确地判成过期。为了演示时五列齐全，把它的
`claimed_at/lease_until` 改到当天 10:12 / 23:12；T-0102 仍然保留过期状态用来演示红色边框。
