#!/usr/bin/env python3
"""从 Qoder Linux 桌面端登录态离线解出 access token。

上游 qoder_signin.py 只认 Windows / macOS 的登录态位置。Linux 桌面端把会话
加密存在 ~/.config/com.qoder.app.stable/auth.v1.dat，口令在 gnome-keyring 里。

本脚本不改上游任何文件，只做一件事：解出 token 打到标准输出，由 run-qoder.sh
注入 QODER_TOKEN 交给上游。诊断信息一律走标准错误。

加密参数（实测自本机客户端 v1.37.0，与 Chromium 标准实现有一处偏差）：
  Salt   = b"saltysalt"                     <-- 9 字节，不补 16 个 0x00
  Key    = PBKDF2-HMAC-SHA1(password, salt, 1)[:16]
  Cipher = AES-128-CBC, IV = b"\x20" * 16
  Data   = 前 3 字节是 "v11" 版本前缀，解密时跳过
  Padding= PKCS#7
  Password = keyring 中 "Chromium Safe Storage / application=Qoder App" 条目

注意 Salt：Chromium 官方的 Linux 实现是 b"saltysalt" + b"\x00"*16（25 字节），
但 Qoder 桌面端实际按 9 字节盐派生了密钥。这一点是靠穷举比对确认的——
用 25 字节盐对所有口令都解不出，改用 9 字节盐立刻得到合法 JSON。
两套盐都保留在 SALT_CANDIDATES 里，避免客户端升级后又踩同一个坑。

keyring 口令的取法（两条路，按可靠性排序）：
  1) Secret Service（D-Bus）：SearchItems 按 xdg:schema 匹配，再在同一条连接内
     OpenSession + GetSecrets。分次调用会各自建连接，报"会话不存在"。
  2) keyring 明文文件：~/.local/share/keyrings/login.keyring 未加密时可直接解析。
     当前机器 keyring 无锁、无 daemon，走这条路。

安全约定：口令、派生密钥、token 均不落盘、不打印；只在标准输出吐 token。
"""

import base64
import hashlib
import json
import os
import sys
import time

SCHEMA = "chrome_libsecret_os_crypt_password_v2"
# 优先级：Qoder 桌面端自己写的条目排最前。实测本机写的是
# "Chromium Safe Storage / application=Qoder App"，而不是 "Qoder Safe Storage"，
# 两者都在 keyring 里且口令不同，选错了就解不开。
APPS = ("Qoder App", "Qoder CN App", "Qoder")
IV = b"\x20" * 16
# 9 字节在前（实测生效），25 字节是 Chromium 官方写法，留作兼容。
SALT_CANDIDATES = (b"saltysalt", b"saltysalt" + b"\x00" * 16)
SALT = SALT_CANDIDATES[0]
KEYRING_FILE = os.path.expanduser("~/.local/share/keyrings/login.keyring")


def log(msg):
    sys.stderr.write("[qoder_token] %s\n" % msg)


def auth_file():
    return os.environ.get("QODER_LINUX_AUTH_FILE") or os.path.expanduser(
        "~/.config/com.qoder.app.stable/auth.v1.dat")


# ---------------------------------------------------------------- 解密

def derive_key(password, salt=SALT):
    return hashlib.pbkdf2_hmac("sha1", password, salt, 1, 16)


def decrypt(blob, password, salt=SALT):
    """返回明文字节；解不出（密钥不对）时返回 None。

    AES-CBC 没有认证标签，密钥错时通常仍能解出字节，但 UTF-8 解码会失败，
    所以调用方必须用"能否解成合法 JSON/UTF-8"来判定成败。
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = derive_key(password, salt)
    try:
        dec = Cipher(algorithms.AES(key), modes.CBC(IV)).decryptor()
        plain = dec.update(blob[3:]) + dec.finalize()
    except Exception:
        return None

    # 不做「key = b"\x00"*16」之类的假擦除：bytes 不可变，重新绑定局部变量
    # 并不会清掉原来那块内存，反而给人虚假的安全感。密钥只活在本次调用栈上，
    # 函数返回即回收；真正的安全边界是「不落盘、不打印」，由调用方保证。

    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and len(plain) >= pad:
        # 只剥掉合法的 PKCS#7 填充；不合法就原样返回，让 JSON 解析去否决它。
        if plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
    return plain


def try_decode(blob, password):
    """解出并校验：必须是 UTF-8，且是 JSON 对象。

    两种盐都试一遍——客户端版本不同可能换用另一套派生参数。
    """
    for salt in SALT_CANDIDATES:
        plain = decrypt(blob, password, salt)
        if plain is None:
            continue
        try:
            obj = json.loads(plain.decode("utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def find_access_token(obj):
    """从登录态 JSON 里挑出 access token 与到期时间。

    结构随版本变化，这里按常见键名做广度搜索，只在顶层两层找。
    """
    def pick(d, names):
        for n in names:
            v = d.get(n)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    token = pick(obj, ("accessToken", "access_token", "token", "idToken"))
    expires = pick(obj, ("expiresAt", "expires_at", "expireTime", "expiry"))
    if token:
        return token, expires

    for key in ("auth", "session", "data", "user"):
        sub = obj.get(key)
        if isinstance(sub, dict):
            token = pick(sub, ("accessToken", "access_token", "token"))
            if token:
                return token, pick(sub, ("expiresAt", "expires_at", "expireTime"))
    return None, None


# ---------------------------------------------------------------- keyring

def keyring_passwords():
    """产出 (来源说明, 口令字节)。按 Qoder 优先、其他应用兜底排序。"""
    seen = set()
    for source, secret in _keyring_file_entries():
        if secret in seen:
            continue
        seen.add(secret)
        yield source, secret


def _keyring_file_entries():
    """解析 gnome-keyring 的明文 keyring 文件。

    格式：[<id>] 段头，`secret=` 明文字段；属性在 [<id>:attributeN] 段里，
    以 name=/value= 成对出现。
    """
    path = KEYRING_FILE
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return

    items = {}       # id -> {"secret": str, "attrs": {name: value}}
    current = None   # 当前条目 id（不含冒号的段头）
    attr = {}        # 当前 attribute 块里已读到的字段
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            head = line[1:-1]
            if ":" in head:
                # 形如 [7:attribute0]：归属到条目 7，但仍沿用 7 作为当前条目
                current = head.split(":", 1)[0]
            else:
                current = head
            # 属性段可能先于主段出现，这里也要建档，否则整条属性会被跳过
            items.setdefault(current, {"secret": None, "attrs": {}})
            attr = {}
            continue
        if current is None or current not in items or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k == "secret":
            items[current]["secret"] = v
        elif k == "value":
            if "name" in attr:
                items[current]["attrs"][attr["name"]] = v
            attr = {}
        elif k == "name":
            # 条目属性块的真实结构是 name= / type= / value= 三行一组。
            # 早先的实现把 type 也塞进 attr，并让 name 被后续字段覆盖，
            # 结果 application 永远取不到，误判成"keyring 里没有 Qoder 条目"。
            attr["name"] = v
        elif k == "type":
            continue
        else:
            attr[k] = v

    def rank(attrs):
        if attrs.get("xdg:schema") != SCHEMA:
            return 9
        app = attrs.get("application", "")
        if app in APPS:
            return APPS.index(app)
        return 5   # 其他 Chromium 应用：可以试，但排后

    ordered = sorted(items.values(), key=lambda it: rank(it["attrs"]))
    for it in ordered:
        raw = it.get("secret")
        if not raw:
            continue
        attrs = it["attrs"]
        label = "%s/%s" % (attrs.get("application", "?"),
                           attrs.get("xdg:schema", "?"))
        try:
            secret = base64.b64decode(raw)
        except Exception:
            continue
        if secret:
            yield label, secret


def secret_service_passwords(timeout=8.0):
    """通过 Secret Service 读口令（D-Bus，单连接内完成会话与读取）。

    没有 keyring daemon 时这条路会失败，因此包了超时并降级到读文件。
    """
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    try:
        from dbus_next import Variant
        from dbus_next.aio import MessageBus
    except Exception:
        return
    import asyncio

    async def run():
        # 不依赖 introspect 生成的代理方法：不同版本 dbus-next 对
        # a{o(oayays)} 这类嵌套结构的自动解包不一致，直接用 call() 收发原始消息，
        # 由我们自己按签名取值，稳定得多。
        from dbus_next import Message
        from dbus_next import MessageType

        bus = await MessageBus().connect()

        async def invoke(path, member, signature=None, body=None,
                         iface="org.freedesktop.Secret.Service"):
            msg = Message(destination="org.freedesktop.secrets", path=path,
                          interface=iface, member=member, signature=signature or "",
                          body=body or [])
            reply = await bus.call(msg)
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError("%s: %s" % (reply.error_name, reply.body))
            return reply.body

        # OpenSession 必须在与本连接相同的会话里完成：session 路径与连接绑定，
        # 跨连接使用会报"会话不存在"。
        _, session = await invoke(
            "/org/freedesktop/secrets", "OpenSession", "sv",
            ["plain", Variant("s", "")])

        unlocked, locked = await invoke(
            "/org/freedesktop/secrets", "SearchItems", "a{ss}",
            [{"xdg:schema": SCHEMA}])

        paths = list(unlocked)
        if locked:
            opened, _prompt = await invoke(
                "/org/freedesktop/secrets", "Unlock", "ao", [list(locked)])
            paths.extend(opened)
        if not paths:
            return []

        # a{o(oayays)}: 路径 -> (session, 参数, 值字节, 内容类型)
        # dbus-next 把它解成 [ (path, [session, params, bytes, ctype]), ... ] 这样的
        # 列表（不是 dict），所以按列表遍历，逐项取出字节。
        secrets = await invoke(
            "/org/freedesktop/secrets", "GetSecrets", "aoo", [paths, session])

        out = []

        def grab(node):
            """递归收集所有 bytes 叶子。

            dbus-next 对 a{o(oayays)} 的解包形式随版本变化：实测是
            [ {path: [session, params, b"..", ctype]}, ... ]，即列表套字典。
            注意结构里除了 value（口令）还有 params（IV）也是字节，所以这里
            会把 IV 一并收进候选；它们解不开 auth.v1.dat，会被 try_decode 的
            JSON 校验否决，只是多试几次，不影响正确性。
            """
            if isinstance(node, (bytes, bytearray)):
                if node:
                    out.append(bytes(node))
            elif isinstance(node, Variant):
                grab(node.value)
            elif isinstance(node, dict):
                for v in node.values():
                    grab(v)
            elif isinstance(node, (tuple, list)):
                for v in node:
                    grab(v)

        grab(secrets)
        return out

    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(asyncio.wait_for(run(), timeout))
        finally:
            loop.close()
        for s in result or []:
            yield "secret-service", s
    except Exception as exc:
        log("Secret Service 不可用：%s" % type(exc).__name__)


# ---------------------------------------------------------------- 主流程

def main():
    path = auth_file()
    if not os.path.isfile(path):
        log("未找到 Qoder 登录态：%s" % path)
        return 2

    with open(path, "rb") as f:
        blob = f.read()
    if len(blob) < 4 or not blob.startswith(b"v11"):
        log("登录态格式不是 v11（前 3 字节 %r）" % blob[:3])
        return 2
    log("登录态 %s（%d 字节）" % (path, len(blob)))

    tried = 0
    for source, password in list(secret_service_passwords()) + list(keyring_passwords()):
        tried += 1
        obj = try_decode(blob, password)
        if obj is None:
            continue
        token, expires = find_access_token(obj)
        if not token:
            log("口令来自 %s，解开但未找到 access token，顶层键：%s"
                % (source, list(obj.keys())))
            continue
        log("口令来源：%s" % source)
        if expires:
            log("登录态到期：%s" % expires)
        else:
            log("登录态未携带到期时间（上游会按不可校验处理）")
        sys.stdout.write(token)
        sys.stdout.write("\n")
        return 0

    if not tried:
        log("keyring 中没有 %s 条目，也没有可用的 Secret Service"
            % SCHEMA)
    else:
        log("试过 %d 个口令均无法解开登录态" % tried)
    log("请在 Qoder 客户端登录一次，让它在 keyring 里写入口令后重试")
    return 3


if __name__ == "__main__":
    sys.exit(main())
