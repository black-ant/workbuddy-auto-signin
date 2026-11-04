# AGENTS.md

本文件面向在此仓库工作的 AI 编码助手（Claude Code / Codex / Cursor / Copilot /
CodeBuddy / CodeArts / Qoder 等），描述仓库结构、硬性约束与常见任务的标准做法。

**先读本文件，再动代码。** 人类部署请看 [`docs/linux-deploy.md`](docs/linux-deploy.md)。

---

## 1. 仓库是什么

一个 Python 小项目，每天自动领取三个平台的每日额度：

| 平台 | 领取内容 | 上游脚本 | 本 fork 适配 |
| --- | --- | --- | --- |
| WorkBuddy（腾讯） | 签到积分 + 成长中心任务 | `signin.py` | `local/run.sh` |
| Qoder（国际版） | 每日 100 Credits | `qoder_signin.py` | `local/run-qoder.sh` + `qoder_token.py` + `qoder_runner.py` |
| CodeArts Agent（华为云码道） | 每日签到 100 积分 | 无（IDE 内自带） | `local/codearts_signin.py` + `codearts_token.py` |

`signin.py` / `qoder_signin.py` 来自上游 [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin)，
**本仓库不修改它们**。

---

## 2. 硬性约束（违反即拒绝该改动）

1. **不修改上游文件**。`signin.py`、`qoder_signin.py`、`README.md`、`install-*.sh`、
   `workbuddy-auto-signin.plist.example`、`tests/` 一律不动。
   需要改行为时，用 `local/` 里的 wrapper / monkey patch 实现。
   > `README.md` 含上游署名与徽章，改了必然在 `git merge upstream/main` 时冲突。

2. **不把任何凭据写进文件**。包括但不限于 token、AK/SK、Cookie、密码、
   `machine_token.json` 内容、`auth.v1.dat`、`workbuddy-desktop.info`、
   `state.vscdb` 的会话字段。

3. **不打印凭据**。日志只允许输出：来源文件名、到期时间、状态、错误码、
   积分数量。禁止 `print(token)`、禁止把凭据写进异常消息、禁止 `repr(session)`。

4. **本地路径不进仓库**。`local/env.sh` 和 `local/systemd/*.service` 里的
   `/home/<user>/...` 是示例占位；不要"顺手"提交某台机器的真实路径、
   主机名、UID、邮箱、账号 ID。文档中一律用 `<CLONE_DIR>`、`<HOSTNAME>`、
   `<user>`、`<ACCOUNT_ID>` 占位。

5. **不新增第三方依赖**，除非确实必要。已有依赖仅 `cryptography` 和 `dbus-next`，
   且必须装在独立 venv。上游脚本只用标准库。

---

## 3. 目录结构

```
.
├── AGENTS.md                  ← 本文件（AI 入口）
├── README.md                  上游文档，勿改
├── signin.py                  WorkBuddy 上游脚本，勿改
├── qoder_signin.py            Qoder 上游脚本，勿改
├── docs/
│   ├── README.md              文档索引
│   ├── linux-deploy.md        Linux 完整部署指南（安装 / 配置 / 验收 / 排错）
│   ├── codearts-signin.md     CodeArts 签到协议实现说明（华为，签名与端点）
│   └── plans/                 上游的历史设计文档
└── local/                     本 fork 的全部适配层（可自由修改）
    ├── env.sh                 机器相关配置的唯一入口
    ├── run.sh                 WorkBuddy 入口
    ├── run-qoder.sh           Qoder 入口
    ├── run-codearts.sh        CodeArts 入口
    ├── qoder_token.py         Qoder v11 解密
    ├── qoder_runner.py        上游 monkey patch：补 Cosy-* 设备头
    ├── codearts_token.py      CodeArts 会话解密 / 续期 / 写回
    ├── codearts_signin.py     CodeArts 签到主逻辑
    ├── DEPLOY-RESULT.md       一次真实部署的实测记录
    ├── REFERENCE-IMPLEMENTATION.md  同类项目源码分析
    └── systemd/               4 组 oneshot + timer
```

---

## 4. 代码约定

### 输出契约（所有入口脚本必须遵守）

- **stdout 只有一行 JSON**，形如 `{"result": "...", "report": "...", ...}`
- **stderr 放诊断日志**，前缀 `[codearts]` / `[qoder]` 之类
- **退出码**：`0` 成功或已领；`1` 一般错误；`2` Qoder `NO_AUTH`；`3` CodeArts `NO_AUTH`
- **幂等**：当天重复执行返回 `ALREADY`，退出码仍为 `0`

### 结果字符串词表（不要自造新词）

```
AUTH_READY | CLAIMED | ALREADY | OK | NO_AUTH | NO_CAMPAIGN | ERROR
```

### 通用写法

- Python 只用标准库 + `cryptography` + `dbus-next`；用 `%` 格式化，不用 f-string
  （与上游 `signin.py` 风格一致，便于人工 diff）。
- Bash 统一 `#!/usr/bin/env bash` + `set -uo pipefail`（**不要** `-e`，
  因为需要自己处理退出码并输出 JSON）。
- `local/run-*.sh` 必须做子命令白名单校验，非法输入直接返回 `ERROR` JSON。
- 修改 `local/` 后必须自检：

```bash
python3 -m py_compile local/*.py
bash -n local/*.sh
systemd-analyze verify local/systemd/*.service local/systemd/*.timer
```

### systemd 单元约定

- `Type=oneshot`、`Persistent=true`、`RandomizedDelaySec=60~120`
- `OnCalendar=` 的写法：同一组时点用逗号分隔**小时**，不要在时分秒上逗号
  - 正确：`OnCalendar=*-*-* 00,10,15,20:20:00`
  - 错误：`OnCalendar=*-*-* 00:20,10:20,15:20,20:20:00`（verify 会报日历错误）
- 必须设 `Environment=HOME=`，否则解不到凭据
- `TimeoutStartSec` 不小于 300（CodeArts 可能要等 STS 续期）

---

## 5. 常见任务怎么做

### 新增一个平台的签到

1. 在 `local/` 新建 `<platform>_signin.py`，遵守第 4 节输出契约；
2. 新建 `local/run-<platform>.sh`，加载 `env.sh` 后 `exec "$SIGNIN_PYTHON" ...`；
3. 在 `local/systemd/` 加 `{service,timer}`，`OnCalendar` 按平台活动重置时间排；
4. 需要监控则接入看板（见 [docs/linux-deploy.md](docs/linux-deploy.md) 附录）；
5. 在 `docs/` 补一份说明，更新本文件第 1 节表格与 `docs/README.md` 索引。

### 调试凭据解不开

按这个顺序查，不要跳步：

1. 客户端是否登录（文件在不在、mtime 新不新）
2. 是否在用户 session bus 里跑（`echo $DBUS_SESSION_BUS_ADDRESS`）
3. keyring 里是否有多个同名条目（Chromium Safe Storage / `<App> Safe Storage`）
4. PBKDF2 盐长度（`b"saltysalt"` 9 字节 vs 25 字节）

### 修改文档

- 面向人类的放 `docs/`，用中文，命令可直接复制执行；
- 所有示例路径用占位符，写完用下面的命令自检脱敏：

```bash
grep -nE '/home/[a-z]|/Users/[a-z]|[0-9]{1,3}(\.[0-9]{1,3}){3}|hw0[0-9]{10,}|[a-zA-Z0-9._%+-]+@(qq|gmail|163|outlook)\.' docs/ AGENTS.md
```

期望输出为空（`<CLONE_DIR>` 这类占位符不匹配）。

---

## 6. 上游同步

```bash
git fetch upstream
git merge upstream/main    # 上游不含 local/ 与 docs/linux-deploy.md，不会冲突
```

只有 `README.md` 可能冲突（因为上游会改它）。**若冲突，取上游版本**，
不要为了保住本地改动去改 README。

---

## 7. 禁止事项

- ❌ 改 `signin.py` / `qoder_signin.py` / `README.md`
- ❌ 提交 `local/logs/`、`__pycache__/`、`signin.log`、任何凭据文件
- ❌ 把 `accessToken`（CodeArts 里是空串）当 token 用
- ❌ 网页 Cookie 路线与 IDE-native 路线混用（端点 / 前缀 / 请求头）
- ❌ 在 `claimable == false` 时调 `POST /v1/ops/claim`
- ❌ 续期失败时写回 `state.vscdb`
- ❌ 只看 `systemctl show -p Result` 就判断任务成功
- ❌ 用 `curl` 手工拼 CodeArts 签名请求来"验证"（会打印出 Authorization 头）
