#!/usr/bin/env python3
"""CodeArts Agent（码道）每日签到 —— IDE-native 路线。

与 WorkBuddy / Qoder 一样：凭据不落盘、不打印、不进仓库。运行时从 IDE 本地
加密会话解出临时 AK/SK，只用于本次进程内存中的请求签名。

技术要点（2026-11-04 读 IDE 插件 huaweicloud.codearts-snap 源码确认）：

  * 端点（商用版）：
        GET  https://snap-access.cn-north-4.myhuaweicloud.com/v1/ops/delivery?channel=IDE
        POST https://snap-access.cn-north-4.myhuaweicloud.com/v1/ops/claim
    签到活动 type=USER_LOGIN，campaignId 从 delivery 响应里取，不写死。
  * 鉴权：华为云 SDK 的 SDK-HMAC-SHA256 AK/SK 签名 + X-Security-Token。
    凭据就是 codearts_token.py 解出的 HuaweiCloudSession。
  * 商用版插件的 x-auth-token 是空串（Pr() 打日志
    "Hc version, token is transferred by the toolkit." 后返回 ""），真正鉴权在
    Mt.sendRequest → getGlobalCredentialClient().withCredentials() 的 SDK 客户端，
    所以这里不构造该头。
  * 坑 1：签名的 CanonicalURI 要逐段转义且末尾补 "/"，但**真实 URL 必须用原始
    path**。二者混用会打到不存在的 /v1/ops/delivery/ 而返回 404；
    /snap-manager 前缀是另一套旧框架路由，也不要加。
  * 坑 2：请求头必须带 Agent-Type: PromptCenter，否则 TM.00001001
    "请求头Agent-Type为空"。
  * 坑 3：安全令牌过期返回 400 APIG.0602；用 refresh_token 轮换后必须把新会话
    写回 state.vscdb（save_session 带自校验），否则 IDE 登录态会失效。

用法：
    codearts_signin.py auto      查活动 + 领取（systemd 用这个）
    codearts_signin.py status    只查活动状态，不领取
    codearts_signin.py doctor    检查会话与接口连通性
"""

import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

import codearts_token as ct

HOST = "snap-access.cn-north-4.myhuaweicloud.com"
DELIVERY_PATH = "/v1/ops/delivery"
CLAIM_PATH = "/v1/ops/claim"
CHANNEL = "IDE"

# 服务端把「已领」标成这几种状态，displayConfig 里统一显示「今日已领」
DONE_STATUSES = {"CLAIMED", "CONFIRMED", "CONSUMED"}
# 领取接口表示「已领过」的错误码，视为幂等成功
ALREADY_CODES = {"40001", "40002"}
TOKEN_EXPIRED_CODES = {"APIG.0602", "APIGW.0301"}


def log(msg):
    sys.stderr.write("[codearts] %s\n" % msg)


def emit(obj, rc=0):
    """stdout 只输出结果 JSON，供 systemd journal / 看板解析。"""
    print(json.dumps(obj, ensure_ascii=False))
    return rc


# ---------------------------------------------------------------- 签名与请求

def canonical_query(params):
    if not params:
        return ""
    items = sorted((k, str(v)) for k, v in params.items())
    return "&".join("%s=%s" % (k, urllib.parse.quote(v, safe="-_.~"))
                    for k, v in items)


def parse_body(raw):
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {"raw": raw[:400]}
    except Exception:
        return {"raw": raw[:400]}


def signed_request(session, method, path, params=None, body=None, timeout=25):
    """按华为云 SDK 规范签名并发请求，返回 (http_code, dict)。

    注意这**不是** AWS SigV4：string-to-sign 只有三行，没有 credential scope。
    """
    ak = session.get("accessKey", "")
    sk = session.get("secretKey", "")
    tok = session.get("securitytoken", "")
    if not ak or not sk:
        raise RuntimeError("会话缺少 AK/SK")

    sdk_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = "" if body is None else json.dumps(body, separators=(",", ":"))
    ctype = "application/json"

    # CanonicalURI 逐段转义、末尾补 "/"（SDK 规范），仅用于签名
    c_uri = "/".join(urllib.parse.quote(seg, safe="-_.~")
                     for seg in path.split("/"))
    if not c_uri.endswith("/"):
        c_uri += "/"
    query = canonical_query(params)

    headers = {"content-type": ctype, "host": HOST, "x-sdk-date": sdk_date}
    signed = sorted(headers)
    canonical_headers = "".join("%s:%s\n" % (k, headers[k]) for k in signed)
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = "\n".join([
        method, c_uri, query, canonical_headers,
        ";".join(signed), payload_hash])
    string_to_sign = "\n".join([
        "SDK-HMAC-SHA256", sdk_date,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signature = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    auth = ("SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s"
            % (ak, ";".join(signed), signature))

    # 真实 URL 用原始 path，不补末尾斜杠、不加 /snap-manager 前缀
    url = "https://%s%s" % (HOST, path)
    if query:
        url += "?" + query

    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", ctype)
    req.add_header("Host", HOST)
    req.add_header("X-Sdk-Date", sdk_date)
    req.add_header("Authorization", auth)
    req.add_header("Agent-Type", "PromptCenter")
    req.add_header("X-Language", "zh-cn")
    req.add_header("x-snap-traceid", uuid.uuid4().hex)
    if tok:
        req.add_header("X-Security-Token", tok)
    if body is not None:
        req.data = payload.encode("utf-8")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, parse_body(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, parse_body(e.read().decode("utf-8", "replace"))
    except Exception as e:
        return -1, {"raw": "%s: %s" % (type(e).__name__, e)}


def request_with_refresh(session, method, path, params=None, body=None):
    """签名请求；安全令牌过期则续期一次并重试。返回 (code, data, session)。"""
    code, data = signed_request(session, method, path, params, body)
    if code in (400, 401) and data.get("error_code") in TOKEN_EXPIRED_CODES:
        log("安全令牌已过期，续期会话")
        session, _rt = ct.refresh_session(session)
        # refresh_token 是轮换的，必须写回，否则 IDE 登录态会失效
        if not ct.save_session(session):
            log("续期结果写回失败（不影响本次运行）")
        code, data = signed_request(session, method, path, params, body)
    return code, data, session


# ---------------------------------------------------------------- 业务流程

def find_login_campaign(data):
    items = ((data.get("data") or {}).get("items") or []) if isinstance(data, dict) else []
    for it in items:
        if isinstance(it, dict) and it.get("type") == "USER_LOGIN":
            return it
    return items[0] if items and isinstance(items[0], dict) else None


def today_key(session, campaign_id):
    """按本地日期生成幂等键：同一天重复触发不会重复发积分。"""
    account = (session.get("account") or {}).get("id") or session.get("id") or "unknown"
    day = datetime.datetime.now().strftime("%Y%m%d")
    return "codearts-daily-%s-%s-%s" % (account, campaign_id, day)


def claim_campaign(session, campaign):
    campaign_id = campaign.get("campaignId")
    body = {
        "campaignId": campaign_id,
        "idempotentKey": today_key(session, campaign_id),
        "channel": CHANNEL,
    }
    code, data, _session = request_with_refresh(session, "POST", CLAIM_PATH, None, body)
    amount = campaign.get("benefitAmount") or 100

    if isinstance(data, dict) and data.get("code") == 0:
        return emit({"result": "CLAIMED", "report": "已领取 %s 积分" % amount,
                     "credit": amount, "campaignId": campaign_id})

    err = str((data or {}).get("error_code", ""))
    msg = str((data or {}).get("error_msg", "") or (data or {}).get("message", ""))
    if err in ALREADY_CODES or "已领" in msg or "重复" in msg:
        return emit({"result": "ALREADY", "report": "今日已领 %s 积分" % amount,
                     "credit": amount, "campaignId": campaign_id})

    return emit({"result": "ERROR",
                 "report": "领取失败 HTTP %s：%s %s" % (code, err, msg[:120]),
                 "campaignId": campaign_id}, 1)


def run_status(session, do_claim):
    # delivery 可能触发会话续期，后续领取必须用刷新后的 session
    code, data, session = request_with_refresh(
        session, "GET", DELIVERY_PATH, {"channel": CHANNEL})

    if code != 200 or (isinstance(data, dict) and data.get("code") not in (0, None)):
        err = (data or {}).get("error_code", "")
        msg = (data or {}).get("error_msg", "") or (data or {}).get("raw", "")
        return emit({"result": "ERROR",
                     "report": "查询活动失败 HTTP %s：%s %s" % (code, err, str(msg)[:120])}, 1)

    campaign = find_login_campaign(data)
    if not campaign:
        return emit({"result": "NO_CAMPAIGN", "report": "当前没有可领取的签到活动"})

    status = str(campaign.get("status") or "").upper()
    claimable = bool(campaign.get("claimable"))
    amount = campaign.get("benefitAmount") or 100

    if not do_claim:
        return emit({"result": "OK", "report": "活动状态 %s，可领取=%s" % (status or "-", claimable),
                     "claimable": claimable, "status": status,
                     "campaignId": campaign.get("campaignId")})

    if claimable:
        return claim_campaign(session, campaign)

    if status in DONE_STATUSES:
        return emit({"result": "ALREADY", "report": "今日已领 %s 积分" % amount,
                     "credit": amount, "campaignId": campaign.get("campaignId")})

    return emit({"result": "NO_CAMPAIGN",
                 "report": "活动尚未开放（状态 %s）" % (status or "-"),
                 "campaignId": campaign.get("campaignId")})


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if cmd not in ("auto", "status", "doctor"):
        return emit({"result": "ERROR", "report": "未知子命令：%s" % cmd}, 1)

    try:
        session = ct.load_session()
    except Exception as e:
        return emit({"result": "NO_AUTH", "report": str(e)}, 3)

    try:
        if cmd == "doctor":
            code, data, _s = request_with_refresh(
                session, "GET", DELIVERY_PATH, {"channel": CHANNEL})
            if code == 200:
                return emit({"result": "AUTH_READY",
                             "report": "会话与接口均可用，活动 %s 个" %
                                       len(((data.get("data") or {}).get("items") or []))})
            return emit({"result": "ERROR",
                         "report": "接口返回 HTTP %s" % code}, 1)
        return run_status(session, do_claim=(cmd == "auto"))
    except Exception as e:
        return emit({"result": "ERROR",
                     "report": "%s: %s" % (type(e).__name__, e)}, 1)


if __name__ == "__main__":
    sys.exit(main())
