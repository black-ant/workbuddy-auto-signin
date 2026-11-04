# CodeArts Agent（码道）每日签到 —— 实现说明

华为云 **CodeArts Agent / 码道 IDE** 的每日签到（100 积分）自动化实现说明。
文件：`local/codearts_signin.py`、`local/codearts_token.py`、`local/run-codearts.sh`。

本文是**协议参考**。安装部署见 [linux-deploy.md](linux-deploy.md)。

---

## 0. 一分钟结论

| 问题 | 结论 |
| --- | --- |
| 要不要浏览器 Cookie / `cftk`？ | **不要**。那是网页端路线，本机 IDE 走的是另一条路 |
| `x-auth-token` 从哪来？ | **商用版是空串**。JS 层不提供 token，`Pr()` 打日志后返回 `""` |
| 真正鉴权是什么？ | 华为云 SDK 的 `SDK-HMAC-SHA256` AK/SK 签名 + `X-Security-Token` |
| 凭据从哪来？ | IDE 的 `HuaweiCloudSession`（`state.vscdb`，`v11` 加密） |
| 需要账号密码吗？ | **不需要**，也**不允许**。只用 IDE 已登录的临时凭据 |

---

## 1. 端点与请求

**HOST**（写死在 `codearts_signin.py`，商用版固定）：

```
snap-access.cn-north-4.myhuaweicloud.com
```

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/v1/ops/delivery?channel=IDE` | 查可领取的福利活动列表 |
| `POST` | `/v1/ops/claim` | 领取指定活动 |

领取 body：

```json
{
  "campaignId": 34,
  "idempotentKey": "codearts-daily-<account>-<campaignId>-<YYYYMMDD>",
  "channel": "IDE"
}
```

- `campaignId` **不要写死**，从 `delivery` 响应里 `type == "USER_LOGIN"` 的活动取
  （当前是 34，活动换季会变）。
- `idempotentKey` 按日生成，保证同日重复触发幂等。
- `channel` 必须是 `IDE`，不是 `WEB`。

---

## 2. 请求头（缺一个就报错）

```python
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", "SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s" % ...)
req.add_header("X-Security-Token", security_token)   # 临时凭据必带
req.add_header("Agent-Type", "PromptCenter")         # 漏了报 TM.00001001
req.add_header("X-Language", "zh-cn")
req.add_header("x-snap-traceid", uuid.uuid4().hex)
```

| 头 | 说明 | 漏了会怎样 |
| --- | --- | --- |
| `Authorization` | 华为云 SDK 签名串 | `401` |
| `X-Security-Token` | 临时 AK/SK 必需的会话令牌 | 鉴权失败 |
| `Agent-Type: PromptCenter` | 平台路由必需 | `TM.00001001 请求头Agent-Type为空` |
| `x-snap-traceid` | 32 位无横线 uuid，链路追踪 | 通常不阻塞，但 IDE 都带 |
| `X-Language` | `zh-cn` | 无 |

**不要**手工构造 `x-auth-token`：商用版它是空串，真正鉴权在签名里。

---

## 3. 签名：canonical URI 与真实 URL 必须分离

这是本项目踩过最久的一个坑，也是唯一一个「签名算对了但请求 404」的原因。

```python
# CanonicalURI：逐段转义、末尾补 "/"（喂给签名算法）
c_uri = "/".join(urllib.parse.quote(seg, safe="-_.~") for seg in path.split("/"))
if not c_uri.endswith("/"):
    c_uri += "/"

# 真实 URL：用原始 path，末尾不补斜杠，不加任何前缀
url = "https://%s%s" % (HOST, path)
```

正确的结果：

| 用途 | 值 |
| --- | --- |
| 真实 URL | `https://snap-access.cn-north-4.myhuaweicloud.com/v1/ops/delivery?channel=IDE` |
| CanonicalURI（仅签名） | `/v1/ops/delivery/` |

**三个禁止**：

1. 禁止把 canonical URI 直接当 URL 发（会打到不存在的 `/v1/ops/delivery/` → `404`）；
2. 禁止加 `/snap-manager` 前缀（那是旧框架路由，不在本网关）；
3. 禁止对 `path` 整体 `quote`（斜杠会被转义成 `%2F`，签名和 URL 双错）。

签名算法（华为云 SDK V11 标准）：

```
CanonicalRequest =
    METHOD \n
    CanonicalURI \n
    CanonicalQueryString \n
    CanonicalHeaders \n     # 小写头名 + ":" + trim(值)，按名排序，每个后接 \n
    SignedHeaders \n
    HexEncode(SHA256(body))

StringToSign =
    "SDK-HMAC-SHA256" \n
    X-Sdk-Date \n
    HexEncode(SHA256(CanonicalRequest))

Signature = HexEncode(HMAC-SHA256(StringToSign, secretKey))
```

`X-Sdk-Date` 格式 `YYYYMMDDTHHMMSSZ`（UTC）。签名串里用的小时/分钟必须与
`X-Sdk-Date` 完全一致，否则 `APIGW.0301`。

---

## 4. 凭据：从 IDE 登录态解出

`codearts_token.py` 负责，不需要任何配置文件：

```
~/.config/codearts-agent/User/globalStorage/state.vscdb   (sqlite)
  └─ key: secret://{"extensionId":"huaweicloud.authentication",
                    "key":"HuaweiCloudSession"}
      value: v11 加密 blob
```

解密链路：

```
v11 前缀 → 去掉前 3 字节
  → AES-128-CBC 解密
      key = PBKDF2-HMAC-SHA1(keyring 口令, salt=b"saltysalt", iter=1, dkLen=16)
      IV  = b"\x20" * 16
  → PKCS7 去填充 → JSON
```

| 要点 | 说明 |
| --- | --- |
| 盐长度 | **9 字节** `b"saltysalt"`，不是 Chromium 标准的 25 字节。`SALT_CANDIDATES` 两种都试 |
| 口令来源 | 必须走 **Secret Service D-Bus**（`gnome-keyring`），不能只读磁盘 `login.keyring`（缓存会滞后） |
| 通道 | 必须在目标用户的 session bus 里跑，root 直连会被 `EXTERNAL` 拒绝 |

解出的会话字段：

```
accessKey  secretKey  securitytoken  refresh_token
account  domainId  expires_at  loginContext.dpopKeyPair
```

`accessToken` 是空串 —— 不要拿它当 token 用。

---

## 5. 会话续期

过期时（`APIG.0602` / `APIGW.0301`）：

```
POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens
  头部带 DPoP proof（用 loginContext.dpopKeyPair 对请求签名）
  body 带 refresh_token
→ 拿到新 AK/SK/securitytoken
→ save_session() 写回 state.vscdb（带自校验）
```

`codearts_signin.py` 的 `request_with_refresh()` 会自动续期一次并重试。

**注意事项**：

- `save_session()` 失败不影响本次运行，但下次还得重新续；
- STS 返回 `504 APIGW.0201 Service STS timed out (30000 ms)` 时**不要**继续重试，
  等人工处理。**续期失败时绝不能写回**，否则会把 IDE 登录态写坏；
- 续期只能延长，不能凭空创建身份。IDE 被登出后必须人重新登录。

---

## 6. 状态映射（幂等的关键）

| 服务端返回 | 映射为 | 说明 |
| --- | --- | --- |
| `status ∈ {CLAIMED, CONFIRMED, CONSUMED}` | `ALREADY` | 今日已领，不调领取接口 |
| `claimable == true` | 调 `POST /v1/ops/claim` | 结果 `CLAIMED` |
| 错误码 `40001` / `40002` | `ALREADY` | 领取接口说「领过了」，视为成功 |
| 无 `USER_LOGIN` 活动 | `NO_CAMPAIGN` | 活动下线或未开放 |
| 会话解不开 | `NO_AUTH`（退出码 3） | IDE 需重新登录 |

**硬规则**：`claimable == false` 时**绝对不要**调 `POST /v1/ops/claim`。
IDE 显示的「今日已领」就是读 `delivery` 的 `status`，不是靠调领取接口探出来的。

输出 JSON 示例：

```json
{"result": "ALREADY", "report": "今日已领 100 积分", "credit": 100, "campaignId": 34}
```

---

## 7. 为什么不用网页 Cookie 路线

社区里常见另一种实现（如 `workbuddy2api-*-panel` 的 `internal/codearts/api.go`）：

```
HOST: https://devcloud.cn-north-4.huaweicloud.com
GET  /chat/PromptCenterService/v1/ops/delivery?channel=WEB
POST /chat/PromptCenterService/v1/ops/claim
Cookie + cftk + x-codearts-doer-scenario-type: web
```

两条路线的区别：

| | 网页 Cookie 路线 | IDE-native 路线（本项目） |
| --- | --- | --- |
| 凭据 | 浏览器 Cookie + `cftk` | IDE 的临时 AK/SK + securitytoken |
| 获取方式 | 手工从浏览器复制，会过期 | 运行时自动从 `state.vscdb` 解出 |
| 能否无人值守 | 不能，Cookie 失效需人工重抓 | 能，自动续期 |
| 适用 | 无 IDE 的服务器 / 面板 | 本机装了 CodeArts Agent |

**本机装了 IDE，就走 IDE-native**。Cookie 路线不必走，也不要把两条路的
端点、前缀、请求头混用 —— 混用必然 `404` 或 `401`。

---

## 8. 验证方法

```bash
# 只读：会话是否可用、接口是否通
./local/run-codearts.sh doctor
# → {"result":"AUTH_READY","report":"会话与接口均可用，活动 N 个"}

# 只读：看活动当前状态，不领取
./local/run-codearts.sh status
# → {"result":"OK","status":"CONSUMED","claimable":false,"campaignId":34}

# 领（幂等）
./local/run-codearts.sh auto
```

在 root / SSH 下调试必须经由用户 session：

```bash
systemd-run --user --wait --pipe --collect ./local/run-codearts.sh doctor
```

排查「是不是我领的」时，直接查 IDE 日志比猜可靠：

```bash
grep -c "claimWelfare\|ops/claim" ~/.config/codearts-agent/User/logs/CodeArts*.log
```

---

## 9. 变更风险

签到协议是从 IDE 插件
`huaweicloud.codearts-snap/out/extension.js` 逆出来的。华为发版后可能变化。

出现以下情况时，优先重读该文件的 `Pz()` / `Pr()` / `Mt.sendRequest` /
`getWelfareDelivery` / `claimWelfare`：

- 端点或域名变更（`policyCenterDomain`）
- 新增必填请求头
- `channel` 值变化
- 错误码语义变化

定位命令：

```bash
EXT=/usr/share/codearts-agent/resources/app/extensions/huaweicloud.codearts-snap/out/extension.js
grep -o "ops/delivery\|ops/claim\|policyCenterDomain[^,;]*\|Agent-Type[^,;]*" "$EXT" | sort -u
```
