#!/usr/bin/env python3
"""CodeArts Agent（码道）Windows 入口：解加密会话 + 过期续期 + 回写。

与 Linux 侧的差别只在「凭据怎么存」这一层，业务流程完全相同：

  Linux   : ~/.config/codearts-agent/... + gnome-keyring 口令 + AES-CBC(v11)
  Windows : %APPDATA%\\codearts-agent\\...  + DPAPI 容器密钥 + AES-GCM(v10)

所以本文件只重写 load_session / save_session，refresh_session 直接复用
`codearts_token.py` 里的 DPoP + STS 实现（那段与平台无关）。

接入上游的方式：把一个 stub 模块塞进 sys.modules["codearts_token"]，再加载
`codearts_signin.py`。这样上游的 `import codearts_token as ct` 拿到的是本文件的
Windows 实现，而上游文件一个字节都不用改。

  ⚠ 不要直接 `import codearts_token` 后再覆盖属性——stub 必须先就位，
    否则 codearts_signin 会绑定到 Linux 实现，读不到 Windows 的 state.vscdb。

两条硬约束（违反会把用户登录态写坏）：

  1. IDE 正在运行时只读不写。VSCode 会把内存里的旧会话刷回磁盘，覆盖我们的
     写入。判断依据 tasklist 里有没有 codearts-agent.exe。
  2. 续期必须回写。refresh_token 是轮换的，只续不写，下次就失效。

用法：
  codearts_run.py doctor | status | auto
"""

import base64
import datetime
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(LOCAL_DIR, "logs")

ACTIONS = ("auto", "status", "doctor")
IDE_PROC = "codearts-agent.exe"

SECRET_KEY = ('secret://{"extensionId":"huaweicloud.authentication",'
              '"key":"HuaweiCloudSession"}')
# IDE 自己写的明文缓存，回写时同步更新，便于插件/人在界面上看到最新有效期
SNAP_KEY = "huaweicloud.codearts-snap"

# 会话提前 120 秒视为过期，留出请求往返的余量
EXPIRY_SKEW = 120


def log(msg):
    sys.stderr.write("[codearts] %s\n" % msg)


def _appdata():
    base = os.environ.get("APPDATA")
    if base:
        return base
    return os.path.join(os.environ.get("USERPROFILE")
                        or os.path.expanduser("~"), "AppData", "Roaming")


def _agent_dir():
    return os.path.join(_appdata(), "codearts-agent")


def state_db_path():
    return os.environ.get("CODEARTS_STATE_DB") or os.path.join(
        _agent_dir(), "User", "globalStorage", "state.vscdb")


def local_state_path():
    return os.environ.get("CODEARTS_LOCAL_STATE") or os.path.join(
        _agent_dir(), "Local State")


# ---------------------------------------------------------------- DPAPI / AES-GCM

def _dpapi_unprotect(data):
    """用当前 Windows 用户的 DPAPI 解开 Local State 里的容器密钥。

    Chromium 系客户端的 os_crypt.encrypted_key 就是 `DPAPI` + CryptProtectData
    的输出，所以必须先剥掉这 5 字节前缀。
    """
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 仅 Windows 可用（当前 %s）" % sys.platform)
    import ctypes
    import ctypes.wintypes as wt

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    src = DataBlob(len(data), ctypes.create_string_buffer(data, len(data)))
    dst = DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst))
    if not ok:
        # 不把 ctypes.WinError 的完整文本带出去，避免混入路径等信息
        raise RuntimeError("DPAPI 解不开密钥，需在与 IDE 相同的 Windows 用户下运行")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _aesgcm(key):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RuntimeError("缺少 cryptography，无法解密 CodeArts 会话") from None
    return AESGCM(key)


def container_key():
    """Local State → os_crypt.encrypted_key → 32 字节 AES 密钥。"""
    path = local_state_path()
    if not os.path.isfile(path):
        raise RuntimeError("未找到 Local State：%s" % path)
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    crypt = state.get("os_crypt") if isinstance(state, dict) else None
    if not isinstance(crypt, dict):
        raise RuntimeError("Local State 缺少 os_crypt 配置")
    raw = base64.b64decode(crypt.get("encrypted_key") or "")
    if raw[:5] != b"DPAPI":
        raise RuntimeError("os_crypt.encrypted_key 不是 DPAPI 格式，客户端可能换实现")
    return _dpapi_unprotect(raw[5:])


def decrypt_session(blob, key):
    """v10 = 3 字节前缀 + 12 字节 nonce + 密文 + 16 字节 tag。"""
    if blob[:3] != b"v10":
        raise RuntimeError("会话格式不是 v10（前 3 字节 %r）" % blob[:3])
    if len(blob) < 3 + 12 + 16:
        raise RuntimeError("会话密文过短，客户端可能正在改写")
    return _aesgcm(key).decrypt(blob[3:15], blob[15:], None)


def encrypt_session(session, key):
    nonce = os.urandom(12)
    plain = json.dumps(session, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
    return b"v10" + nonce + _aesgcm(key).encrypt(nonce, plain, None)


# ---------------------------------------------------------------- state.vscdb

def _uri(path):
    # sqlite 的 URI 只认正斜杠；Windows 路径原样传会解析失败
    return "file:%s?mode=ro" % path.replace("\\", "/")


def _copy_db():
    """复制副本再读：运行中的 IDE 可能持有写锁，直连会 database is locked。"""
    src = state_db_path()
    tmp_dir = tempfile.mkdtemp(prefix="codearts-")
    dst = os.path.join(tmp_dir, "state.vscdb")
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        extra = src + suffix
        if os.path.isfile(extra):
            shutil.copy2(extra, dst + suffix)
    return dst


def read_value(key):
    """读 ItemTable 里某个键的字符串值；不存在返回 None。"""
    if not os.path.isfile(state_db_path()):
        raise RuntimeError("未找到 CodeArts Agent 配置：%s" % state_db_path())
    last = None
    for path in (state_db_path(), _copy_db()):
        con = None
        try:
            con = sqlite3.connect(_uri(path), uri=True, timeout=10)
            row = con.execute("select value from ItemTable where key=?",
                              (key,)).fetchone()
            return row[0] if row else None
        except sqlite3.Error as exc:
            last = exc
            log("直读失败，改用副本（%s）" % type(exc).__name__)
        finally:
            if con is not None:
                con.close()
    raise RuntimeError("读取客户端存储失败（%s）" % type(last).__name__)


def write_value(key, value):
    con = sqlite3.connect(state_db_path(), timeout=15)
    try:
        con.execute("update ItemTable set value=? where key=?", (value, key))
        con.commit()
    finally:
        con.close()


def _read_secret_blob():
    row = read_value(SECRET_KEY)
    if not row:
        raise RuntimeError("未找到华为云登录会话，请先在 CodeArts Agent 里登录")
    try:
        return bytes(json.loads(row)["data"])
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("登录会话的存储结构变了（不是 Buffer 数组）") from None


# ---------------------------------------------------------------- IDE 运行检测

def ide_running():
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq " + IDE_PROC, "/NH"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        # 查不到就当没在跑，但下面写回前还有一次备份与自校验兜着
        return False
    return IDE_PROC.encode("ascii", "ignore") in (out.stdout or b"").lower()


# ---------------------------------------------------------------- 会话

def _parse_expiry(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    return None


def session_expired(session):
    """解析不出到期时间时按未过期处理，交给服务端返回 APIG.0602 再续。"""
    dt = _parse_expiry(session.get("expires_at") or session.get("expiresAt"))
    if dt is None:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    return dt - datetime.timedelta(seconds=EXPIRY_SKEW) <= now


_REAL_TOKEN_MODULE = None


def _real_token_module():
    """加载真实的 codearts_token.py，只取其中的 refresh_session。

    不能用 `import codearts_token`——那时 sys.modules 里已经是本文件的 stub。
    """
    global _REAL_TOKEN_MODULE
    if _REAL_TOKEN_MODULE is None:
        path = os.path.join(LOCAL_DIR, "codearts_token.py")
        if not os.path.isfile(path):
            raise RuntimeError("未找到 codearts_token.py：%s" % path)
        spec = importlib.util.spec_from_file_location(
            "codearts_token_impl", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _REAL_TOKEN_MODULE = module
    return _REAL_TOKEN_MODULE


def refresh_session(session):
    """DPoP + refresh_token 换新 AK/SK/securitytoken（复用 Linux 侧实现）。"""
    return _real_token_module().refresh_session(session)


def _backup():
    src = state_db_path()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = "%s.bak-%s" % (src, stamp)
    try:
        shutil.copy2(src, dst)
        return dst
    except OSError as exc:
        log("备份失败（%s），继续尝试写回" % type(exc).__name__)
        return None


def _sync_snap(session):
    """把新凭据同步到 IDE 的明文缓存项，只更新已存在的字段，不新建结构。"""
    try:
        row = read_value(SNAP_KEY)
    except Exception as exc:
        log("读取明文缓存失败（%s），跳过同步" % type(exc).__name__)
        return
    if not row:
        return
    try:
        data = json.loads(row)
    except ValueError:
        return
    origin = ((data.get("userInfoKey") or {}).get("originData")
              if isinstance(data, dict) else None)
    if not isinstance(origin, dict):
        return

    mapping = (("accessKey", "accessKey"), ("secretKey", "secretKey"),
               ("securitytoken", "securityToken"), ("expires_at", "expiresAt"))
    changed = False
    for src_key, dst_key in mapping:
        if src_key in session and dst_key in origin:
            value = session[src_key]
            if origin[dst_key] != value:
                origin[dst_key] = value
                changed = True
    if not changed:
        return
    try:
        write_value(SNAP_KEY, json.dumps(data, ensure_ascii=False))
    except sqlite3.Error as exc:
        log("明文缓存更新失败（%s），不影响加密会话" % type(exc).__name__)


def save_session(session):
    """加密写回 secret:// 项 + 同步明文缓存，写完回读校验。"""
    if ide_running():
        # VSCode 会把内存里的旧会话刷回磁盘，此时写入等于主动破坏登录态
        log("IDE 正在运行，只读不写")
        return False

    key = container_key()
    try:
        _decrypt_session_check(_read_secret_blob(), key)
    except Exception as exc:
        log("无法解开当前会话，跳过写回（%s）" % type(exc).__name__)
        return False

    _backup()
    blob = encrypt_session(session, key)
    payload = json.dumps({"type": "Buffer", "data": list(blob)})
    try:
        write_value(SECRET_KEY, payload)
    except sqlite3.Error as exc:
        log("写回失败（%s）" % type(exc).__name__)
        return False

    _sync_snap(session)

    try:
        back = bytes(json.loads(read_value(SECRET_KEY))["data"])
        _decrypt_session_check(back, key)
    except Exception as exc:
        log("写回后自校验失败（%s），登录态可能已损坏" % type(exc).__name__)
        return False
    return True


def _decrypt_session_check(blob, key):
    return json.loads(decrypt_session(blob, key).decode("utf-8"))


def load_session():
    """取会话；过期且 IDE 未运行时先续期再返回。"""
    key = container_key()
    blob = _read_secret_blob()
    try:
        session = _decrypt_session_check(blob, key)
    except Exception as exc:
        raise RuntimeError("会话无法解密（%s）" % type(exc).__name__) from None
    if not isinstance(session, dict) or not session.get("accessKey"):
        raise RuntimeError("解出的会话缺少 AK/SK，请在 IDE 里重新登录")

    if not session_expired(session):
        return session

    if ide_running():
        # IDE 会自己续期并落盘，我们插一脚反而冲突
        log("会话已过期，但 IDE 正在运行：交给 IDE 续期")
        return session

    log("会话已过期，执行续期")
    try:
        new_session, _refresh_token = refresh_session(session)
    except Exception as exc:
        raise RuntimeError("会话续期失败（%s）" % type(exc).__name__) from None

    # refresh_token 是轮换的：写不回去，下次就彻底失效，所以必须尝试
    if not save_session(new_session):
        log("续期结果未能写回，本次仅内存可用；下次运行可能需要重新登录")
    return new_session


# ---------------------------------------------------------------- 接入上游

def _install_token_stub():
    stub = types.ModuleType("codearts_token")
    stub.load_session = load_session
    stub.refresh_session = refresh_session
    stub.save_session = save_session
    stub.log = log
    sys.modules["codearts_token"] = stub
    return stub


def _run_signin(argv):
    _install_token_stub()
    path = os.path.join(LOCAL_DIR, "codearts_signin.py")
    if not os.path.isfile(path):
        raise RuntimeError("未找到 codearts_signin.py：%s" % path)
    spec = importlib.util.spec_from_file_location("codearts_signin_win", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["codearts_signin_win"] = module
    spec.loader.exec_module(module)

    old_argv = sys.argv
    sys.argv = [path] + list(argv)
    try:
        return module.main()
    finally:
        sys.argv = old_argv


# ---------------------------------------------------------------- 输出兜底

def _ensure_streams():
    buf = None
    if sys.stdout is None:
        buf = io.StringIO()
        sys.stdout = buf
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    return buf


def _dump(buf):
    if buf is None:
        return
    text = buf.getvalue()
    if not text:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "codearts_signin.log"), "a",
                  encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 主流程

def _emit(obj, rc=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return rc


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "auto"
    if action not in ACTIONS:
        return _emit({"result": "ERROR",
                      "report": "未知子命令：%s（可用：%s）"
                                % (action, " / ".join(ACTIONS))}, 1)

    try:
        return _run_signin([action])
    except Exception as exc:
        # 凭据类问题统一归到 NO_AUTH，退出码与上游一致，便于计划任务判定
        report = str(exc)
        result = "NO_AUTH" if ("登录" in report or "会话" in report
                               or "DPAPI" in report or "Local State" in report
                               or "解密" in report) else "ERROR"
        return _emit({"result": result, "report": report},
                     3 if result == "NO_AUTH" else 1)


if __name__ == "__main__":
    _buf = _ensure_streams()
    try:
        rc = main()
    finally:
        _dump(_buf)
    sys.exit(rc)
