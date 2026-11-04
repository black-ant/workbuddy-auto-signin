# 部署结果记录

> 说明：本仓库内为**对外公开版**，真实账号、UID、邮箱、主机名与本机绝对路径已脱敏。
> 含真实值的原始记录仅保留在本机工作区，不进入版本控制。

部署日期：2026-11-03
目标机：Debian 13 (trixie)，内核 6.12.94，x86_64，主机名 `<HOSTNAME>`
用户：`<user>`（uid 1000，linger 已开启）

---

## 一、结论速览

| 部分 | 状态 | 说明 |
| --- | --- | --- |
| WorkBuddy 签到 + 成长中心 | ✅ 已完成 | doctor / status / auto 全通，systemd 实测退出码 0 |
| WorkBuddy 定时任务 | ✅ 已启用 | 签到每天 09:00~10:00 内随机、成长轮询 01/05/09/13/17/21 点 |
| Qoder 取 token | ✅ 已完成 | keyring 口令 + AES 解密 + 设备头全通，systemd 实测退出码 0 |
| Qoder 定时任务 | ✅ 已启用 | 首领 10:05~11:05 随机 + 白天 3 次兜底，实测 `Result=success` |
| CodeArts 签到 | ✅ 已完成 | IDE-native AK/SK 签名路线，`doctor` → `AUTH_READY`，systemd 实测退出码 0 |
| CodeArts 定时任务 | ✅ 已启用 | 首签 09:00~10:00 随机 + 白天 3 次兜底，实跑返回 `ALREADY` |
| 看板接入 | ✅ 已完成 | `GET /api/claims` 返回 3 账号 / 4 任务，三张卡片均绿 |
| 参考实现调研 | ✅ 已完成 | `local/REFERENCE-IMPLEMENTATION.md`；并更正了 CodeArts 的技术路线（第九节） |

> 定时单元共 **4 组**（原 3 组 + `codearts-claim`），凭据全程不落盘、不打印、不进仓库。

**Qoder 卡点的最终结论（2026-11-03 15:57 定稿）**：
先前两次判断（"从未登录" → "客户端登出导致文件被删"）都只是表象。真正的原因有两个，
而且**都出在我自己写的适配代码里**：

1. **口令优先级选错**：keyring 里有两条 Qoder 相关条目，且口令不同——
   `[7] Chromium Safe Storage / application=Qoder App`（客户端实际使用）
   和 `[8] Qoder Safe Storage / application=Qoder`。
   原实现把 `Qoder` 排在前、`Qoder App` 排在后，先试错的口令就直接放弃了。
2. **PBKDF2 盐长度搞错**：Chromium 官方 Linux 实现用 25 字节盐
   （`b"saltysalt" + 16×0x00`），但 Qoder 桌面端实际按 **9 字节** `b"saltysalt"` 派生。
   这一条是穷举比对出来的：同一口令、同一密文，换 9 字节盐立刻得到合法 JSON。

修完这两点，用 `[7]` 的口令 + 9 字节盐一次成功。账号没变，
仍是 `qoder-user@example.com`（Qoder 账号）。

---

## 二、适配层（`local/`）

位于 `<CLONE_DIR>/local/`。最初用 `.git/info/exclude` 整目录排除，
后来为了把适配层提交进自己的 fork，改成只排除运行产物：

```
local/logs/
local/__pycache__/
```

CodeArts 接入后核心脚本为 8 个（见附录 A 的文件清单），定时单元 4 组。

### 仓库 remote 约定

本机这份是 fork 出来的，remote 分成两个，别搞混：

| remote | 地址 | 用途 |
| --- | --- | --- |
| `origin` | `github.com/black-ant/workbuddy-auto-signin` | 自己的 fork，`main` 跟踪它 |
| `upstream` | `github.com/88lin/workbuddy-auto-signin` | 原作者仓库，只读，用来同步更新 |

同步上游的正确姿势（合并到本地工作副本，不碰 `local/`）：

```
git fetch upstream
git merge upstream/main        # 上游不含 local/，不会冲突
```

推自己的改动：

```
git push origin main
```

> `README.md` 里的徽章、作者署名、推广链接都指向 `88lin`，那是上游内容，
> **不要改**——改了每次合并上游都会冲突。自己的适配说明一律写在 `local/` 下。

| 文件 | 实际内容要点 |
| --- | --- |
| `env.sh` | 凭据、可执行文件、端点、Qoder 登录态与设备缓存路径、venv 解释器 |
| `run.sh` | 加载 env.sh 后 `exec` 上游 `signin.py`，透传子命令 |
| `run-qoder.sh` | 先取 token 注入 `QODER_TOKEN`，再调 `qoder_runner.py` |
| `qoder_token.py` | Secret Service + keyring 文件双路取口令，AES-128-CBC 离线解密 |
| `qoder_runner.py` | 导入上游后替换 `risk_headers`，补四个设备头 |

### `env.sh` 实际填入的值

| 变量 | 值 | 依据 |
| --- | --- | --- |
| `WORKBUDDY_AUTH_FILE` | `~/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` | 探测到的实际文件（8621 字节） |
| `WORKBUDDY_EXE` | `/opt/WorkBuddy/workbuddy` | 存在、可执行，Electron 37.10.3 |
| `QODER_BASE_URL` | `https://openapi.qoder.sh` | 日志中实际请求域名（国际版） |
| `SIGNIN_PYTHON` | `~/.workbuddy/binaries/python/envs/default/bin/python` | 见下节 |

**端点判定过程**：`~/.config/Qoder/logs/` 下聚合出 `openapi.qoder.sh`（12 次）、
`center.qoder.sh`（21 次）、`marketplace.qoder.sh`，**没有任何 `qoder.com.cn` 请求**，
故判为国际版。

### 额外的运行时依赖（文档第五节未列，但实际必需）

上游只用标准库，但 `qoder_token.py` 需要 AES 与 D-Bus：
- `cryptography`（AES-128-CBC）
- `dbus-next`（Secret Service）

系统 python 没有这两个包，因此建了独立 venv：
`~/.workbuddy/binaries/python/envs/default`，由 `env.sh` 的 `SIGNIN_PYTHON` 指向。
**换机器时要重建这个 venv**：
```
<managed-python> -m venv ~/.workbuddy/binaries/python/envs/default
~/.workbuddy/binaries/python/envs/default/bin/pip install \
  -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple cryptography dbus-next
```

---

## 三、WorkBuddy 验收实测

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 凭据就绪 | `./local/run.sh doctor` | `AUTH_READY`，`credential_format: sym-v1`，`electron_version: 37.10.3` |
| 当日状态 | `./local/run.sh status` | HTTP 200，`today_checked_in: true`，`streak_days: 3`，`total_credits: 300` |
| 领取 | `./local/run.sh auto` | `ALREADY`（今日已签），成长中心批量领取成功 |
| 幂等 | 再次 `auto` | 仍返回 `ALREADY`，未重复加分 |
| systemd 实跑 | `systemctl --user start workbuddy-signin.service` | `Result=success`，`ExecMainStatus=0` |

最后一条是关键：**前台能跑通不代表 systemd 里能跑通**，本次已在 user manager
真实环境验证通过（keyring 未锁，`gnome-keyring-daemon` 正常运行）。

---

## 四、定时任务

单元文件在 `local/systemd/`，已复制到 `~/.config/systemd/user/`。

> **2026-11-04 调整**：签到类 3 个 timer 改为「工作时间随机」——触发点全部收进
> 09:00~18:00，`RandomizedDelaySec` 从 60/120 秒扩大到 1 小时（每个触发点在原点
> 往后 1 小时内随机、每天时刻各不相同，模拟真人上班签到），凌晨/深夜兜底触发点
> 全部挪到白天；`workbuddy-growth-poll` 维持原状。修改后需重新同步部署：
> `cp local/systemd/*.timer ~/.config/systemd/user/ && systemctl --user daemon-reload`。

| 单元 | 频率 | 状态 |
| --- | --- | --- |
| `workbuddy-signin.timer` | 每天 09:00~10:00 内随机 | ✅ enabled |
| `workbuddy-growth-poll.timer` | 01/05/09/13/17/21 点 | ✅ enabled |
| `qoder-claim.timer` | 首领 10:05~11:05 随机，兜底 12:30~13:30 / 14:30~15:30 / 16:30~17:30 | ⏸ 已 disable（待登录后启用） |

三者均 `Type=oneshot` + `Persistent=true` + `RandomizedDelaySec`。
`ExecStart` 直接指向仓库里的 `local/run.sh`、`local/run-qoder.sh`。

### 开机自启链路（已核实）

```
开机 → user@<uid>.service（linger=yes，无需登录即启动）
     → default.target → timers.target
     → timers.target.wants/workbuddy-signin.timer  ← 软链已建立
     → timers.target.wants/workbuddy-growth-poll.timer
     → OnCalendar 到点拉起对应 .service
```

核实命令与结果：

| 检查项 | 命令 | 结果 |
| --- | --- | --- |
| linger | `loginctl show-user <user> -p Linger --value` | `yes` |
| 自启链接 | `ls ~/.config/systemd/user/timers.target.wants/` | 两个 timer 软链均在 |
| 启用态 | `systemctl --user is-enabled ...` | 两个 `enabled`，Qoder `disabled` |
| 激活态 | `systemctl --user is-active ...` | 两个 `active`，Qoder `inactive` |
| 服务结果 | `systemctl --user show ... -p Result` | 均为 `Result=success` |

`Persistent=true` 的作用：关机期间错过的触发点，开机后会立刻补跑一次。

**注意**：`timers.target` 属于 user 实例，由 `user@<uid>.service` 拉起。
该实例能开机自启的前提是 **linger=yes**（已确认）。若 linger 被关掉，
定时任务只在用户登录后才会跑。

日志查询：
```
journalctl --user -u workbuddy-signin.service -n 50
journalctl --user -u workbuddy-growth-poll.service -n 50
journalctl --user -u qoder-claim.service -n 50
```

---

## 五、Qoder 链路（已打通）

### 账号已确认

| 字段 | 值 |
| --- | --- |
| 邮箱 | `qoder-user@example.com` |
| 用户名 | Qoder 账号 |
| 用户 ID | `<QODER_UID>` |
| 账号类型 | `personal_professional_trial`（个人专业版试用） |
| 客户端版本 | 1.37.0 |

来源：`~/.config/Qoder/SharedClientCache/cache/status.json`（明文），
并与 `auth.v1.dat` 解密出的 `user` 字段互相印证。

登录时间线（来自 `~/.config/Qoder/logs/`）：

| 时间 | 事件 |
| --- | --- |
| 2026-10-30 11:45:55 | 发起 PKCE 设备码登录，`loginBase=https://www.qoder.com/device/selectAccounts`，`openApiBase=https://openapi.qoder.sh` |
| 2026-10-30 11:46:08 | 拿到设备 token（重试 10 次） |
| 2026-10-30 11:46:10 | **登录成功**，`AicodingLoginService: login success` |
| 2026-11-03 15:47 | 打开客户端时处于登出状态，日志持续报 `user not logged in`，`auth.v1.dat` 被删 |
| 2026-11-03 15:52 | 重新登录，`auth.v1.dat` 重新生成（531 字节） |

**端点再次确认**：日志里 `openApiBase=https://openapi.qoder.sh`，与 `env.sh` 中的
`QODER_BASE_URL` 一致，选国际版是对的。

### 解密参数（本机实测，与 Chromium 官方实现有一处偏差）

```
Password = keyring 中 "Chromium Safe Storage / application=Qoder App" 的 secret
Salt     = b"saltysalt"                    <-- 9 字节，不是 25 字节
Key      = PBKDF2-HMAC-SHA1(password, salt, 1)[:16]
Cipher   = AES-128-CBC, IV = b"\x20" * 16
Data     = 跳过前 3 字节 "v11" 前缀
Padding  = PKCS#7
```

### 两个真正的坑（都是适配代码的锅）

**坑 1：口令选错条目。** keyring 里有两条 Qoder 相关、且口令不同的条目：

```
[7] label="Chromium Safe Storage"  application=Qoder App   <-- 客户端实际用的
[8] label="Qoder Safe Storage"      application=Qoder
```

原实现的 `APPS = ("Qoder CN App", "Qoder App", "Qoder")` 其实顺序是对的，
但**读文件的路径压根没取到 `application` 属性**（见坑 3），退化成按条目号顺序试，
先命中 `[8]` 的错口令就一路错下去。现已在 `APPS` 里把 `Qoder App` 放首位，
并保留 `Qoder CN App` / `Qoder` 作兜底。

**坑 2：PBKDF2 盐长度。** Chromium 官方 Linux 实现是 `b"saltysalt" + b"\x00"*16`（25 字节），
Qoder 桌面端实际按 **9 字节** `b"saltysalt"` 派生。这一条是靠穷举比对定位的：

```
同一口令 + 同一密文：
  salt=25字节  -> 解密结果是乱码，padding 校验不过（pad=84，非法）
  salt= 9字节  -> 立刻得到合法 JSON，padding=3 正确
```

`qoder_token.py` 现在用 `SALT_CANDIDATES = (b"saltysalt", b"saltysalt"+16×0x00)`，
两种都试，客户端升级换实现也不会立刻挂。

**坑 3：keyring 文件解析器（上一轮已修）。** 属性块是三行一组
`name=application` / `type=string` / `value=Qoder App`。旧实现把 `type` 也塞进 attr，
随后 `name` 被覆盖，导致 `application` 永远取不到、标签退化成 `?/?`。

### 当前实测结果

```
$ ./local/run-qoder.sh status
[qoder_token] 口令来源：secret-service
[qoder_token] 登录态到期：2026-12-03T07:52:27Z
{"result": "OK", "report": "共 2 个活动", "http": 200, ...}

$ ./local/run-qoder.sh auto
{"result": "CLAIMED", "report": "成功领取 100 Credits（act-20261102-734）", "credits": 100}

$ ./local/run-qoder.sh auto          # 幂等验证
{"result": "ALREADY", "report": "本周期已领取 100 Credits（act-20261102-734，窗口 11-03 10:00 ~ 11-04 09:59）"}
```

systemd 环境实测：`Result=success`、`ExecMainStatus=0`，日志里同样是
`口令来源：secret-service`，说明 `DBUS_SESSION_BUS_ADDRESS` 在 unit 里已正确注入。

### 维护提醒

- **token 到期 2026-12-03 07:52（CST）**，refresh token 到 2027-10-29。
  到期前一周打开一次 Qoder 客户端让它续期即可，不用重新登录。
- 若某天开始报 `试过 N 个口令均无法解开登录态`，按这个顺序查：
  1. `auth.v1.dat` 的 mtime 是不是变新了（客户端改写）→ 重跑一次；
  2. `systemctl --user -M <user>@ list-units | grep keyring` 确认 daemon 在；
  3. 客户端版本升级 → 可能又换了盐，重跑一次穷举定位。
- 若报 `未找到 Qoder 登录态`，就是客户端被登出了，重新登录一次即可。

---

## 六、避坑记录（本次实际踩到的）

| # | 现象 | 根因 | 处理 |
| --- | --- | --- | --- |
| 1 | 克隆失败 | `github.com` 主站不通，`codeload` 通 | 用 `ghfast.top` 前缀镜像克隆，之后 remote 改回官方地址 |
| 2 | `ps` 看不到 `dbus-daemon` / `gnome-keyring` | 执行环境有进程视图隔离，看不到用户会话进程 | 改用 `systemctl --user -M <user>@ list-units` 判断，真实存在且 active |
| 3 | root 连不上用户总线 | D-Bus EXTERNAL 认证拒绝跨用户 | 用 `systemd-run --user -M <user>@` 以 `<user>` 身份执行 |
| 4 | `gdbus` 调 GetSecrets 返回签名不匹配 | `a{o(oayays)}` 解包形式随版本变化 | 改用 `bus.call(Message(...))` 原始收发，递归收集 bytes |
| 5 | 后台 `nohup` 进程被回收 | 执行沙箱回收子进程 | 用 `systemd-run --user -M <user>@` 托管，或前台跑 |
| 6 | 长输出污染终端 | 命令回显带完整环境变量 | 查关键事实时用 `stat` / `systemctl` 等短输出命令 |

---

## 七、安全落实

- 口令、派生密钥、token 均不落盘、不写入仓库、不打印；
- `qoder_token.py` 的 token 只走标准输出，诊断信息全走标准错误；
- `run-qoder.sh` 通过 `QODER_TOKEN` 环境变量交给上游，`unset` 掉中间变量；
- `local/` 在 `.git/info/exclude` 中；`git status` 干净，适配层不会误提交；
- keyring 文件仅读取，未做任何写入。

---

## 八、看板接入（`ant-system-dashboard`）

在现有看板 `<DASHBOARD_DIR>` 新增「每日自动领取」面板，
位置在服务列表之上。

新增/改动文件：
- `claims.js`（新增）：读取领取状态，导出 `collectClaims()`
- `server.js`：注册 `/api/claims` 路由
- `public/index.html`：新增面板 DOM
- `public/styles.css`：新增 `.claim-*` 样式
- `public/app.js`：新增 `renderClaims()` / `refreshClaims()`，并入 5 秒轮询

数据来源（纯本地只读，不触发网络）：
1. `systemctl --user show <unit>.service` → `Result` / `ExecMainStatus` / 上次执行时间
2. `journalctl --user -u <unit>.service -o cat` → 解析最近一条 JSON 结果
3. `systemctl --user show <unit>.timer` → 下次触发、启用/激活状态
4. 上游 `signin.log` → 兜底历史记录

**不读取凭据、keyring、token**，只看执行结果与调度状态。

面板展示：
- 每账号一张卡：状态徽章（已领取 / 待领取 / 需登录 / 失败）、今日积分、
  连续天数、累计积分、结果原文、上次执行时间、退出码
- 下方任务表：调度表达式、启用状态、上次结果、下次触发时间

验证结果：
```
GET /api/claims → accounts: 2 | jobs: 3

WorkBuddy    result=ALREADY  checkedIn=true  ok=true  credits=100
Qoder        result=ALREADY  checkedIn=true  ok=true

WorkBuddy 签到      enabled active  next=11-04 00:05  last=11-03 15:22  result=success status=0
WorkBuddy 成长轮询  enabled active  next=11-03 17:00  last=11-03 15:57  result=success status=0
Qoder 100 Credits   enabled active  next=11-03 19:05  last=11-03 15:56  result=success status=0
```

两张卡片均为绿色（已领取），无 `needs_attention`。
注意端口是 **3100**（`HOST=<LAN_IP> PORT=3100`），不是 3080。

服务已 `systemctl --user restart ant-system-dashboard.service`，状态 `active`，
`is-enabled` 为 `enabled`（`default.target.wants` 软链已存在，随开机自启）。

访问入口同原看板（`http://<DASHBOARD_HOST>`，本机 `<LAN_IP>:3100`）。

---

## 九、CodeArts Agent（码道）每日签到调研（2026-11-03 16:30，17:50 更正）

> **⚠️ 本节结论已于 2026-11-03 17:50 更正，见下方「结论更正」。**
> 下面保留原始调研记录，是因为「会话解密 / STS 续期 / AK-SK 签名」这三段
> 本身没错、且已实测可用；但**方向从一开始就走错了**——签到压根不走 IAM / APIG 网关。

**当时的结论：这条路没打通，未部署定时任务。** 调研脚本留在
`local/codearts_token.py`，未接入 systemd、未进看板。**至今仍未实现签到、未部署。**

### 产品事实（官方文档核对过）

码道 CodeArts 确实有每日签到，官方帮助中心 `offers-codeartsagent_offers_0004`：

| 套餐 | 每日签到积分 |
| --- | --- |
| 个人体验版 | 100 |
| 个人标准版 | 150 |
| 个人高级版 / 旗舰版 | 200 |

活动期 2026-10-27 ~ 2026-12-31。**积分当日有效，逾期自动失效**，
所以签到漏一天就是实打实少了。

入口（官方给的 4 条路）：码道 IDE / 插件右上角「限时福利」→「每日领取」；
控制台左下角头像 →「活动福利」→「每日领取」；桌面端/网页端同上；小程序同上。

本机装的是码道 IDE（`/usr/share/codearts-agent`），已登录账号
`<CODEARTS_ACCOUNT>`（domainId `<CODEARTS_DOMAIN_ID>`）。

### 已经打通的 3 段

**1. 会话解密** —— 与 Qoder 同构，`v11` + AES-128-CBC + 9 字节盐
（`b"saltysalt"`），口令在 keyring 的 `application=codearts-agent` 条目。
解开可得 AK/SK、securitytoken、refresh_token、DPoP 私钥。

**2. 会话续期** —— `POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens`，
`grant_type=refresh_token`，需要 DPoP proof。

> **踩坑**：JWS 的 ES256 签名必须是 **raw R||S（各 32 字节）**，
> 不是 DER 编码。用 DER 提交会报
> `STS5.1804 invalid DPoP proof: 'decode DPoP failed with jwk'`——
> 错误信息指向 JWK，实际是签名格式错，很容易被带错方向。

> **另一个坑**：refresh_token 是**一次性轮换**的。手动试通一次就已经消费掉，
> 若不立即写回客户端存储，下次就报 `STS5.1806 has been used`。
> 写回时必须用"能解开当前密文的那个口令"加密，不能取候选列表第一个——
> Secret Service 返回顺序不固定。

**3. AK/SK 签名** —— 华为 APIG 的 `SDK-HMAC-SHA256` 与 AWS SigV4 不同：

```
string-to-sign 只有三行：
  SDK-HMAC-SHA256
  <X-Sdk-Date>
  sha256(canonical_request)
```

AWS V4 里那行 credential scope（`date/region/service/ak-sk-auth`）
**不存在**。多写这一行会得到 `401 verify ak sk signature fail`，
症状极像 AK/SK 错。

### 卡在哪（这就是没做成的原因）

业务 API 在 `snap-access` 网关上要求的是 **IAM 用户身份（`X-Auth-Token` 头）**，
不是 AK/SK 签名身份。实测对照：

| 请求 | 响应 |
| --- | --- |
| 不带认证 | `401 APIG.0301 x-auth-token not found` ← 网关在等 token |
| 带 AK/SK 签名 | `404 APIG.0101 API not published` ← 走了另一套未发布路由 |

尝试过的绕过路径，全部失败：

- `POST /v3/auth/tokens` 用 `methods=["hw_ak_sk"]` 换 IAM token → `400`，华为云不支持
- 换出来的临时 AK/SK 权限极窄：连客户端真实调用过的
  `sts.cn-north-4/v5/caller-identity` 都返回 `404 APIGW.0101`
- 网关 `snapengine-apig.cn-north-7.myhuaweicloud.com`（扩展里硬编码的国内版）
  根本不存在，DNS 直接 NXDOMAIN，那是内测版（`isGammaVersion`）的遗留域名
- `opengw.developer.huaweicloud.com` 是国际版网关，国内账号在上面无任何已发布 API

### 当时的两条候选路线（现已证明都不是必需的）

1. **抓运行时请求**：启动码道 IDE 后抓它实际发出的 `X-Auth-Token`，
   取自 `getProductConfigs().developersToken`（构建期注入）或
   `cloudUser.userInfo.cloudDragonToken`。若拿到固定值，就能直接复用。
2. **HUAWEI_ID 登录**：用账号密码走 IAM 换 `X-Auth-Token`。命令行可行，
   但等同于把华为云账号密码写进脚本，不符合本项目的安全约定，不建议。

### 现成可用的部分

```
python local/codearts_token.py session   # 看会话有效期与账号（需要 DBUS 环境）
python local/codearts_token.py refresh   # 续期并写回客户端存储
```

`session` 当前输出：账号 `<CODEARTS_ACCOUNT>`，`can_refresh=true`。

### 结论更正（2026-11-03 17:50，参考实现调研后）

拉取 `workbuddy2api-trae-qoder-minimax-panel`（tag `v1.15.3-panel`）逐文件分析后，
发现**签到根本不走 IAM / `X-Auth-Token` / AK-SK / DPoP**。参考仓库
`internal/codearts/codearts.go:3-10` 写得很明确：客户端里没有签到接口，
签到入口是网页/控制台「活动福利 → 每日领取」，只能用**网页会话 Cookie**；
客户端本地那套 STS 临时凭证（AK/SK/securityToken）拿它调不了活动接口。

正确的端点是另一套网页 API（`internal/codearts/api.go:14-22`）：

```
GET  https://devcloud.cn-north-4.huaweicloud.com/chat/PromptCenterService/v1/ops/delivery?channel=WEB
POST https://devcloud.cn-north-4.huaweicloud.com/chat/PromptCenterService/v1/ops/claim
     {"campaignId":N,"channel":"WEB","idempotentKey":"codearts-daily-<accountID>-<campaignID>-<YYYYMMDD>"}
```

鉴权只需浏览器登录后的 `Cookie` + 头 `cftk`（取自 cookie `devclouddevuibjtcftk`），
外加 `x-codearts-doer-scenario-type: web`、`x-requested-with: XMLHttpRequest`、
`language/x-language: zh-cn`、`Referer: .../chat/home`。
每日活动判据是 `type=USER_LOGIN`（标题「每日签到领100积分」）。

**本机实测佐证（17:50，未带任何凭据）**：不带 Cookie 打 `ops/delivery` 返回
`HTTP 200 + 空 body`（0.36s）。参考实现 `api.go:34-40` 记录的「会话失效特征」
正是 `HTTP 200 + 空 body`；若它是 APIG 的 `X-Auth-Token` 网关，不带 token 应返回
`401 APIG.0301`（就像我们之前打 `snap-access` 那样）。域名可达、端点存在、
鉴权模型对得上——**上面「卡在哪」那一节是我们自己走错了网关**。

其余实测坑（均来自参考实现，尚未在本机验证）：
`idempotentKey` 不带会返回 `40001「并发冲突，请重试」`（其实是无法区分重试与重复领取）；
`Already` 认 `40001/40002` 或文案含「已领」；响应 `Set-Cookie` 要吸收续期；
还要校验 CDP 端口归属，防止拿 A 的会话替 B 签到。

因此参考仓库面板的路线是：**CDP 打开码道网页端登录一次抓 Cookie →
纯 HTTP 查活动 → 带幂等键领取**。但这条路线**不是本机的必需路线**，
见下面 2026-11-04 的再更正。

> 签到仍未实现、未部署、未进看板。完整分析见下一节。

### 结论再更正（2026-11-04，读 IDE 插件源码后）

上面 17:50 的更正只适用于**参考仓库那个面板**，不能据此推出「本机必须
浏览器登录抓 Cookie」。直接读本机 IDE 插件
`huaweicloud.codearts-snap/out/extension.js` 后确认：客户端自带签到实现，
**不需要 Cookie / cftk**。

- `Pz()` 造请求头：`x-snap-traceid`（`It()` = uuid 去横线）、
  `Agent-Type: PromptCenter`、`Content-Type`、`X-Language`，
  `x-auth-token: await Pr()`。
- `Pr()` 商用版打日志 `Hc version, token is transferred by the toolkit.` 后
  **返回空串**；内部版才用 `cloudDragonToken`。也就是说 JS 层不提供 token。
- 真正鉴权在 `Mt.sendRequest` → `getGlobalCredentialClient().withCredentials()`
  → 华为云 SDK 的 **AK/SK 签名**（`SDK-HMAC-SHA256` + `X-Security-Token`）。
- 端点（商用版）：`GET  https://snap-access.cn-north-4.myhuaweicloud.com/v1/ops/delivery?channel=IDE`
  与 `POST .../v1/ops/claim`，body `{"campaignId":N,"idempotentKey":<uuid去横线>,"channel":"IDE"}`。
- 凭据就是 `HuaweiCloudSession` 里的 `accessKey` / `secretKey` / `securitytoken`
  —— 即 `codearts_token.py` 一直在解的那份会话。

**之前没打通是两个实现 bug，不是「拿不到凭据」**：
1. `codearts_token.py` 把签名用的 canonical URI（末尾补 `/`、且带 `/snap-manager`
   前缀）当成真实 URL 发出去，真实路径 `/v1/ops/delivery/` 不存在 → 404；
2. 误判网关要 `X-Auth-Token`，没看出商用版鉴权在 SDK credential client 里。

**本机实测（2026-11-04，全程只读，未打印任何凭据值）**：
- 修正 URL 后带签名打 `GET /v1/ops/delivery?channel=IDE`
  → `HTTP 400 APIG.0602 "Bad request: the security token has expired"`
  —— 路径命中、签名被网关接受，只差会话有效；
- 不带签名 → `401`；旧 `/snap-manager/...` 路径 → `404`（对照）；
- 会话 `expires_at=2026-11-03T10:06:31Z` 已过期。续期时华为云 STS 返回
  `HTTP 504 APIGW.0201 "Service STS timed out (30000 ms)"`，属服务端超时，
  重试仍失败。`save_session` 未执行，`state.vscdb` 未被改动。

**结论**：能从 IDE 拿到，网页 Cookie 那条路不必走。2026-11-04 在 IDE 里
重新登录一次后会话恢复，随即完成实现与部署，见下方「最终落地」。

### 最终落地（2026-11-04 10:33）

新增文件（均纳入版本控制）：

| 文件 | 作用 |
| --- | --- |
| `local/codearts_signin.py` | 查活动 + 领取主脚本；`SDK-HMAC-SHA256` 签名 + `X-Security-Token`，`claimable=false` 不调领取，同日用 `idempotentKey` 保证幂等 |
| `local/run-codearts.sh` | 入口，支持 `doctor / status / auto` |
| `local/systemd/codearts-claim.{service,timer}` | oneshot + `Persistent=true`，白天 4 个触发点（09:00 / 11:30 / 14:00 / 16:30）各往后 1 小时内随机 |

- 会话过期时脚本自动 `refresh_session()` + `save_session()` 一次并重试；
  `APIG.0602` / `APIGW.0301` 触发续期，续期失败仍可能需要人在 IDE 重新登录。
- 已领状态 `CLAIMED / CONFIRMED / CONSUMED` 与错误码 `40001 / 40002`
  一律映射为 `ALREADY`，不会重复发放。
- 凭据仍不落盘、不打印、不进仓库：AK/SK 只在 Python 进程内存里用于签名，
  不经环境变量传递（与 Qoder 的 token 注入方式不同）。

systemd 用户实例实跑验收：

```
Result=success
ExecMainExitTimestamp=Wed 2026-11-04 10:33:36 CST
ExecMainStatus=0
{"result": "ALREADY", "report": "今日已领 100 积分", "credit": 100, "campaignId": 34}
```

timer 状态 `active / enabled`，下次触发 15:21。看板 `/api/claims` 验证：

```
accounts: 3   jobs: 4
WorkBuddy  CLAIMED  checkedIn=True credit=100 ok=True
Qoder      CLAIMED  checkedIn=True credit=100 ok=True
CodeArts   ALREADY  checkedIn=True credit=100 ok=True
```

> 历史说明：本节上方 17:50 的「结论更正」描述的是参考仓库面板的网页
> Cookie 路线，只适用于那个面板；本机走的是 IDE-native 路线，两者不要混用。
> 上面的「签到仍未实现、未部署、未进看板」是当时的状态，已被本小节取代。

---

## 十、参考实现调研（2026-11-03 17:50）

**完整报告：`local/REFERENCE-IMPLEMENTATION.md`**（约 11 节）。
参考仓库解压在 `<REF_REPO_DIR>`，
只读分析，未修改其任何文件，也未在本机部署。

三个最有用的结论：

1. **CodeArts 路线更正**（上一节）—— 唯一能把签到真正做成的一处。
2. **它不是签到脚本，而是 2API 网关 + 多平台账号运营平台**：约八成代码是
   chat 管线与号池治理；签到逻辑分散在 6 个包。正确做法是抽取
   「签到 API + 调度模型 + 内嵌页面骨架」，不要 fork 裁剪。
3. **凭据模型与我们相反**：它自己走 OAuth，把明文 token 落在
   `auths/*.json`（0600 原子写）；只在 Windows 上用 DPAPI 解客户端已有加密文件，
   且**从未使用系统钥匙环保管自己的凭据**。我们「运行时从 Secret Service 解密、
   凭据不落盘」在静态保护上明显更强，代价是 headless / 容器场景更难移植。

可借鉴清单（详见报告第 8 节）：幂等记录带时间戳与平台信任线（Qoder 用今日
10:05 分界）、严格区分「已领 / BLOCKED / 无活动 / 失败」、执行前先查上游状态、
只重试 5xx 与网络错误、账号间限速、Qoder 30 分钟补试。
确认不需要：整条 chat 管线、`pool/` `session/` `redisstore/`、`minimax/`
`mmapi/` `traechat/`、浏览器自动化与成长任务体系。
