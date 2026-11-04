# 参考实现分析：workbuddy2api-trae-qoder-minimax-panel

调研日期：2026-11-03 17:50
版本：`v1.15.3-panel`（tag 包，`main` 分支只有 `archive/`，不含 Go 源码）
源码目录：`<REF_REPO_DIR>`
方法：三条独立源码走读（签到调度 / 凭据号池 / 网关面板）+ README 交叉核对 + 本机接口实测

> 本文回答上一轮「拉取参考仓库，看看别人怎么实现的」。上一轮只做到下载解压、
> 确认「tag 包有 247 个 Go 文件」，没有实质结论，这里补齐。
> 全程只读参考仓库，**未修改其任何文件**，也未在本机部署它。

---

## 0. 一句话结论

这不是「签到脚本」，而是一个 **2API 网关 + 多平台账号运营平台**：约八成代码服务于
「把 IDE 账号转成 OpenAI 兼容 LLM 接口」和号池治理，签到只是其中一个子系统，
而且签到逻辑**分散在 6 个包**里。它的价值不在于可以照搬，而在于：

1. **推翻了我们此前对 CodeArts 的判断**——签到根本不走 IAM / `X-Auth-Token` / AK-SK，
   而是走网页会话 Cookie + `cftk`（见第 5.7 节，附本机实测）；
2. 给出一套成熟的**幂等 + 假成功识别**方法论，比「今天跑过就不跑」可靠得多（第 4.3 节）；
3. 解释了 Qoder 每日 100 Credits 的「按人发放」规则与 `SAME_PERSON_ALREADY_CLAIMED`（第 5.3 节）。

**对我们的定位判断**：正确做法是抽取「签到 API + 调度模型 + 内嵌页面骨架」，
而不是 fork 整个仓库再裁剪。第 9 节列了可借鉴清单与完全不需要的模块。

---

## 1. 仓库概况与代码版图

| 项 | 值 |
| --- | --- |
| module | `github.com/linguo2625469/workbuddy2api-panel` |
| Go | `go 1.26.0`（`Dockerfile` 基础镜像却是 `golang:1.23-alpine`，见第 11 节待核） |
| 外部依赖 | 仅 3 个：`go-redis/v9`、`golang.org/x/sys`、`modernc.org/sqlite` |
| 入口 | `cmd/` 8 个：`app` `credit` `launcher` `login` `server` `signin` `traeprobe` `trial` |
| 内部包 | `internal/` 22 个，见下表 |
| 文档 | `README.md` 约 1120 行 / 98 KB，含「关键断言 ↔ 代码出处」对照表 |
| 默认端口 | **7863**（Docker / Compose / Windows exe / 源码四种形态一致） |
| 部署形态 | Docker Compose（`TZ=Asia/Shanghai`）、GHCR 镜像、Windows 单文件 exe、`go run ./cmd/server` |

`internal/` 分层：

| 层 | 包 |
| --- | --- |
| 装配 / 配置 | `appserver`（唯一装配点）、`appdir`、`livecfg` |
| 网关主链路 | `server`（路由/编排）、`upstream`（上游适配）、`httpauth` |
| 账号 | `auth`、`pool`、`session`、`redisstore` |
| 定时 / 运营 | `scheduler`、`panel`、`codearts`、`ideauth`、`doubao`、`minimax` |
| 旁路通道 | `traechat`、`mmapi` |
| 可观测 | `usage`、`reqlog`、`logfmt`、`prompt` |

装配顺序（`internal/appserver/main.go`）：`auth.LoadDir` → `redisstore.New` →
`pool.New` → `session.New` → `upstream.New` → `scheduler.New` → `usage` / `reqlog` →
`panel.New` → `server.NewHandler` → `:438` 起 `http.Server`。

**一句话数据流**：

```
OpenAI 兼容请求 → server.Handler（鉴权/限体/提示词改写/模型前缀解析）
→ session.Router（粘性绑定）→ pool.Pool（加权选号/熔断/冷却/在途租约）
→ upstream.Client（payload 改写 + headers 造头）→ copilot.tencent.com/v2/chat/completions（SSE）
→ sse.go 白名单重建/聚合 → 回客户端
旁路：指标进 reqlog/usage，状态进 data/state.json 与可选 Redis 快照
```

---

## 2. 两套彼此独立的账号体系（最容易误判的一点）

| | 主网关号池（chat 路由） | IDE 账号池 |
| --- | --- | --- |
| 入口 | `cmd/login`、`panel/login.go`、`login.sh`、`panel/import.go` | `internal/ideauth`、`internal/doubao`、`panel/ide_*` |
| 凭据文件 | `auths/workbuddy-<uid>.json` | `data/ide_accounts/*.json` |
| 内存模型 | `auth.Auth` → `pool.Pool` → `server.Handler` | `QoderSnapshot` / `WorkBuddySnapshot` / Cookie 快照 |
| 用途 | `/v1/chat/completions`、token 刷新、选号 | 各 IDE 签到、一键切号、客户端登录态捕获 |
| **读本机加密存储** | **否，必须自己 OAuth 登录** | **是，但仅 Windows** |

### 2.1 登录：设备授权的变体（不是标准 RFC 8628，无 PKCE）

`cmd/login/main.go:1-18` 文件头直接定义协议：

```
POST {base}/v2/plugin/auth/state?platform=CLI   → {state, authUrl}   （state 落 /tmp/wb2api-login-state.json，0600）
GET  {base}/v2/plugin/auth/token?state=         → {accessToken, refreshToken, expiresIn, domain}
GET  {base}/v2/plugin/login/account?state=      → {uid, enterpriseId, nickname}
```

CN base `https://copilot.tencent.com`，global `https://www.workbuddy.ai`；
`Origin/Referer` 分别为 `https://www.codebuddy.cn` / `https://www.workbuddy.ai`；
UA `CLI/2.63.2 CodeBuddy/2.63.2`。`doJSON` 统一解 `{code,msg,data}` 信封。

输出 JSON 字段：`access_token, refresh_token, expires_in, domain, realm, uid,
enterprise_id, nickname`。**没有 cookie、没有 machineId** —— `machineId` 是
`internal/upstream/desktop.go:51` 由 uid 稳定派生的 36 位 hex 指纹，不是读来的。

Web 面板版（`panel/login.go`）把 state 存进程内 `p.logins`（不再落 /tmp，为 Windows 兼容），
前端每 3s 轮询 `login/poll`；`validUID`（`:72`）只放行 `[A-Za-z0-9_-]` 且 ≤64 字符，
防 UID 拼文件名路径穿越。登录成功后 `Pool.Add` + `Revive` **热加载进池，无需重启**。

### 2.2 凭据落盘：明文 JSON + 0600 原子写

`auth.Auth`（`internal/auth/auth.go:22-46`）核心字段：
`AccessToken / RefreshToken / ExpiresAt / Domain / realm / UID / EnterpriseID / Nickname / FilePath / DeviceToken`。

`SaveAtomic()`（`:309`）：持锁 → 空 accessToken 拒写 → `json.MarshalIndent` 成嵌套形
`{"account":{...},"auth":{...}}` → `WriteFile(tmp, 0o600)` → `os.Rename`。
`LoadDir`（`:361`）扫 `workbuddy*.json`，解析失败静默跳过，并按 domain 后缀
`BackfillRealm()` 补写 realm 做存量迁移。

> **权限不一致缺口**：`login.sh:143` 首次落盘用 Python `open(path,"w")`，
> 无显式 chmod，遵循 umask（通常 0644）。README `:957` 自己承认这点，建议手动
> `chmod 600 auths/*.json`。Go 侧才是 0600。

### 2.3 token 刷新：两段式并发安全

`internal/upstream/client.go:930` `RefreshToken`：

- `POST {chatBase}/v2/plugin/auth/token/refresh`，头 `X-Refresh-Token` +
  `X-Auth-Refresh-Source: plugin` + `X-Enterprise-Id` + `X-Domain` + `X-Device-Token`。
- **两段式**：锁内读 `rtSnapshot/atBefore` → 网络 I/O 在锁外（30s 超时）→
  锁内 AND 校验 `a.AccessToken != atBefore && a.RefreshToken != rtSnapshot`，
  并发刷新已完成就丢弃本次结果（`:986`）。
- `ExpiresAtFromExpiresIn`（`:908`）：`expiresIn <= 0` 或 `>= 10 年` 视为脏数据返回 0，
  保留旧值 —— 分别防「刷新风暴」和「token 静默失效」。注释记录实测 `expiresIn=5184000`（60 天）。
- 只有 `tok.RefreshToken != ""` 才更新 RT（缺省保留旧值）。

触发点两处：
- 请求内按需：`server/handler.go:866`，`NeedsRefresh(RefreshSkew=10m)` → 刷新 →
  `SaveAtomic()`；`ErrSessionDead` 直接 `Pool.Disable`，其它错误 `NoteError` 喂熔断。
- 定时保活：`scheduler.RunKeepaliveNow()` 默认 22:00；12153 `session dead`
  **连续 3 次**才禁用（`state.go:32`），成功则 `ClearSessionDead`。
  设计动机写在注释里：一次 12153 常是网络抖动，历史「一次即禁用」误伤了 13 个号。

### 2.4 号池：加权随机 + 多层硬过滤（不是简单轮询）

`pool/pick.go:45` `pick()` 流程：

1. 过滤：请求级 `tried`、realm 分池、`healthyForModel`、积分保底 `credit_floor`、
   `max_in_flight`、过期 `modelCooldowns` 惰性清理；
2. **成本分层** `costTier`：tier0 实测免费 / tier1 无观测 / tier2 实测收费，硬过滤只留最优层；
   另有 `costExploreInterval=30m` 条件探索，把未知号搭车试一次；
3. 权重 `weightOf`（`:408`）：
   `1.0 + credits/maxCredits×10 + min(闲置小时×idle_weight_per_hour, idle_weight_max)`，
   `prefer_expiring` 再乘 `expiringVirtualSlots(3)`；
4. Top5 截断 + 加权随机（定点累加抽签），等权重时 Fisher-Yates 洗牌防字典序饿死；
5. `minPickGap=100ms` 防惊群，Top5 全用过则按 `usedSeq` LRU 兜底；
6. 全冷却时 `pickEarliestExpiryLocked` 取最早解冻者顶班。

状态持久化 `data/state.json`（0600 tmp+rename，5s flusher），可选镜像到 Redis。
错误处置有完整矩阵：429 账号级软冷却（有 `resetAt` 就对齐墙钟，否则指数退避，
封顶 2h）、6004 **模型级**独立冷却（不污染账号级）、11102 模型负缓存（6h 起、封顶 24h）、
熔断 `fails>=3` → `breakerUntil = now + cooldown×2^retryCount`（封顶 6h）、
11140 直接禁用并标 `account banned by upstream`。

企业版识别 = `EnterpriseID != ""`（`auth.go:193`）；企业号跳过五类成长任务
（无个人成长体系，打了必然触发风控），但选号、保活、额度查询照常，额度改走
`get-enterprise-user-usage`。realm 分池见 `pool/realm.go`。

### 2.5 【重点】是否从本机加密存储读现成 token？

**必须分两层回答，不能一概而论：**

- **主网关号池：完全没有。** `auth.LoadDir` 唯一入口是 glob 明文 JSON；
  `auth` / `pool` / `session` / `server` 里没有任何 `os_crypt` / `keyring` / DPAPI /
  `Local State` / `state.vscdb` 调用。容器化 Linux 下只能 OAuth 或手工导入 JSON。
- **IDE 账号池：确实实现了，但全是 Windows 专属。**
  `internal/ideauth/doc.go` 包级自述：

  | 客户端 | 读取源 | 解密链 |
  | --- | --- | --- |
  | Qoder | `%APPDATA%\com.qoder.app.stable\Local State` 的 `os_crypt.encrypted_key` + `auth.v1.dat` | base64 → 校验 `DPAPI` 前缀 → `CryptUnprotectData` → 32B key → AES-256-GCM 解 `"v10"+iv12\|ct\|tag16` |
  | 豆包 | `Doubao\User Data\Local State` + `Default\Network\Cookies`（SQLite） | DPAPI → AES-256-GCM 解 `encrypted_value`（明文前 32B 是校验块） |
  | Trae | `storage.json` 的 `iCubeAuthInfo://icube.cloudide` | 自研 byteCrypto：`header(6)\|seed(32)\|AES-128-CBC(SHA512(plain)\|plain)` |
  | WorkBuddy 桌面端 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\<id>.info` | **明文 JSON**；token 用 keyblob envelope 但 `keyId` 跨账号相同，可整份备份/写回实现「真实切号」 |

  `win.go` 带 `//go:build windows`；`win_other.go` 在 Linux/macOS 直接返回
  「DPAPI 解密仅 Windows 可用」。`state.vscdb` 只有路径常量
  （`workbuddy.go:574`），**全仓无调用者，是死路径**。SQLite 唯一真实用途是
  读豆包 Chromium Cookies 库，与网关状态无关。

> **结论**：项目用的是「解密客户端已有的加密文件」，**不是**「用系统钥匙环保管自己的凭据」；
> 没有使用任何 libsecret / Keychain / Credential Manager。解出的明文又写进
> `data/ide_accounts/*.json`，等于把加密存储里的凭据再复制一份明文出来。

### 2.6 会话粘性：纯内存 + Upstash Redis 异步镜像，不是 sqlite

`session/session.go:1-9` 明确「纯内存 + redisstore 异步镜像」。
`Router.entries map[string]entry` 是权威；Redis 只做 `SetBind/DelBind` fire-and-forget +
启动时 `LoadFromStore()` 恢复一次，`keyTTL=7d`，未配置或 Ping 失败降级 `Noop`。
粘性键 `ExtractKey`（`:335`）顺序：`metadata.conversation_id` → 顶层 `conversation_id` →
`prompt_cache_key` → 派生键 `SHA256(system + 首条 user)` 前 16 字节。

> README `:623` 仍写 `metadata.user_id` 在提取顺序里，但代码 `P1-anti-monopoly`
> （`:314-318`）已**显式剔除 user_id**（user 维度过粗会把一个 user 的并行对话全钉到同一账号）。
> 以代码为准。

---

## 3. 定时调度：不依赖 systemd / crontab

### 3.1 排程全景

`internal/appserver/main.go:397-428` 用 `signal.NotifyContext` 统一控制生命周期，
所有排程都是**进程内 goroutine + time.Timer**。主号池六类（`scheduler.go:219` `taskKind`）：

| 任务 | 开关 | 默认时点 | 上游接口 | 作用 |
| --- | --- | --- | --- | --- |
| 签到+余额刷新+解冻 | `checkin_enabled` | `[9,21]` | `POST /v2/billing/meter/daily-checkin`、`get-user-resource` | 签到；余额>0 解冻冷却号；末尾跑连登管家 |
| 活跃上报 | `activity_enabled` | `[10]` | `POST /v2/report`（`chat_request_send` 事件数组） | 点亮连登、解锁 `first_buddy`，每号每天 1 次 |
| 猫猫旅行 | `travel_enabled` | `[9,21]` | `/activity/growth/buddy/*` 六个端点 | 领养 +300c、到站领积分 |
| token 保活 | `keepalive_enabled` | `[22]` | `POST /plugin/auth/token/refresh` | 续期（见 2.3） |
| 夜猫子 | `blackcat_enabled` | `[23]` | 查任务差额 → `glm-5.2` 真实对话 + `/v2/report` | 23:00–08:00 窗口内 3 次对话 |
| 成长任务队列 | `growth_enabled` | `[1]` | tasks list/accept/claim 一整套 | 01:00 因 Sequential 族零点解锁一环 |

另有 3 个独立排程（不在 `taskKind` 里）：IDE 号池签到（`panel/ide_auto.go:30`，
默认 `[10]`）、Trae 签到（`trae.auto_hours`，默认 `[]` = 仅手动）、余额后台刷新
（`StartBalanceRefresh`，5 分钟）；以及 Qoder 补试循环（`appserver/qoder_retry.go:25`，
启动 3 分钟后 + 每 30 分钟）、MiniMax 凭证续期（每 30 分钟）。

**CodeArts 不在任何自动排程里** —— `ideCheckinAll()` 只调 Qoder / WorkBuddy IDE /
MiniMax / 豆包，`appserver/main.go` 全文无 `codearts` 关键词，它只有面板手动「一键签到」。

### 3.2 调度器为什么不用 cron

`scheduler.go` 的两个细节值得记：

- `waitSlot`（`:334`）：**分段等待**，每段 60s 醒来用墙钟 `time.Until(next)` 重判。
  解决 macOS/Windows Modern Standby 冻结单调时钟导致 timer 顺延、错过整点；
- `awaitWakeupGrace`（`:300`）：迟到补跑前给 DNS/网络栈 5s 恢复，准点触发零延迟。

`nextWake`（`:232`）把同一小时的多类任务合并成一批，`runBatch`（`:397`）各起一个
goroutine 并行 + `wg.Wait()`。`Reconfigure`（`:161`）支持**改时点不重启**，
用 `rearmSchedule` / `rearmBalance` 两个独立 channel（同 channel 会被两个 select
消费丢信号）。`SetGrowthHook` 事后挂载 panel 的执行器（panel 构造晚于 scheduler）。

配置项（`appserver/config.go:298`）：`*_hours` 数组 + `*_enabled` 开关 + `*_disabled`
命名（零值 Config 即全部启用，向后兼容）；「空数组/null = 回落默认，**不是禁用**」；
小时非法值启动即报错；全禁用时 `Run` 阻塞等 channel，不空转。

> 对照：我们的 systemd timer + `Persistent=true` 在**开机补跑**上等价且更简单，
> 但没有「休眠唤醒后的墙钟重判」和「不重启改时点」。第 8 节有逐项对比。

### 3.3 幂等：四层 + 假成功识别（最值得抄的部分）

| 层 | 实现 |
| --- | --- |
| ① 上游业务码 | `IsAlreadyCheckin`（`client.go:2208`）只认带分类的 `*Error`「已签到/already」；网络层错误**不得**当成已签 |
| ② 池内当日标记 | `pool/cooldown.go:52` `NoteCheckinDone(uid)` 写 `lastCheckinDay`，跨零点自然过期 |
| ③ 本地记录 + 平台信任线 | `ideauth/checkinlog.go:177` `CheckinRecordTrusted`：Qoder 用**今日 10:05** 分界，其它平台用今日 00:00；10:05 前写的旧记录一律不采信 |
| ④ 显式幂等键 | CodeArts `idempotentKey`、连登/抽奖 `client_token`(UUID)、global trial 幂等码 `14051`、MiniMax `claim_result=2` |

**比幂等更关键的是识别「假成功」**：

- `AcceptTasksDetailed` 必须解析 `data.results[]` —— 上游 HTTP 200 + `code=0`
  但把失败藏在数组里（`tasks.go:306-311`）；
- 活跃上报后回读 `GET /activity/growth/streak`，`days==0` 打
  `report OK but streak.days=0 (silent drop?)`（`scheduler.go:559`）；
- 成长任务进度**有界轮询** `claimPollAttempts=4 × 3s`（`autotask.go:347`），
  等上游异步计分落定再领奖（注释实测对话后 5–8 秒进度才变）；
- accept 登记回读 `accept_status`，未生效重试一次（`autotask.go:397`）；
- Qoder `blocked` **绝不写 `MarkCheckedIn`**，还 `UnmarkCheckedIn` 清旧误记
  （`qoder_pool_checkin.go:220`，注释：「那是假签到：本号压根没领到」）；
- CodeArts 校验 CDP 端口归属 `caPortBelongsTo`，防「拿别人会话替这个号签到」。

失败隔离：`retryBillingTransient`（`report.go:83`）只对 5xx / 网络错误有界重试
2 次（2s、4s），业务错误（已签 / 4xx / 限流）**不重试**；单账号失败不影响其它号；
领养门槛未达按 CST 自然日每号只试一次；`12153` 连续 3 次才禁用。

---

## 4. 多平台签到实现逐一拆解

### 4.1 WorkBuddy 主号池（CN / global）

`upstream/client.go:2198` `DailyCheckin`：单次
`POST {billingBase}/v2/billing/meter/daily-checkin`，body `{}`。
CN 域 `www.codebuddy.cn` 单路径；global 先试 `/billing/meter/daily-checkin`，
404 才 fallback V2（`checkinMeterPaths`，`:770`）。

`BillingHeaders`（`headers.go:375`）：`Authorization: Bearer` + `X-User-Id` +
`X-Enterprise-Id` / `X-Tenant-Id` + `X-Domain` + `Accept-Language` +
`X-Device-Token`（`injectDeviceToken` 风控头）。

`cmd/signin/main.go` 是一次性批量工具：遍历 `auths/workbuddy-*.json` →
`NeedsRefresh(2h)` → `RefreshToken` + `SaveAtomic` → `DailyCheckin` → `UserResource`；
`signin.sh` 只是 `go build` 后 `exec`。`login.sh:77` 在 OAuth 成功后**顺手签一次**，
注释「幂等不阻塞」。

**三类域不能混**（README `:840`）：

| 域 | 用途 |
| --- | --- |
| `copilot.tencent.com` | 聊天 SSE、token 刷新、OAuth、模型列表、growth 域（旅行 / streak） |
| `www.codebuddy.cn` | 每日签到、余额查询、活跃上报 |
| `www.workbuddy.cn` | **任务领奖专用域**（领奖能成功的关键），带 `x-client-platform: web` + 对应 Origin/Referer |

> 关于 `silent-poll` / `growth-poll`：**全仓不存在这两个标识符**。我们上游 `signin.py`
> 里的概念，在参考实现里对应的是 3.3 的「有界轮询等异步计分」和「上报后回读自检」，
> 不是签到本身的状态轮询。签到是单次 POST + 有界重试。

### 4.2 WorkBuddy IDE（另一套接口）

`internal/ideauth/workbuddy.go:517`「先查再领」：

```
GET  {endpoint}/v2{workBuddyPrefixPath}/checkin/status
POST {endpoint}/v2{workBuddyPrefixPath}/checkin/claim   body {}
```

### 4.3 Qoder 每日 100 Credits

`internal/ideauth/qoder_checkin.go:35`：

```
域名：国内 https://openapi.qoder.com.cn / 国际 https://openapi.qoder.sh（国际优先、国内兜底）
GET  /sash/api/v1/me/campaigns                       找 actionType=CLAIM_BENEFIT && claimStatus=CLAIMABLE
POST /sash/api/v1/me/campaigns/{cid}/claim   body {}
另有 /api/v1/deviceToken/refresh、/api/v1/me/usage
```

每日 10:00（UTC+8）刷新 `campaignId`。风控头 `Cosy-ClientType / Cosy-MachineOS /
Cosy-MachineHostname / Cosy-MachineId / Cosy-MachineToken / Cosy-MachineCode /
Cosy-MachineType / Cosy-Version` + `User-Agent: Qoder/claim`（`:62-79`、`:340`），
指纹由客户端 `resources/umid/runtime-info.exe --account-stdin` 生成。
**与我们 `qoder_runner.py` 里 patch 的 `Cosy-*` 头是同一套**，方向一致。

**为什么「号池里两个号只有一个能领到 100 积分」**（README `:990-1047`）：

- 实测返回 `{"status":"BLOCKED","failureCode":"SAME_PERSON_ALREADY_CLAIMED",
  "benefit":{"kind":"CREDITS","amount":100}}` —— 是 `SAME_PERSON` 不是 `SAME_DEVICE`；
- 换 `Cosy-MachineId` / 去 `MachineToken` / 改 hostname / 改 UA / 换 profile 全部无效，
  连 `grantId` 都相同；
- 根因：服务端认「人」用登录时下发的 **`agentAccountScope`**
  （`ideauth/qoder.go:116` `QoderScope`，从 `qoder-data.v1.json` 读）；
  面板直接写登录态切号**不会重新签发 scope**，第二个号沿用第一个号的 scope → 算同一个人。
  要拆开必须每个号各自真登录一次。
- 落地：`QoderCheckinPoolOrdered(emails, priority)` 让指定号「先打先得」；
  某号列表里没今天的活动时，拿**同一人另一个账号的活动实例 id** 再领一次，
  向服务端要真实判定；`blocked` 不写本地已签。
- 防陈旧活动误报：`qoderCampaignIsToday`（`startAt > 今日 00:00 CST`）、
  `qoderClassifyCampaigns` 分 `claimable / claimedToday / staleClaimed`。

**对我们的意义**：本机只有单账号，暂不受影响；但若以后加号，必须每个号独立登录，
不能复用同一份 `auth.v1.dat` 切号。另外它的 30 分钟补试循环值得抄——
每日活动不是 10:00 准时下发，我们首次触发取 10:05~11:05 随机窗口、白天多次兜底，已经隐含覆盖了这个思路。

### 4.4 Trae / MiniMax / 豆包（速览）

| 平台 | 端点与鉴权 | 幂等 / 指纹 |
| --- | --- | --- |
| Trae | `https://api.trae.cn/trae/api/v2/ug/checkin_credits/{status,claim}`，`Authorization: Cloud-IDE-JWT <jwt>`，body 恒 `{}` | `device_id / market_user_id / session_id` 必须按 `user_id` **确定性派生** `SHA-256(salt:seed + uint32BE(i))`，随机会被拒 |
| MiniMax | `GET {cloud}/signin/status?timezone_id=Asia/Shanghai`、`POST .../signin/claim`、`GET .../credit/details`；`Bearer` | `claim_result` 1=claimed / 2=already；AT 仅 1h、RT 每刷必轮换 → 每 30 分钟巡检并**双向同步回客户端** |
| 豆包 | CDP 无头浏览器通道，`data/doubao_accounts.json`（0600） | Cookie 由 `extract_windows.go` 从 Chromium Cookies SQLite + DPAPI 解出 |

注：`internal/traechat/` 是 Trae **模型转发通道**，不涉及签到；
`internal/usage/` 是 token 用量分桶统计，与积分领取无关。

### 4.5 CodeArts（码道）—— 本次最重要的更正

**我们此前（DEPLOY-RESULT.md 第七节）的判断是错的**：我们顺着 IDE 插件里的
`HuaweiCloudSession` 走，解开了 AK/SK、DPoP、STS 续期、APIG 签名，
最后卡在 `snap-access` 网关要 `X-Auth-Token`，得出「只有抓运行时 token 或用账号密码
换 IAM token 两条路」的结论。

**参考实现根本不走这条路。** `internal/codearts/codearts.go:3-10` 开头就说死了：

> - 客户端（VS Code 派生）里**没有**签到接口，签到入口是控制台/网页里的
>   「活动福利 → 每日领取」，只能带**网页会话**去点；
> - 客户端本地只给 STS 临时凭证（AK/SK/securityToken，约 12h），
>   **拿它调不了活动接口**，所以凭据用浏览器登录后的 Cookie。

全仓 grep `X-Auth-Token` / `DPoP` / `IAM` / `HMAC` / AK-SK 签名代码：**零命中**
（AK/SK 仅出现在上面这段注释里）。它用的是另一套网页端 API：

```
APIBase = https://devcloud.cn-north-4.huaweicloud.com
GET  /chat/snap-manager/v1/package/overview                    积分/套餐总览 + Keepalive
GET  /chat/PromptCenterService/v1/ops/delivery?channel=WEB     活动列表（含 claimable）
POST /chat/PromptCenterService/v1/ops/claim                    领取
     body {"campaignId":N,"channel":"WEB","idempotentKey":"codearts-daily-<accountID>-<campaignID>-<day>"}
```

请求头（`internal/codearts/api.go:75-87`，已逐行核对）：

```
Cookie: <浏览器登录后抓的 Cookie 快照>
User-Agent: Chrome/156.0.0.0
Accept: application/json, text/plain, */*
Content-Type: application/json
Referer: https://devcloud.cn-north-4.huaweicloud.com/chat/home
language: zh-cn   x-language: zh-cn
x-codearts-doer-scenario-type: web
x-requested-with: XMLHttpRequest
cftk: <cookie devclouddevuibjtcftk 或 cftk>       ← 缺了会报 "invalid channel, group not found"
```

几个坑，都是实测记录：

- **会话失效的特征是 `HTTP 200 + 空 body`，不是 401/403**（`api.go:34-40`、`:100-103`）。
  只判状态码会把「会话没了」误当成「接口坏了」，账号会被反复当成可签到。
- **必须带 `idempotentKey`**：不带时当天第二次领取返回 `40001「并发冲突，请重试」`
  —— 文案像并发冲突，实际是服务端无法区分「重试」与「重复领取」（`:297-302`）。
- 每日活动识别：`type=USER_LOGIN`，标题「每日签到领100积分」（`DailyType`，`:211`）；
  `DailyCampaign`（`:261`）先按 type 再按标题含「签到」，因为列表里还有
  「学生认证领积分」这类干扰项。
- `Already()` 认 `40001 / 40002` 或文案含「已领」（`:283`）。
- `absorbSetCookies`（`:108`）吸收响应 `Set-Cookie` 续期，不用重新登录。
- 主路径 `codeartsCheckinHTTP`（`panel/codearts_browser.go:1090`）是**纯 HTTP**：
  先记余额 → `ClaimWithKey` → 等到账后算 `LastGain`；浏览器只用于**首次 CDP 登录抓 Cookie**
  和 `?openWelfare=true` 鼠标点击兜底。
- 防假签到：`caPortBelongsTo`（`:646`）校验 CDP 端口归属，防止拿 A 的会话替 B 签到。
- 号池：`data/ide_accounts/codearts_accounts.json`，`IdentityOf = domain_tag|user_tag`
  + `Fingerprint` + `MergePool` 专门修「同一个人被拆成多条 / 端口回收互相覆盖」。

**本机实测（2026-11-03 17:50，未带任何凭据）**：

```
GET https://devcloud.cn-north-4.huaweicloud.com/chat/PromptCenterService/v1/ops/delivery?channel=WEB
→ HTTP 200, body 长度 0, 0.36s
```

即：域名可达、端点存在，且**不带 token 时返回 200 空 body** —— 与参考实现
`ErrSessionExpired` 的描述完全吻合。若它真是 APIG 的 `X-Auth-Token` 网关，
不带 token 应返回 `401 APIG.0301`（就像我们之前打 `snap-access` 那样）。
这从侧面确认了：**签到走网页 Cookie 鉴权，与 IAM / AK-SK 完全无关**。

**因此第七节「若要继续只有两条路」应更正为三条，且第三条才是正解**：
用 CDP 打开码道网页端登录一次，抓 Cookie（含 `devclouddevuibjtcftk`），
之后纯 HTTP `ops/delivery` → `ops/claim` 即可，不需要 `X-Auth-Token`、
不需要 AK/SK 签名、不需要 DPoP/STS。我们已写好的 `codearts_token.py`
（会话解密 / 续期 / 签名）在这条路上**用不上**，但它本身无错，可作为
「万一以后要调 `snap-manager` 类接口」的备用能力保留。

> 仍未实现签到、未接 systemd、未进看板：抓 Cookie 需要一次真实浏览器登录，
> 属于要动凭据的动作，本轮按「只读调研」处理，没有擅自执行。
> 要做的话，第 9 节列了最小步骤。

---

## 5. 成长任务体系（README 标题「17/18」）

官方成长计划 18 个任务里 **17 个可纯 API 一键完成**，剩余 1 个
`Expert_Philanthropy` 需真实捐款（服务端领奖时校验捐赠回执，实测无法绕过）。
README 表格实际列 16 行、合计约 `+1950 credits +78 能量`，`autoActions`
静态表有 21 个动作（含 `black_cat`、`school_season`、`Sequential_Tasks_1..7`、
国际版 checklist 4 项）—— 数字口径不严格一致，以「1 个不可自动」为准。

实现 `panel/autotask.go:53` `autoActions` + `runAutoAll`（`:1446`）：
拉任务列表 → 批量 `AcceptTasksDetailed` → 逐项跑行为事件链 →
`taskByCodeWaiting` 回读 → `claimCompletedTasks` 领奖。
任务接口 `upstream/tasks.go:29`：

```
GET  /v2/activity/growth/tasks                         （CLI 域，带 /v2）
POST /v2/activity/growth/tasks/accept
POST {www.workbuddy.cn}/activity/growth/tasks/<task_code>/claim   （Web 域，无 /v2）
```

计分事件链在 `upstream/desktop.go`（CLI / 桌面 / web 三套指纹，
`chat_request_send` 事件 30+ 字段全量照抄客户端形状，**必须带 `userId`**，
否则 200 但静默丢弃）。

额外四个任务：

| 任务 | 实现 | 要点 |
| --- | --- | --- |
| 连登管家 | `scheduler/streak.go:40` | 补签卡（`heatmap` 找 score=0）→ 新手礼包 → 补偿 → 查 7/14/28 档位 → 逐档 `GrowthRedeemTier` → 抽奖次数抽完；`client_token` UUID 幂等 |
| 活跃上报 | `scheduler.go:509` | `POST /v2/report` 事件数组，`cid = "wb2api-"+毫秒时间戳`，无需真实会话；成功后回读 streak 自检；账号间限速 800ms |
| 猫猫旅行 | `scheduler/travel.go` | 状态机 `idle/traveling/arrived`，单趟只做一个动作不轮询；`location_id=4`（4 个地点收益相同）；领养链路 `ReportChatActivity` → 1050ms → `BuddyAgreement` → `BuddyFirst`(+300c)；`cstZone` 用 `time.FixedZone("CST",8h)` 不依赖容器 tzdata |
| 夜猫子 | `scheduler/blackcat.go` | `InNightWindow = h>=23 || h<8`，先查 `black_cat` 差额，再 `glm-5.2` 真对话 3 次（每次 sleep 4s）+ 上报；唯一整任务都是真实模型对话的任务 |

**我们目前只做了「签到 + 成长轮询」两项**，与它 17 项的差距就在这里。
不过这些任务多数需要**真实模型对话**或**网页端行为事件**，在「不落盘凭据 +
纯 systemd 定时」的约束下成本不低，属于可选增强，不是必须补齐。

---

## 6. 网关与面板（与我们定位正交，简述）

**对外端点**（`server/handler.go:186`）：`POST /v1/chat/completions`、
`POST /v1/responses`（OpenAI **Responses API**，适配层翻译回同一条选号管线）、
`GET /v1/models`、`GET /status`、`GET /healthz`（恒无鉴权）、`/panel/`。
`api_key` 非空时才校验 Bearer（`httpauth` 用 SHA-256 摘要 + 常量时间比较）。

> **没有**对外暴露 Anthropic `/v1/messages`，也没有 Gemini `/v1beta/*`。
> Anthropic Messages 是 **MiniMax 上游**协议（由 `internal/minimax` 转成 OpenAI 格式），
> Gemini 只是模型目录里的模型名。`trae:` / `minimax:` / `mm:` / `global:` 是
> 网关侧**模型名前缀路由**（`server/resolve_model.go:16`），不是对外协议。

**出站改写管线**（`upstream/payload.go:30`）：强制 `stream:true`、
`translateMaxCompletionTokens`、clamp、`stream_options.include_usage`、
`normalizeToolChoice/Roles/ImageURL`、`injectThinking`、
`normalizeReasoningEffort`、`backfillReasoningContent`、`sanitizeMessages`，
并 `InjectPromptCacheKey(body, uid, conversationID)` 做前缀缓存省钱。

**流式**：`sse.go` 白名单重建帧（`normalizeFrame`，`:387`）、按 index 合并
`tool_calls`、保证恰好一个 `data: [DONE]`（上游漏发兜底补写）、空流先写 error 帧；
非流式 `Aggregate` 把整条 SSE 聚成单个 `chat.completion`。
聊天用 `Timeout=0` 的专用 client，长思考无总时长上限。

**内容拦截降级**：`server/degrade.go` + `handler.go:947` —— 遇
`ErrContentBlocked` 且本次未降级 → `prompt.Rewrite(body, prompt.Degraded)` +
换号**同请求内重试**，降级状态持续到次日 00:00 CST。

**原生工具调用标记还原（DSML）**：`upstream/dsml.go:50` 用全角竖线
`\uFF5C\uFF5C` 拼标签，`MarkupRepair` 把命中帧拆成「正文帧 + N 调用帧 + 收尾帧」，
`finish_reason: stop` 改写为 `tool_calls`。

**面板**：`panel/index.go:16` 用 `go:embed` 内嵌 `index.html` + `app.js`
（不是 README 说的「单文件」），带严格 CSP（`default-src 'none'`、
`frame-ancestors 'none'`）+ `nosniff` + `X-Frame-Options: DENY`。
9 个视图：账号池 / 签到池 / 用量 / 积分构成 / **任务中心** / IDE 账号池 /
模型与档位 / 配置 / 运行日志。`/panel/api/*` 约 80 个路由，按「只读总览 /
登录导入 / 单账号运维 / 单账号任务 / 任务中心 / 批量保号 / Trae / Qoder IDE /
MiniMax IDE / Doubao IDE / CodeArts / WorkBuddy IDE」分组。
配置热生效：`api_key`、`cooldown.*`、`features.*`、`pool.*`、`schedule.*` 立即生效；
`listen`、`auth_dir`、`upstream.*`、`upstash.*` 需重启。

**存储澄清**：主体是本地 JSON/JSONL（`state.json` / `usage.json` /
`requests-*.jsonl` / `auths/*.json`）；Redis 是**可选**镜像（跨重启粘性与状态快照）；
SQLite **只在 Windows 上读豆包 Chromium Cookies 库时用一次**。
三者不是「两套并存的主存储」，只是恰好都在依赖清单里。

---

## 7. 与本机方案的逐项对比

| 维度 | 参考实现 | 本机方案（`workbuddy-auto-signin/local/`） | 评价 |
| --- | --- | --- | --- |
| 定位 | 2API 网关 + 多平台账号运营 | 只做每日领取 + 看板 | 目标不同，不必对齐 |
| 语言/进程 | Go 单进程常驻 | Python 脚本 + systemd oneshot，跑完即退 | 本机更轻、更符合「不落盘」 |
| 调度 | 进程内 goroutine，墙钟分段校正，不重启改时点 | 3 个 user timer + `Persistent=true`，`linger=yes` | 开机补跑等价；参考实现多了休眠唤醒校正与热改配置 |
| 凭据获取 | 自己 OAuth，明文 `auths/*.json`（0600 原子写） | 运行时从 Electron 加密存储 + Secret Service 解密，**不落盘** | **本机安全性明显更强**，且无需维护独立登录态 |
| 凭据生命周期 | 需自己管 refresh / 轮换 / 保活 / 禁用 | 复用客户端登录态，客户端自己续期 | 本机少一整块状态机 |
| 号池 | 加权随机 + 熔断 + 模型级冷却 + 会话粘性 | 单账号，无池 | 单账号场景下号池是纯负担 |
| 幂等 | 四层 + 假成功识别 | 依赖上游 `ALREADY` + systemd 退出码 | **可借鉴**（见第 8 节） |
| 风控指纹 | Qoder `Cosy-*`、Trae 确定性 SHA-256、CodeArts `cftk`、账号间限速 | Qoder 已 patch `Cosy-*` 设备头 | 方向一致 |
| 平台覆盖 | WorkBuddy / Qoder / Trae / MiniMax / 豆包 / CodeArts | WorkBuddy + Qoder（CodeArts 调研中） | 参考实现覆盖更广 |
| 成长任务 | 17 项 + 连登/旅行/夜猫子 | 签到 + growth 轮询 | 可选增强 |
| 看板 | 内嵌 9 视图 + ~80 API，端口 7863 | 独立 Node 看板挂「每日自动领取」面板，端口 3100 | 用户已要求简化，保持现状 |
| 可观测 | `reqlog` 脱敏归档 + `usage` 分桶 + 结构化日志 | journalctl + 上游 `signin.log` 解析 | 够用 |

---

## 8. 值得借鉴 / 完全不需要

### 8.1 值得抄（按性价比排序）

1. **CodeArts 改走网页 Cookie 路线**（第 4.5 节）—— 唯一能真正把签到做成的一处，
   且推翻了现有文档结论。最小步骤：
   CDP 打开 `https://devcloud.cn-north-4.huaweicloud.com/chat/home` 登录一次 →
   导出 Cookie（含 `devclouddevuibjtcftk`）→ `GET ops/delivery?channel=WEB` 找
   `type=USER_LOGIN` → `POST ops/claim` 带 `idempotentKey=codearts-daily-<accountID>-<campaignID>-<YYYYMMDD>`
   → 吸收 `Set-Cookie` 续期。**不需要** `X-Auth-Token` / AK-SK / DPoP / STS。
2. **幂等记录带时间戳 + 平台信任线**：Qoder 用今日 10:05 分界，避免把
   「10:00 前写的旧记录」当成今天已领。我们 `status.json` 现在是纯日期，
   可以加一条时间判断。
3. **严格区分「已领 / 被同人领走 / 无活动 / 失败」**：绝不把 `BLOCKED` 写成成功。
   我们 `qoder_runner.py` 目前只有 `CLAIMED` / `ALREADY`，可以补 `BLOCKED`。
4. **执行前先查上游状态再决定打不打**（`campaigns` / `status` / `tasks`），
   而不是盲打一次靠业务码兜底。
5. **失败分类重试**：只重试 5xx / 网络错误，业务错误立即结束；账号间加限速。
6. **Qoder 30 分钟补试**：活动不是 10:00 准时下发；我们 10:05~11:05 首领 + 白天
   兜底已隐含覆盖，可保留，但日志里应区分「无活动」和「失败」。

### 8.2 完全不需要（按包）

| 类别 | 可整体删 | 理由 |
| --- | --- | --- |
| 整条 chat 管线 | `server/handler.go` 主体、`responses*.go`、`upstream/payload.go`、`sse.go`、`dsml.go`、`thinking.go`、`sanitize.go`、`prompt/`、`server/degrade.go`、`/v1/models` | 我们不对外提供 LLM 补全 |
| 号池治理 | `pool/`、`session/`、`redisstore/` | 签到是逐号定时执行，不需要为每次请求挑号、不需要粘性 |
| 上游模型通道 | `minimax/`、`mmapi/`、`traechat/` | 把账号当 LLM 用才需要 |
| 可观测增强 | `usage/`、`reqlog/`、`logfmt/` | 只需「哪个号、哪天、成功/失败、得多少分」 |
| 浏览器自动化 | `panel/ide_*` 的 browser handler、`autotask.go`、`taskcenter.go`、`qoder_web.go`、成长任务/旅行/连登/抽奖 | 与每日领取正交 |
| 分发壳与 SQL | `cmd/app`、`cmd/launcher`、`cmd/traeprobe`、`cmd/trial`、`appdir/`、`modernc.org/sqlite` | 单进程 + 无豆包 Cookie 提取 |

> 结论：**抽取「签到 API + 调度模型 + 内嵌页面骨架」，不要 fork 裁剪。**
> 而且它的签到逻辑分散在 `upstream/client.go`（主号池）、
> `ideauth/qoder_checkin.go`（Qoder）、`upstream/trae.go`（Trae）、
> `panel/ide_*` + `doubao/` + `codearts/`（其余）、`scheduler/`（调度）、
> `panel/panel.go:241`（入口）—— 想只抄「签到」也得跨 6 个包。

---

## 9. 关键文件索引（相对参考仓库根）

| 主题 | 路径 |
| --- | --- |
| 装配 / 生命周期 / 配置默认 | `internal/appserver/{main,config}.go` |
| 调度主循环 / 墙钟校正 | `internal/scheduler/scheduler.go` |
| 连登管家 / 猫猫旅行 / 夜猫子 | `internal/scheduler/{streak,travel,blackcat}.go` |
| 成长任务动作表 / 有界轮询 | `internal/panel/autotask.go` |
| 任务队列 | `internal/panel/taskcenter.go` |
| IDE 号池自动签到 / Qoder 补试 | `internal/panel/ide_auto.go`、`internal/appserver/qoder_retry.go` |
| 主号池签到 / 域路由 / token 刷新 | `internal/upstream/client.go` |
| 请求头 / 活跃上报 / 重试 | `internal/upstream/{headers,report}.go` |
| 任务列表 / accept / claim | `internal/upstream/tasks.go` |
| 连登兑换 / 旅行 / 夜猫子 API | `internal/upstream/{streak,travel,blackcat}.go` |
| Trae 签到 | `internal/upstream/trae.go` |
| Qoder 端点 / Cosy 风控头 | `internal/ideauth/qoder_checkin.go` |
| Qoder「先打先得」/ blocked | `internal/ideauth/qoder_pool_checkin.go` |
| 本地签到日志 / 信任线 | `internal/ideauth/checkinlog.go` |
| Qoder DPAPI/os_crypt（Windows） | `internal/ideauth/{doc,win,win_other,crypto_common,qoder}.go` |
| **CodeArts 网页 API / cftk / 幂等键** | `internal/codearts/api.go` |
| CodeArts 号池 / CDP 登录 | `internal/codearts/codearts.go`、`internal/panel/codearts_browser.go` |
| 凭据模型 / 原子写 | `internal/auth/auth.go` |
| 号池选号 / 状态机 / 冷却 | `internal/pool/{pick,entry,state,cooldown,transition}.go` |
| 会话粘性 | `internal/session/session.go` |
| 面板静态资源 / 路由表 | `internal/panel/{index.go,panel.go}` |
| 命令入口 | `cmd/{login,signin,credit,trial,server}/main.go` |
| 配置样例 / 容器时区 | `config.example.json`、`docker-compose.yml` |

---

## 10. 只读标注：参考仓库自身的待核差异（未修改）

1. `go.mod:3` 声明 `go 1.26.0`，`Dockerfile:2` 却是 `golang:1.23-alpine`；
   本轮未编译，不断言构建结果，部署前需核实。
2. `prompt.mode` 默认两层不一致：`config.go:324` / README 均为 `passthrough`，
   但 `server/handler.go:182` 在 `Config{}` 裸构造时置为 `custom`（仅影响单测）。
3. README `:729` 说面板是「go:embed 单文件」，实际 `index.go:16` 嵌入两个文件。
4. README `:623` 的粘性键仍含 `metadata.user_id`，代码已剔除（见 2.6）。
5. README `:813` 写「请求体上限 8 MiB」、`:1093` 断言「无网关侧上限」，自相矛盾。
6. README 标题「17/18」但任务表只列 16 行，`autoActions` 表 21 项，口径不严格一致。
7. README `:723` 与 `:809` 有两个「API 端点」标题，`:723` 下面实际是余额刷新说明。

---

## 11. 本轮实际改动

| 文件 | 改动 |
| --- | --- |
| `local/REFERENCE-IMPLEMENTATION.md` | 新建，本文 |
| `local/DEPLOY-RESULT.md` | 修正重复/乱序章节编号；第七节 CodeArts 增加结论更正；新增「十、参考实现调研」；速览表加一行 |

**未改动**：参考仓库任何文件、看板前端、`codearts_token.py`、systemd 单元、
上游 `88lin/workbuddy-auto-signin` 任何文件。CodeArts 签到仍未实现、未部署、未进看板。
