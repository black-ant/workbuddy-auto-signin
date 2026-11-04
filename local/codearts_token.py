#!/usr/bin/env python3
"""CodeArts Agent（码道）会话解密与续期。

职责边界：本文件**只负责凭据**，不发起任何业务请求。签到逻辑在
`codearts_signin.py`，它 import 本模块拿 session。

能力：
  1. 从 state.vscdb 解出华为云会话（AK/SK/securitytoken/refresh_token）✅
  2. 用 DPoP + refresh_token 续期会话并写回客户端存储 ✅

关于业务接口的路线（2026-11-04 读 IDE 插件源码后定稿，勿再按旧结论改）：
  业务 API 走 `snap-access` 网关，鉴权是 **SDK-HMAC-SHA256 的 AK/SK 签名 +
  X-Security-Token**，不是 `X-Auth-Token`。插件里 `Pr()` 在商用版打日志
  "Hc version, token is transferred by the toolkit." 后**返回空串**，
  JS 层不提供 token；真正签名在 `Mt.sendRequest` 的 credential client 里。
  所以不需要账号密码，也不需要浏览器 Cookie。

  旧版本此处曾断言「必须 IAM X-Auth-Token、这条路没打通」，那是误判，
  原因有两个实现 bug：把签名用的 canonical URI 当真实 URL（多末尾 "/" 和
  /snap-manager 前缀 → 404），以及漏了 `Agent-Type: PromptCenter`。
  详见 docs/codearts-signin.md。

用法：
  codearts_token.py session    查看会话有效期与账号（不打印任何凭据值）
  codearts_token.py refresh    续期会话并写回客户端存储
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

STATE_DB = os.path.expanduser(
    "~/.config/codearts-agent/User/globalStorage/state.vscdb")
SECRET_KEY = ('secret://{"extensionId":"huaweicloud.authentication",'
              '"key":"HuaweiCloudSession"}')

# 国内账号的业务网关（来自 huaweicloud.authentication 的 snapEngineApiHost）
HOST = "snap-access.cn-north-4.myhuaweicloud.com"
# 业务请求的 path / 签名 / 请求头都在 codearts_signin.py 里，本文件不发起业务请求。
# 注意：不要给业务 URL 加 /snap-manager 前缀（旧框架路由，会 404）。

SALT_CANDIDATES = (b"saltysalt", b"saltysalt" + b"\x00" * 16)
IV = b"\x20" * 16


def log(msg):
    sys.stderr.write("[codearts] %s\n" % msg)


# ---------------------------------------------------------------- 取凭据

def secret_service_passwords(timeout=8.0):
    """通过 Secret Service 读口令。

    必须走这条路而不是只读 keyring 文件：client 改写登录态时会更新
    keyring daemon 里的口令，但磁盘上 login.keyring 的缓存可能滞后，
    两者对不上就解不开密文。
    """
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return []
    try:
        import asyncio

        from dbus_next import Message, MessageType, Variant
        from dbus_next.aio import MessageBus
    except Exception:
        return []

    SVC = "org.freedesktop.secrets"
    ROOT = "/org/freedesktop/secrets"

    async def run():
        bus = await MessageBus().connect()

        async def call(path, iface, member, sig="", body=None):
            r = await bus.call(Message(destination=SVC, path=path,
                                       interface=iface, member=member,
                                       signature=sig, body=body or []))
            if r.message_type == MessageType.ERROR:
                raise RuntimeError(r.error_name)
            return r.body

        paths, _ = await call(ROOT, "org.freedesktop.Secret.Service",
                              "SearchItems", "a{ss}", [{}])
        labels = {}
        for p in paths:
            try:
                b = await call(p, "org.freedesktop.DBus.Properties", "Get",
                               "ss", ["org.freedesktop.Secret.Item", "Label"])
                a = await call(p, "org.freedesktop.DBus.Properties", "Get",
                               "ss", ["org.freedesktop.Secret.Item", "Attributes"])
                av = a[0].value if hasattr(a[0], "value") else a[0]
                av = {k: (v.value if hasattr(v, "value") else v)
                      for k, v in av.items()} if isinstance(av, dict) else {}
                labels[p] = (str(b[0].value if hasattr(b[0], "value") else b[0]),
                             av.get("application", "?"))
            except Exception:
                labels[p] = ("?", "?")

        _, session = await call(ROOT, "org.freedesktop.Secret.Service",
                                "OpenSession", "sv",
                                ["plain", Variant("s", "")])
        secrets = await call(ROOT, "org.freedesktop.Secret.Service",
                             "GetSecrets", "aoo", [list(paths), session])

        out = []

        def grab(node, cur=None):
            if isinstance(node, dict):
                for k, v in node.items():
                    grab(v, k)
            elif isinstance(node, Variant):
                grab(node.value, cur)
            elif isinstance(node, (tuple, list)):
                for v in node:
                    grab(v, cur)
            elif isinstance(node, (bytes, bytearray)):
                if node:
                    label, app = labels.get(cur, ("?", "?"))
                    out.append(("%s/%s" % (label, app), bytes(node)))

        grab(secrets)
        return out

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                asyncio.wait_for(run(), timeout)) or []
        finally:
            loop.close()
    except Exception as exc:
        log("Secret Service 不可用：%s" % type(exc).__name__)
        return []


def keyring_passwords():
    """读 keyring 里 application=codearts-agent 的口令，其余作兜底。"""
    path = os.path.expanduser("~/.local/share/keyrings/login.keyring")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []

    items, current, attr = {}, None, {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            head = line[1:-1]
            current = head.split(":", 1)[0] if ":" in head else head
            if head not in items and ":" not in head:
                items[head] = {"secret": None, "attrs": {}}
            attr = {}
            continue
        if current is None or current not in items or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k == "secret":
            items[current]["secret"] = v
        elif k == "name":
            attr["name"] = v
        elif k == "value":
            if "name" in attr:
                items[current]["attrs"][attr["name"]] = v
            attr = {}
        elif k == "type":
            continue

    def rank(it):
        app = it["attrs"].get("application", "")
        if app == "codearts-agent":
            return 0
        if it["attrs"].get("xdg:schema") == "chrome_libsecret_os_crypt_password_v2":
            return 1
        return 9

    out = []
    for it in sorted(items.values(), key=rank):
        if not it.get("secret"):
            continue
        try:
            secret = base64.b64decode(it["secret"])
        except Exception:
            continue
        if secret:
            out.append((it["attrs"].get("application", "?"), secret))
    return out


def decrypt_session(blob, password):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if blob[:3] != b"v11":
        return None
    for salt in SALT_CANDIDATES:
        key = hashlib.pbkdf2_hmac("sha1", password, salt, 1, 16)
        try:
            d = Cipher(algorithms.AES(key), modes.CBC(IV)).decryptor()
            plain = d.update(blob[3:]) + d.finalize()
        except Exception:
            continue
        pad = plain[-1] if plain else 0
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
        try:
            obj = json.loads(plain.decode("utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("accessKey"):
            return obj
    return None


def load_session():
    if not os.path.isfile(STATE_DB):
        raise RuntimeError("未找到 CodeArts Agent 配置：%s" % STATE_DB)
    con = sqlite3.connect("file:%s?mode=ro" % STATE_DB, uri=True)
    row = con.execute("select value from ItemTable where key=?",
                      (SECRET_KEY,)).fetchone()
    con.close()
    if not row:
        raise RuntimeError("未找到华为云登录会话，请先在 CodeArts Agent 里登录")
    raw = json.loads(row[0])
    blob = bytes(raw["data"])

    # 优先 Secret Service（最新），再回落 keyring 文件；同一条口令可能重复，
    # 用集合去重但保持顺序。
    seen = set()
    candidates = []
    for app, pw in secret_service_passwords() + keyring_passwords():
        if pw in seen:
            continue
        seen.add(pw)
        candidates.append((app, pw))

    for app, pw in candidates:
        obj = decrypt_session(blob, pw)
        if obj:
            log("会话来源：%s" % app)
            return obj
    raise RuntimeError("会话无法解密（试过 %d 个口令）" % len(candidates))


# ---------------------------------------------------------------- AK/SK 签名

def sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def canonical_query(params):
    if not params:
        return ""
    items = sorted((k, str(v)) for k, v in params.items())
    return "&".join("%s=%s" % (k, urllib.parse.quote(v, safe="-_.~"))
                    for k, v in items)


def request(method, path, params=None, body=None, session=None, timeout=20):
    """按华为云 APIG 的 SDK-HMAC-SHA256 规范签名并发起请求。

    注意这**不是** AWS SigV4：string-to-sign 只有三行，
      第1行 "SDK-HMAC-SHA256"
      第2行 X-Sdk-Date
      第3行 sha256(canonical_request)
    AWS V4 里那行 credential scope（date/region/service/ak-sk-auth）在这里
    不存在。多写这一行会得到 401 "verify ak sk signature fail"，
    症状很像 AK/SK 不对，其实是签名串多了一行。
    """
    session = session or {}
    ak = session.get("accessKey", "")
    sk = session.get("secretKey", "")
    token = session.get("securitytoken", "")
    if not ak or not sk:
        raise RuntimeError("会话缺少 AK/SK")

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    payload = "" if body is None else json.dumps(body, separators=(",", ":"))
    ctype = "application/json"

    # canonical URI：逐段转义，末尾补 "/"
    uri = "/".join(urllib.parse.quote(seg, safe="-_.~")
                   for seg in path.split("/"))
    if not uri.endswith("/"):
        uri += "/"

    query = canonical_query(params)

    # canonical headers：小写键、按字母序、值 trim
    headers = {"content-type": ctype, "host": HOST, "x-sdk-date": amz_date}
    signed = sorted(headers)
    canonical_headers = "".join("%s:%s\n" % (k, headers[k]) for k in signed)

    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = "\n".join([
        method, uri, query, canonical_headers, ";".join(signed), payload_hash])

    string_to_sign = "\n".join([
        "SDK-HMAC-SHA256", amz_date,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signature = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    auth = ("SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s"
            % (ak, ";".join(signed), signature))

    url = "https://%s%s" % (HOST, uri)
    if query:
        url += "?" + query
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", ctype)
    req.add_header("Host", HOST)
    req.add_header("X-Sdk-Date", amz_date)
    req.add_header("Authorization", auth)
    if token:
        req.add_header("X-Security-Token", token)
    if body is not None:
        req.data = payload.encode("utf-8")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, {"raw": text[:400]}


# ---------------------------------------------------------------- 会话续期

STS_HOST = "https://sts.cn-north-4.myhuaweicloud.com"
TOKEN_API = "/v1/oauth2/tokens"
CLIENT_ID = "codearts-agent"


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _dpop_proof(private_key_jwk, public_key_jwk, method, url):
    """构造 DPoP proof JWT。

    坑：JWS 的 ES256 要求签名是 raw R||S（各 32 字节、共 64 字节），
    不是常见的 DER 编码。用 DER 提交，STS 会报
    STS5.1804 "decode DPoP failed with jwk"，看起来像 JWK 格式问题，
    实际是签名格式错。
    """
    import time
    import uuid

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    d = private_key_jwk["d"]
    d_int = int.from_bytes(base64.urlsafe_b64decode(d + "=" * (-len(d) % 4)), "big")
    priv = ec.derive_private_key(d_int, ec.SECP256R1())

    header = {"alg": "ES256", "typ": "dpop+jwt", "jwk": public_key_jwk}
    payload = {"htm": method, "htu": url,
               "iat": int(time.time()), "jti": uuid.uuid4().hex}
    signing_input = "%s.%s" % (
        _b64u(json.dumps(header, separators=(",", ":")).encode()),
        _b64u(json.dumps(payload, separators=(",", ":")).encode()))
    der = priv.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input + "." + _b64u(raw)


def refresh_session(session):
    """用 refresh_token 换新的 AK/SK/securitytoken。

    返回 (新会话 dict, 新 refresh_token)。失败抛异常。
    注意 refresh_token 是轮换的，必须把新的存回去，否则下次就失效。
    """
    import urllib.parse

    ctx = session.get("loginContext") or {}
    refresh_token = session.get("refresh_token")
    if not ctx.get("dpopKeyPair") or not refresh_token:
        raise RuntimeError("登录态缺少 DPoP 密钥或 refresh_token，需重新登录")

    url = STS_HOST + TOKEN_API
    proof = _dpop_proof(
        ctx["dpopKeyPair"]["privateKeyJwk"],
        ctx["dpopKeyPair"]["publicKeyJwk"], "POST", url)

    body = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "code_verifier": ctx["pkcePair"]["codeVerifier"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("DPoP", proof)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("刷新失败 HTTP %s: %s"
                           % (e.code, e.read().decode("utf-8", "replace")[:200]))

    inner = data.get("result") or data
    cred = inner.get("credentials") or {}
    if not cred.get("access_key_id"):
        raise RuntimeError("刷新响应缺少凭据：%s" % str(data)[:200])

    new = dict(session)
    new["accessKey"] = cred["access_key_id"]
    new["secretKey"] = cred["secret_access_key"]
    new["securitytoken"] = cred["security_token"]
    new["expires_at"] = cred.get("expiration")
    new["refresh_token"] = inner.get("refresh_token") or refresh_token
    return new, new["refresh_token"]


def save_session(session):
    """把刷新后的会话写回 state.vscdb（加密后覆盖原值）。

    必须用"能解开当前密文的那个口令"回写，不能取第一个候选——
    Secret Service 的返回顺序不固定，取错口令加密出来的 blob 客户端读不了。
    做法：先用原密文反查出正确口令，再用它加密新内容。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        con = sqlite3.connect(STATE_DB, timeout=10)
        row = con.execute("select value from ItemTable where key=?",
                          (SECRET_KEY,)).fetchone()
        if not row:
            con.close()
            return False
        old_blob = bytes(json.loads(row[0])["data"])

        # 反查口令：能解出旧密文的那条才是对的
        pw = None
        for _app, candidate in secret_service_passwords() + keyring_passwords():
            if decrypt_session(old_blob, candidate):
                pw = candidate
                break
        if not pw:
            con.close()
            log("找不到能解开当前登录态的口令，跳过写回")
            return False

        plain = json.dumps(session, separators=(",", ":")).encode("utf-8")
        pad = 16 - len(plain) % 16
        plain += bytes([pad]) * pad
        key = hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1, 16)
        enc = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
        blob = b"v11" + enc.update(plain) + enc.finalize()

        con.execute("update ItemTable set value=? where key=?",
                    (json.dumps({"type": "Buffer", "data": list(blob)}),
                     SECRET_KEY))
        con.commit()
        con.close()

        # 自校验：写完必须能读回来
        check = sqlite3.connect("file:%s?mode=ro" % STATE_DB, uri=True)
        back = bytes(json.loads(check.execute(
            "select value from ItemTable where key=?",
            (SECRET_KEY,)).fetchone()[0])["data"])
        check.close()
        if not decrypt_session(back, pw):
            log("写回后自校验失败，登录态可能已损坏")
            return False
        return True
    except Exception as exc:
        log("写回登录态失败（不影响本次运行）：%s" % exc)
        return False


# ---------------------------------------------------------------- 主流程

def call_with_refresh(method, path, params=None, body=None, session=None):
    """发起请求；遇到 token 过期自动刷新一次并重试。

    返回 (http_code, data, session)。session 是可能已被刷新的新会话。
    """
    code, data = request(method, path, params, body, session)
    if code == 400 and isinstance(data, dict) and \
            data.get("error_code") in ("APIG.0602", "APIGW.0301"):
        log("token 已过期，自动续期…")
        session, _rt = refresh_session(session)
        save_session(session)
        code, data = request(method, path, params, body, session)
    return code, data, session


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "session"
    try:
        session = load_session()
    except Exception as e:
        print(json.dumps({"result": "NO_AUTH", "report": str(e)},
                         ensure_ascii=False))
        return 3

    try:
        if cmd == "session":
            exp = session.get("expires_at")
            left = None
            if exp:
                try:
                    t = datetime.datetime.strptime(
                        exp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                        tzinfo=datetime.timezone.utc)
                    left = int((t - datetime.datetime.now(
                        datetime.timezone.utc)).total_seconds())
                except Exception:
                    pass
            print(json.dumps({"result": "OK",
                              "account": (session.get("account") or {}).get("label"),
                              "expires_at": exp,
                              "seconds_left": left,
                              "can_refresh": bool(session.get("refresh_token"))},
                             ensure_ascii=False))
            return 0

        if cmd == "refresh":
            new, _rt = refresh_session(session)
            ok = save_session(new)
            print(json.dumps({"result": "REFRESHED" if ok else "REFRESHED_NOSAVE",
                              "expires_at": new.get("expires_at")},
                             ensure_ascii=False))
            return 0

        print(json.dumps({"result": "ERROR",
                          "report": "未知子命令：%s" % cmd}, ensure_ascii=False))
        return 1
    except Exception as e:
        print(json.dumps({"result": "ERROR", "report": str(e)},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
