# Linux 部署指南（WorkBuddy + Qoder + CodeArts）

本文是 `local/` 适配层的完整安装部署说明，覆盖三个平台的每日自动领取：
**WorkBuddy 签到 + 成长中心**、**Qoder 每日 100 Credits**、
**CodeArts Agent（码道）每日签到 100 积分**。

- 适用系统：Linux（`systemd --user`）；Windows 见[附录 B](#附录-bwindows-适配层)
- 目标读者：人类操作者，以及需要在本仓库内工作的 AI 编码助手
- 上游项目：[88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin)
- 本 fork：[black-ant/workbuddy-auto-signin](https://github.com/black-ant/workbuddy-auto-signin)

> 只想快速跑起来：按 [1](#1-前置条件) → [4](#4-配置-localenvsh) → [7](#7-首次手工验证) → [8](#8-部署定时任务) 顺序做。
> 需要理解原理或排错：读 [docs/codearts-signin.md](codearts-signin.md) 与 [11 排错](#11-排错)。

---

## 0. 设计约束（改代码前必读）

1. **不改上游**。`signin.py` / `qoder_signin.py` 保持原样，随时可 `git merge upstream/main`。
   所有机器相关逻辑只写在 `local/`。
2. **凭据不落盘、不打印、不进仓库**。token / AK-SK 运行时从客户端加密存储 +
   系统钥匙环解出，只在进程内存中用。
3. **幂等优先**。每个平台都可能被调度多次，已领时必须返回 `ALREADY` 而非报错；
   能先查状态就不直接领。

违反以上任一条的改动，即使功能可用也不接受。

---

## 1. 前置条件

| 项 | 要求 | 检查命令 |
| --- | --- | --- |
| 系统 | Linux + 可用的 systemd user session | `systemctl --user status` |
| WorkBuddy | 桌面端**已登录**，凭据文件存在 | `ls -l "$HOME/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"` |
| Qoder | 桌面端**已登录**，`auth.v1.dat` 存在 | `ls -l "$HOME/.config/com.qoder.app.stable/auth.v1.dat"` |
| CodeArts Agent | IDE **已登录**，`state.vscdb` 存在 | `ls -l "$HOME/.config/codearts-agent/User/globalStorage/state.vscdb"` |
| 钥匙环 | gnome-keyring / Secret Service 可用 | `systemctl --user status gnome-keyring-daemon` |
| Python | 3.11+，可装 `cryptography`、`dbus-next` | `python3 --version` |
| linger | 已开启，否则未登录 / 关机时 timer 不跑 | `loginctl show-user "$USER" -p Linger` |

**核心认知**：三个平台都是从**本机客户端已登录的加密登录态**里解凭据，
不是脚本自己去登录。客户端未登录、登出、重装清配置，链路立刻断。
所以部署前必须在图形界面里把要用到的客户端各登录一次。

---

## 2. 获取代码

```bash
git clone https://github.com/black-ant/workbuddy-auto-signin.git
cd workbuddy-auto-signin

# 可选：留一个上游只读 remote，方便跟进原作者的修复
git remote add upstream https://github.com/88lin/workbuddy-auto-signin.git
```

| remote | 地址 | 用途 |
| --- | --- | --- |
| `origin` | `black-ant/workbuddy-auto-signin` | 自己的 fork，日常提交与部署 |
| `upstream` | `88lin/workbuddy-auto-signin` | 原作者，只 `fetch` + `merge` |

同步上游（上游不含 `local/`，不会冲突）：

```bash
git fetch upstream && git merge upstream/main
```

克隆后应能看到 `local/`（Linux 的 `.sh` / `.py` 入口 + 4 组 systemd 单元，
以及 Windows 的 `*_run.py`、`install-tasks.ps1`、`status.ps1`）。
若只有 `signin.py` / `qoder_signin.py`，说明远程 fork 还没同步适配层提交。

---

## 3. 安装运行时依赖

```bash
python3 -m venv "$HOME/.venvs/workbuddy-signin"
"$HOME/.venvs/workbuddy-signin/bin/pip" install cryptography dbus-next
```

> 目录名要与 `local/env.sh` 里 `SIGNIN_PYTHON` 的默认值一致（当前是
> `.venvs/workbuddy-signin`）。用别的名字就必须同时改 `env.sh`，否则取不到登录态。

| 包 | 用途 |
| --- | --- |
| `cryptography` | 解客户端 `v11` 登录态（AES-128-CBC） |
| `dbus-next` | 读 Secret Service 里的 Chromium Safe Storage 口令 |

记下解释器绝对路径，填到 `env.sh` 的 `SIGNIN_PYTHON`。**不要装进系统 Python**。

---

## 4. 配置 `local/env.sh`

所有机器相关配置集中在这一个文件，其余脚本不写死路径。

```bash
# WorkBuddy 桌面端登录凭据
export WORKBUDDY_AUTH_FILE="$HOME/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"

# WorkBuddy 主程序（提供 Electron 运行时以解密凭据，不启动界面）
export WORKBUDDY_EXE="/opt/WorkBuddy/workbuddy"

# Qoder 端点：国际版 openapi.qoder.sh；国区 openapi.qoder.com.cn
export QODER_BASE_URL="https://openapi.qoder.sh"

# Qoder 登录态与设备身份缓存
export QODER_LINUX_AUTH_FILE="$HOME/.config/com.qoder.app.stable/auth.v1.dat"
export QODER_MACHINE_TOKEN="$HOME/.config/Qoder/SharedClientCache/cache/machine_token.json"

# 装了依赖的解释器
export SIGNIN_PYTHON="${SIGNIN_PYTHON:-$HOME/.venvs/workbuddy-signin/bin/python}"

export LOCAL_LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs"
mkdir -p "$LOCAL_LOG_DIR"
```

| 变量 | 说明 | 怎么确认 |
| --- | --- | --- |
| `WORKBUDDY_AUTH_FILE` | 桌面端凭据；上游自动探测不认这个文件名，必须显式指定 | `find "$HOME/.local/share" -name '*.info'` |
| `WORKBUDDY_EXE` | WorkBuddy 主程序路径 | `command -v workbuddy` 或看安装目录 |
| `QODER_BASE_URL` | 国际 / 国区端点，选错直接连不上 | 看 `~/.config/Qoder/logs/` 里的 `openApiBase` |
| `QODER_LINUX_AUTH_FILE` | Qoder 加密登录态 | 见第 1 节 |
| `QODER_MACHINE_TOKEN` | 设备指纹，100 Credits 活动必需 | 登录客户端后自动生成 |
| `SIGNIN_PYTHON` | 装了 `cryptography` / `dbus-next` 的解释器 | `"$SIGNIN_PYTHON" -c "import cryptography, dbus_next"` |

**CodeArts 不需要新增变量**：`codearts_token.py` 按 `$HOME` 自己拼
`~/.config/codearts-agent/User/globalStorage/state.vscdb`，端点写死在
`codearts_signin.py` 的 `HOST`。IDE 登录态在就够了。

> 仓库里的 `env.sh` 与 `local/systemd/*.service` 预填的是**示例机器的绝对路径**
> （`SIGNIN_PYTHON`、`WorkingDirectory`、`ExecStart`、`Environment=HOME`），
> 必须按第 5 节改成自己机器的值，否则直接取不到登录态。

---

## 5. 修改 systemd 单元里的绝对路径（必做）

```bash
CLONE_DIR="$(pwd)"
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$CLONE_DIR#" local/systemd/*.service
sed -i "s#^ExecStart=.*/local/#ExecStart=$CLONE_DIR/local/#" local/systemd/*.service
sed -i "s#^Environment=HOME=.*#Environment=HOME=$HOME#" local/systemd/*.service

grep -H "WorkingDirectory\|ExecStart\|Environment" local/systemd/*.service
```

`Environment=HOME=` 必须指向真实用户目录 —— `env.sh` 所有凭据路径都基于 `$HOME`，
CodeArts 的 `state.vscdb` 也一样，填错等于白装。

---

## 6. 脚本接口一览

| 入口 | 子命令 | 说明 |
| --- | --- | --- |
| `local/run.sh` | `doctor` `status` `auto` `silent` `silent-poll` `growth` `claim` | WorkBuddy，转调上游 `signin.py` |
| `local/run-qoder.sh` | `doctor` `status` `auto` `silent` | Qoder，先解 token 再转调 `qoder_runner.py` |
| `local/run-codearts.sh` | `doctor` `status` `auto` | CodeArts，白名单校验后转调 `codearts_signin.py` |
| `local/workbuddy_run.py` | 同上（Windows） | WorkBuddy，钉住 exe / 凭据后转调 `signin.py` |
| `local/qoder_run.py` | `doctor` `status` `auto` `silent` | Qoder，注入登录态目录与国际版端点 |
| `local/codearts_run.py` | `doctor` `status` `auto` | CodeArts，DPAPI + AES-GCM 解会话并回写 |

Windows 三个入口与 shell 版共享同一套子命令与输出契约，由 `python` / `pythonw`
直接调用，调度见[附录 B](#附录-bwindows-适配层)。

约定：

- **标准输出只有一行结果 JSON**，诊断信息全部走 stderr，便于 `journalctl` 解析。
- 退出码：`0` 成功 / 已领；`2` Qoder 登录态不可用（`NO_AUTH`）；
  `3` CodeArts 登录态不可用（`NO_AUTH`）；`1` 其他错误。
- 所有 `auto` 都必须幂等：当天第二次执行返回 `ALREADY`，不算失败。

`local/codearts_token.py` 是独立诊断工具（不参与正常签到）：

```bash
"$SIGNIN_PYTHON" local/codearts_token.py session   # 看会话有效期与账号，不打印凭据
"$SIGNIN_PYTHON" local/codearts_token.py refresh   # 主动续期并写回 IDE 存储
```

---

## 7. 首次手工验证

部署 timer 之前必须手工跑一遍，确认凭据链路通了。

```bash
cd <克隆目录>

./local/run.sh doctor           # 期望 AUTH_READY
./local/run.sh status           # 期望 today_checked_in / total_credits
./local/run.sh auto             # 首次 CHECKED，再跑 ALREADY

./local/run-qoder.sh doctor     # 期望能解出登录态
./local/run-qoder.sh status     # 期望 HTTP 200，列出活动
./local/run-qoder.sh auto       # CLAIMED 100 Credits；再跑 ALREADY

./local/run-codearts.sh doctor  # 期望 AUTH_READY
./local/run-codearts.sh status  # 期望 OK / ALREADY
./local/run-codearts.sh auto    # CLAIMED 或 ALREADY
```

### D-Bus 环境（最常见的坑）

三个平台解密都要连当前用户的 Secret Service。在 root、SSH、纯 shell 里手工执行会报
`EXTERNAL 认证被拒`。正确做法是在用户 systemd 环境里跑：

```bash
systemd-run --user --wait --pipe --collect ./local/run-codearts.sh status
```

systemd service 天然在这个环境里，所以定时任务不受影响。**这只影响手工调试**。

---

## 8. 部署定时任务

```bash
mkdir -p "$HOME/.config/systemd/user"
cp local/systemd/*.service local/systemd/*.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload

systemctl --user enable --now workbuddy-signin.timer
systemctl --user enable --now workbuddy-growth-poll.timer
systemctl --user enable --now qoder-claim.timer
systemctl --user enable --now codearts-claim.timer

# 开机自启关键一步（需要 root / sudo）
sudo loginctl enable-linger "$USER"
```

| timer | 触发时间（仓库当前 = 方案 B） | 执行 | 为什么这么排 |
| --- | --- | --- | --- |
| `workbuddy-signin` | 09:00 + 随机 1h | `run.sh auto` | 白天开机才跑；上游签到跨零点重置 |
| `workbuddy-growth-poll` | 01/05/09/13/17/21 点 | `run.sh silent-poll` | 补签 + 成长中心；已领时空跑不写日志 |
| `qoder-claim` | 10:05 + 12:30 / 14:30 / 16:30 | `run-qoder.sh auto` | 活动 10:00 才下发且不总准时，多次幂等补领 |
| `codearts-claim` | 09:00 + 11:30 / 14:00 / 16:30 | `run-codearts.sh auto` | 活动按北京时间 00:00 重置，多次幂等补领 |

> 想知道为什么改到白天、以及旧版凌晨时刻是什么样，见
> [9.1 触发时刻](#91-触发时刻仓库里有两套方案选一套并真正装进去)。

- 四个 timer 全部 `Persistent=true`：关机期间错过的触发，开机后补跑。
- 签到类三个用 `RandomizedDelaySec=1h` 让每天时刻不同；成长轮询用 120 秒抖动。
- 开机自启链路：`linger` → `user@<uid>.service` → `timers.target` → timer。
  **user timer 依赖 linger，不是"开机瞬间"而是"用户实例启动即跑"。**

---

## 9. 验收

```bash
# 1) timer 是否都活着
systemctl --user list-timers

# 2) service 是否真跑过（关键：不要只看 Result）
systemctl --user show codearts-claim.service \
  -p Result -p ExecMainStatus -p ExecMainExitTimestamp -p ActiveState

# 3) 看最近一次输出
journalctl --user -u codearts-claim.service -n 20 --no-pager
```

> ⚠️ **别只看 `Result=success`**：systemd 的 `Result` 初始值就是 `success`，
> 从没跑过的 service 也显示成功。必须 `ExecMainExitTimestamp` 有值、
> `ExecMainStatus=0` 才算真执行过。必要时手动 `systemctl --user start <unit>.service`
> 跑一次产生时间戳。

| 检查项 | 期望 |
| --- | --- |
| 三个 `doctor` | WorkBuddy / CodeArts `AUTH_READY`，Qoder 无 `NO_AUTH` |
| 三个 `auto` | `CLAIMED` 或 `ALREADY`，退出码 0 |
| 四个 timer | `enabled` + `active` |
| 四个 service | `ExecMainExitTimestamp` 有值且 `ExecMainStatus=0` |
| linger | `Linger=yes` |

### 9.1 触发时刻：仓库里有两套方案，选一套并真正装进去

`local/systemd/*.timer` 目前是**工作时间随机**版；但已部署机器上曾按
**凌晨固定时刻**版安装过。两者不通用，`cp` 一份就以为生效是最常见的误判。

| 方案 | 适用 | workbuddy-signin | qoder-claim | codearts-claim |
| --- | --- | --- | --- | --- |
| **A. 凌晨固定**（旧） | 机器 24 小时开机 | `00:05` | `10,11,15,19,23:05` | `00,10,15,20:20` |
| **B. 白天随机**（仓库当前） | 白天才开机 / 想错开整点 | `09:00` + 随机 1h | `10:05` + 12:30/14:30/16:30 | `09:00` + 11:30/14:00/16:30 |

方案 B 的特点：`RandomizedDelaySec=1h`，每个触发点在原点之后一小时内随机落地，
所以每天执行时刻都不同，看起来更像真人；且全部落在 09:00~17:30 之间，
**依赖机器在白天开机**。

> 部署后务必验证**实际生效**的时刻，而不是看文件内容：
>
> ```bash
> systemctl --user list-timers --all | grep -E 'workbuddy|qoder|codearts'
> ```
>
> 若这里的 `NEXT` 与 `local/systemd/*.timer` 的内容不一致，说明只改了文件、
> 没执行 `cp` + `daemon-reload`，或改完没重启 timer。
>
> ```bash
> cp local/systemd/*.timer "$HOME/.config/systemd/user/"
> systemctl --user daemon-reload
> systemctl --user restart workbuddy-signin.timer qoder-claim.timer codearts-claim.timer
> ```

---

## 10. 安全边界

| 平台 | 凭据来源 | 如何使用 | 落盘 |
| --- | --- | --- | --- |
| WorkBuddy | `workbuddy-desktop.info`（`sym-v1` AES-256-GCM） | 经 `WORKBUDDY_*` 环境变量传给上游 | 否 |
| Qoder | `auth.v1.dat`（`v11` AES-128-CBC）+ keyring 口令 | token 经 `QODER_TOKEN` 环境变量传给上游 | 否 |
| CodeArts | `state.vscdb`（`v11`）+ keyring 口令，解出临时 AK/SK | **仅 Python 进程内存**，参与 `SDK-HMAC-SHA256` 签名，不经环境变量 | 否 |

- 派生密钥、口令、token、AK/SK **都不落盘、不打印**。日志只打来源、到期时间、文件名。
- CodeArts 续期后写回的是 **IDE 自己的** `state.vscdb`（`save_session()` 带自校验），
  不是本仓库任何文件。
- **禁止提交**：`auth.v1.dat`、`*.info`、`login.keyring`、`machine_token.json`、
  `state.vscdb`、`signin.log`、`local/logs/`、`__pycache__/`。
  仓库 `.gitignore` + `.git/info/exclude` 已排除运行产物。
- **多账号**：Qoder 100 Credits 按「人」发放，同一人多账号一天只有一份
  （`SAME_PERSON_ALREADY_CLAIMED`）。替换登录态文件不会让服务端重新签发身份，
  多账号需各自真实登录。
- 本文档与仓库内所有文档均已脱敏，不含真实账号、UID、邮箱、主机名或本机绝对路径。

---

## 11. 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| Qoder `NO_AUTH` | 客户端未登录，或 `auth.v1.dat` 被登出删除 | 打开 Qoder 客户端登录一次 |
| CodeArts `NO_AUTH` | IDE 未登录，或 `HuaweiCloudSession` 解不开 | 打开 CodeArts Agent 重新登录一次 |
| 多个口令都解不开 | keyring 里有多条 Chromium Safe Storage 条目，实际用的是 `application=Qoder App` 那条 | 适配层已按 `Qoder App` 优先并遍历候选；仍失败看客户端版本 |
| 解密得到乱码 | PBKDF2 盐长度不对 | Qoder / CodeArts 桌面端用 **9 字节** `b"saltysalt"`，不是 Chromium 标准的 25 字节。适配层两种都试 |
| CodeArts `APIG.0602` / `APIGW.0301` | 安全令牌过期 | 脚本会自动 `refresh_session()` 一次并重试；仍失败说明 STS 异常，稍后重试或 IDE 重新登录 |
| CodeArts `504 APIGW.0201 Service STS timed out` | 华为云 STS 服务端超时 | 续期失败时**不会**写回 `state.vscdb`，登录态不受破坏；等一会儿重试 |
| CodeArts `TM.00001001 请求头Agent-Type为空` | 漏了 `Agent-Type: PromptCenter` | 适配层已补，勿删 |
| CodeArts 404 | 把签名用的 canonical URI 当真实 URL，或加了 `/snap-manager` 前缀 | 真实 URL 用原始 `/v1/ops/delivery`，末尾不补 `/`；canonical URI 只参与签名 |
| root / SSH 里报 D-Bus `EXTERNAL` 拒绝 | 连的不是目标用户的会话 bus | `systemd-run --user --wait --pipe --collect ...` |
| `ModuleNotFoundError` | 用错解释器 | 检查 `SIGNIN_PYTHON` 是否装了依赖 |
| `Result=success` 但实际没跑 | `Result` 是初始值 | 看 `ExecMainExitTimestamp`；手动 start 一次 |
| 时间差 8 小时 | 把 systemd 输出的本地时间又当 UTC 解析 | `systemctl show` 输出的是本地时区，按本地时间构造 `Date` |
| timer 开机不跑 | 没开 linger | `sudo loginctl enable-linger "$USER"` |
| Qoder 报 404 / 端点不对 | `QODER_BASE_URL` 国际 / 国区选错 | 对照客户端日志的 `openApiBase` 改 |

CodeArts 的协议细节（签名串构造、请求头、状态映射）见
[codearts-signin.md](codearts-signin.md)。

---

## 12. 已知边界

- 覆盖 3 个账号 / 4 个定时任务；三套都依赖「本机桌面客户端已登录」。
- CodeArts 会话过期会自动续期；若华为云 STS 持续超时，仍需人进 IDE 重新登录，
  这一环做不到完全无人值守。
- 单账号场景。号池 / 选号 / 余额路由 / 成长任务 17 项等属于另一套更复杂的网关系统，
  本方案刻意不做。
- 仅在 Debian 13 + GNOME keyring + Python 3.13 实测通过；
  其他发行版需自备 Secret Service 与 `systemd --user`。

---

## 附录 A：文件清单

```
local/
# Linux
├── env.sh                         集中配置（唯一需要按机器修改的文件）
├── run.sh                         WorkBuddy 入口（转调上游 signin.py）
├── run-qoder.sh                   Qoder 入口（解 token → qoder_runner.py）
├── run-codearts.sh                CodeArts 入口（doctor / status / auto）
├── qoder_token.py                 Qoder v11 登录态离线解密，stdout 只输出 token
├── qoder_runner.py                导入上游并补 Cosy-* 设备头
├── codearts_token.py              CodeArts 会话解密 / 续期 / 写回 IDE 存储
├── codearts_signin.py             CodeArts 签到主脚本（SDK AK/SK 签名）
├── systemd/                       4 组 oneshot + timer
# Windows
├── workbuddy_run.py               入口：钉住 exe 与凭据路径
├── qoder_run.py                   入口：登录态目录 / 国际版端点 / 出口策略
├── codearts_run.py                入口：DPAPI + AES-GCM 解会话 / 续期 / 回写
├── net_win.py                     系统代理 CONNECT+TLS 实测，坏代理时仅目标域名直连
├── qoder_dns_pin.json             Qoder DNS 兜底 IP（ips 为空 = 走系统 DNS）
├── install-tasks.ps1              注册 4 个计划任务（抖动 + 登录补跑 + 回读校验）
├── status.ps1                     四任务 + 三平台体检
# 文档
├── DEPLOY-RESULT.md               某次真实部署的实测记录与踩坑复盘
└── REFERENCE-IMPLEMENTATION.md    同类开源项目源码分析（借鉴取舍）
```

---

## 附录 B：Windows 适配层

Windows 不用 systemd，四个计划任务由 `local/install-tasks.ps1` 注册，
与 `local/systemd/*.timer` 一一对应：

| 任务名 | 对应 timer | 触发时刻 | 抖动 |
| --- | --- | --- | --- |
| `WorkBuddyAutoSignin` | `workbuddy-signin` | 09:00 | `PT1H` |
| `WorkBuddyGrowthPoll` | `workbuddy-growth-poll` | 01/05/09/13/17/21 | `PT2M` |
| `QoderDailyCredits` | `qoder-claim` | 10:05 / 12:30 / 14:30 / 16:30 | `PT1H` |
| `CodeArtsDailyClaim` | `codearts-claim` | 09:00 / 11:30 / 14:00 / 16:30 | `PT1H` |

```powershell
# 解释器缺省自动探测 .venv\Scripts\pythonw.exe，也可显式指定
powershell -ExecutionPolicy Bypass -File .\local\install-tasks.ps1

# 体检：四个任务状态 + 三平台 doctor
powershell -ExecutionPolicy Bypass -File .\local\status.ps1
```

与 Linux 版的差异，三条务必留意：

1. **任务运行身份是 Interactive**，要求用户已登录。DPAPI 解密钥依赖当前用户
   配置文件，所以计划任务不能以 SYSTEM 或「不管用户是否登录」方式运行。
   每个任务额外挂 `AtLogOn` 触发器（延迟 2 分钟），补上 Linux `linger` 的位置。
2. **CodeArts 会话在 IDE 运行时只读不写**。VSCode 会把内存中的旧会话刷回
   `state.vscdb`，覆盖外部写入。但 `refresh_token` 是轮换的，续期后必须回写，
   两步保护（运行检测 + 写前备份/写后自校验）都不要省。
3. **`local/*.ps1` 必须存成 UTF-8 with BOM**。PowerShell 5.1 在无 BOM 时按 GBK
   解析中文注释，会报「字符串缺少终止符」并把错误定位到文件末尾。
