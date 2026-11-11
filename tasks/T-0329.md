---
id: T-0329
title: 前置：登记 lightprism 仓到 hive 与 Gitea
repo: hive
base: main
state: ready
owner: null
agent: null
priority: P0
needs:
  - docs
paths:
  - agents/fb88-dsh.json
  - agents/workbuddy.json
  - agents/raccoon-ic6.json
locks:
  - agents/fb88-dsh.json
  - agents/workbuddy.json
  - agents/raccoon-ic6.json
verify:
  - python hive.py doctor
  - python hive.py agent list
depends_on: []
lease_minutes: 60
timeout_minutes: 30
created: 2026-11-11T22:05:00+08:00
max_retries: 2
---

# 前置：登记 lightprism 仓到 hive

## 背景（实测）

`lightprism` 仓已经建好并推了三远端（`docs/远端与发布.md`），但**没有任何 agent 登记它**。
实测 `hive/agents/*.json` 全部 17 份配置：

```
fb88-dsh         mode=manual  dispatchable=True   repos=[hive, lightharness, light, lighthive, lightplugin]
workbuddy        mode=manual  dispatchable=None   repos=[hive, lightharness, lightmem, light, lighthive, lightplugin]
raccoon-ic6      mode=manual  dispatchable=None   repos=[hive, light, lighthive, lightmem, lightharness]
trae-win         mode=accept  dispatchable=None   repos=[hive, lightharness, light, lightmem]
workbuddyai-win  mode=accept  dispatchable=None   repos=[hive, lightharness, light, lightmem]
doubao-win       mode=accept  dispatchable=None   repos=[hive, lightharness, light, lightmem]
...（其余 11 台都没有 lightprism）
```

`hive.py` 的 `draft_lint` 会在 repo 未被任何 agent 登记时报错：

```
repo 'lightprism' 没有任何 agent 登记 —— 这张卡永远派不出去
```

所以 T-0330/02/03/04/05/06 六张卡现在**全部派不出去**。

## 目标

让 `lightprism` 至少被 **2 台 agent** 登记，其中至少 1 台 `dispatchable`。

## 要做的事

### 1. 确认 lightprism 仓状态

```sh
cd /home/skywalk/dswork/lightprism
git remote -v          # 应有 origin / github / gitcode
git log --oneline -1   # 应为 90b3951 或更新
curl -s -o /dev/null -w "%{http_code}\n" \
  http://192.168.1.5:3000/api/v1/repos/skywalk/lightprism   # 期望 200
```

Gitea 上已存在（2026-11-11 核实），无需新建。

### 2. 给三台 agent 加 `lightprism` 到 repos

目标文件与加的值（本地路径统一为 `/home/skywalk/dswork/lightprism`）：

| 文件 | 加什么 | 理由 |
|---|---|---|
| `agents/fb88-dsh.json` | `"lightprism": "/home/skywalk/dswork/lightprism"` | 唯一 `dispatchable`，必须加 |
| `agents/workbuddy.json` | 同上 | 有 docs 能力，可接 T-0335 |
| `agents/raccoon-ic6.json` | 同上 | 有 python，可接 01/02 |

**注意**：只加 repos，**不要**动 `mode` / `dispatchable` / `accepting` 等字段 ——
那是各机主人自己决定的，不由本卡改。

### 3. 验证

```sh
python hive.py doctor
python hive.py agent list
```

要求：`doctor` 无「lightprism 未被登记」类错误；`agent list` 里三台都能看到 `lightprism`。

## 完成标准

- 三份 `agents/*.json` 的 `repos` 含 `lightprism`
- `python hive.py doctor` 通过（若原有其他错误，只需证明**新增**的 lightprism 相关错误消失）
- `draft_lint` 对这六张卡不再报「永远派不出去」（可用 `python hive.py draft lint` 或对应命令验证）

## 禁止

- 不要改任何 agent 的 `mode` / `dispatchable` / `accepting` / `caps`
- 不要新建 Gitea 仓（已存在）
- 不要把 Windows 路径写成 Linux 路径（Windows agent 用 `G:\dswork\lightprism`，
  本卡只处理三台 Linux/内网 agent；Windows 机器由其主人自行登记）
