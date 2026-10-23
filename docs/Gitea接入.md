# Gitea 接入记录（2026-10-23 上午）

## 1. 凭据

- 文件：`/home/skywalk/dswork/.env`
- 关键行：`gitea = http://<user>:<token>@192.168.1.5:3000`
- 原权限 `644`（同机其他用户可读），已改为 **`600`**。
- 该文件还包含 SSH 密码、`SUDO_PASS`、GitHub/GitCode/AIStudio/OpenAI 等密钥，
  不要提交、不要放进任何仓库、不要贴到聊天里。

验证结果：Gitea 账号 `skywalk`，`is_admin=true`，版本 1.24.2。

## 2. 仓库

- 新建仓库：`http://192.168.1.5:3000/skywalk/hive`
- 本地 `~/dswork/hive` 已 `init`（补齐 `tasks/ inbox/ results/ agents/`）并推送 `main`
- 仓库内容：`hive.py`、`windows/`、`docs/`、`tests/`、`examples/`、`CHANGELOG.md`、`.gitignore`
- 默认分支 `main`；目前只有 `main` 一个分支（冒烟任务的 claim/res 已随 `close` 删除）

### 可见性变更记录

| 时间 | 可见性 | 操作 |
|---|---|---|
| 2026-10-23 上午 | private | 建仓时为私有 |
| 2026-10-23 10:45 | **public** | `PATCH /api/v1/repos/skywalk/hive {"private": false, "visibility": "public"}`；匿名访问网页和 `git clone` 均返回 200 |

公开前做过敏感信息扫描：被跟踪文件里无真实密钥；`.env`（含 SSH 密码、管理员 token、
各家 AI 密钥）未被跟踪且已在 `.gitignore`；`dashboard/status.json`、`status.err` 也已忽略。
**注意公开后可见的内容**：README/docs 里的内网 IP（`192.168.1.5:3000`、`192.168.0.88`）、
主机名（fb88 / fb5 / win10）、以及 `tasks/` `results/` 里的完整任务历史。要收回私有：

```sh
curl -X PATCH -H "Content-Type: application/json" \
  -u "<user>:<token>" \
  -d '{"private": true, "visibility": "private"}' \
  http://192.168.1.5:3000/api/v1/repos/skywalk/hive
```

## 3. 凭据 helper（不在 git 里存 token）

`scripts/gitea-credential.sh` 读取 `.env` 里的 `gitea=` 行，按 git credential 协议输出
用户名/令牌。只对 `192.168.1.5:3000` 生效：

```sh
git config --global credential."http://192.168.1.5:3000".helper \
  /home/skywalk/dswork/hive/scripts/gitea-credential.sh
```

- 可用 `HIVE_ENV=/path/to/file` 指向别的凭据文件；
- 仓库 remote 保持干净 URL，token 不出现在 `git remote -v` 或进程参数里；
- 换 token 只需改 `.env`，不用动 git 配置。

验证：

```sh
printf 'protocol=http\nhost=192.168.1.5:3000\n\n' | git credential fill
GIT_TERMINAL_PROMPT=0 git ls-remote http://192.168.1.5:3000/skywalk/hive.git
```

## 4. 已接入的节点

| agent | 模式 | 仓库 | 说明 |
|---|---|---|---|
| `fb88-verify` | verify | light=/home/skywalk/src/light | 只跑验收，不调用 LLM |
| `fb88-dsh` | exec | light=/home/skywalk/src/light | 需要 `DEEPSEEK_API_KEY` 才能启动 |

`light` 已 clone 到 `/home/skywalk/src/light`（65 MB，`main@a02a9b2`）。
为了让 light 的 pytest 默认参数能跑，补装了 `pytest-timeout`（`pip install --user`）。

## 5. 真实冒烟（T-0001）

```sh
python3 hive.py --dir ~/dswork/hive task new --id T-0001 \
  --title "集成冒烟：light 测试环境" --repo light --needs freebsd \
  --verify "python3 -m pytest -q tests/test_lpd_002_import_hint.py" ...
python3 hive.py --dir ~/dswork/hive assign T-0001 --agent fb88-verify
python3 hive.py --dir ~/dswork/hive work --agent fb88-verify --once
```

结果：

```
T-0001 领取成功
T-0001 验证 0: python3 -m pytest -q tests/test_lpd_002_import_hint.py
T-0001 完成: done（4.2s）
```

`close` 后：

```
已删除分支 claim/T-0001
已删除分支 res/T-0001
[ OK ] 1 张任务，2 个 agent，0 个进行中租约，0 个待合并结果
```

归档在 `results/T-0001.json`，任务卡标记 `done`。

## 6. 安全建议

1. **给 worker 单独 token**：当前 `.env` 里的 token 是管理员权限；建议在 Gitea
   「设置 → 应用 → 生成令牌」建一个只授权 `hive`、`light` 的 token，给 worker 用。
2. **拆分凭据文件**：把 Gitea 那行单独放到 `~/.config/hive/gitea.env`（600），
   `.env` 里其它 SSH/sudo 凭据不要和 agent 共用；helper 支持 `HIVE_ENV`。
3. **分支保护**：`main` 禁止直接 push；允许 `task/*`、`claim/*`、`res/*`。
4. **Windows**：用 Git Credential Manager 存单独账号的 token，不要用管理员 token。
5. **审计**：Gitea 的仓库活动 + git 历史就是审计日志；定期看 `hive doctor`。
