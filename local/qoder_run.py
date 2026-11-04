#!/usr/bin/env python3
"""Qoder Windows 入口：注入登录态目录、国际版端点与出口策略后转调上游。

三处与上游默认值的差异（都是实测踩出来的）：

  1. 登录态目录：上游 AUTH_DIR_GLOB 写死 `com.qodercn.app*`（国区包名）。
     国际版客户端装出来是 `%APPDATA%\\com.qoder.app.stable`，glob 匹配不到，
     上游会直接报 NO_AUTH。这里用 QODER_AUTH_DIR 显式指定。

  2. 端点：国区 `openapi.qoder.com.cn` 对国际版账号返回 401。
     国际版必须用 QODER_BASE_URL=https://openapi.qoder.sh。

  3. 出口：部分网络环境下系统 DNS 对 openapi.qoder.sh 返回 NXDOMAIN，
     但直连 IP 是通的。策略是双保险——
        先探本地代理 127.0.0.1:7890，通就设 HTTPS_PROXY；
        不通则在进程内 monkeypatch socket.getaddrinfo，改用 qoder_dns_pin.json
        里钉住的 IP。两者都不改系统 hosts / DNS，退出即失效。

不修改上游任何文件，只在进程内改环境变量与 socket 行为，再用 runpy 转调。

用法：
  qoder_run.py doctor | status | auto | silent [--dry-run] [--once]
"""

import io
import json
import os
import runpy
import socket
import sys

from net_win import ensure_direct

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(LOCAL_DIR)
UPSTREAM = os.path.join(REPO_DIR, "qoder_signin.py")
PIN_FILE = os.path.join(LOCAL_DIR, "qoder_dns_pin.json")
LOG_DIR = os.path.join(LOCAL_DIR, "logs")

ACTIONS = ("auto", "silent", "status", "doctor")
FLAGS = ("--dry-run", "--once")

# 国际版端点；国区是 https://openapi.qoder.com.cn
BASE_URL = "https://openapi.qoder.sh"
# 国际版客户端的登录态目录名（相对 %APPDATA%）
AUTH_DIR_NAME = "com.qoder.app.stable"

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7890


def log(msg):
    sys.stderr.write("[qoder] %s\n" % msg)


def _appdata():
    base = os.environ.get("APPDATA")
    if base:
        return base
    return os.path.join(os.environ.get("USERPROFILE")
                        or os.path.expanduser("~"), "AppData", "Roaming")


# ---------------------------------------------------------------- 出口

def proxy_alive(timeout=0.4):
    """只做 TCP 连通性探测，不握手、不发请求，避免代理不可用时拖慢启动。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((PROXY_HOST, PROXY_PORT))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def load_pin():
    """读 DNS 兜底表；文件缺失或格式不对一律当作「没有兜底」。"""
    try:
        with open(PIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, []
    if not isinstance(data, dict):
        return None, []
    host = data.get("host")
    ips = data.get("ips")
    if not isinstance(host, str) or not host.strip():
        return None, []
    if not isinstance(ips, list):
        return host.strip(), []
    clean = [str(ip).strip() for ip in ips if str(ip).strip()]
    return host.strip(), clean


def pin_dns(host, ips):
    """把 host 的解析结果固定成给定 IP；其他域名原样交给系统解析。

    urllib / http.client 都会走 getaddrinfo，patch 这一层对它们透明。
    返回原函数，供调用方在需要时还原（本进程内其实无需还原）。
    """
    original = socket.getaddrinfo
    ipv4 = [ip for ip in ips if ":" not in ip]
    if not ipv4:
        return original

    def patched(name, port, family=0, type=0, proto=0, flags=0):
        if name == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", (ip, port)) for ip in ipv4]
        return original(name, port, family, type, proto, flags)

    socket.getaddrinfo = patched
    return original


def setup_egress():
    """返回一句人类可读的出口说明，仅用于 stderr。"""
    no_proxy = (os.environ.get("NO_PROXY") or "").lower()
    if "openapi.qoder.sh" in [x.strip() for x in no_proxy.split(",")]:
        return "直连（系统代理不可用）"
    if proxy_alive():
        proxy = "http://%s:%d" % (PROXY_HOST, PROXY_PORT)
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["HTTP_PROXY"] = proxy
        # 上游构造 urllib 请求时未显式禁用代理，getproxies 会读这两个变量
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
        return "本地代理 %s" % proxy

    host, ips = load_pin()
    if not host:
        return "系统 DNS（无兜底表）"
    if not ips:
        return "系统 DNS（%s 的兜底表为空）" % host
    pin_dns(host, ips)
    return "DNS 钉住 %s → %d 个 IP" % (host, len(ips))


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
        with open(os.path.join(LOG_DIR, "qoder_signin.log"), "a",
                  encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 主流程

def _fail(report, rc):
    sys.stdout.write(json.dumps({"result": "ERROR", "report": report},
                                ensure_ascii=False) + "\n")
    return rc


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    plain = [a for a in args if a not in FLAGS]
    action = plain[0] if plain else "auto"
    if action not in ACTIONS:
        return _fail("未知子命令：%s（可用：%s）"
                     % (action, " / ".join(ACTIONS)), 1)

    if not os.path.isfile(UPSTREAM):
        return _fail("未找到上游脚本：%s" % UPSTREAM, 1)

    auth_dir = os.environ.get("QODER_AUTH_DIR")
    if not auth_dir:
        auth_dir = os.path.join(_appdata(), AUTH_DIR_NAME)
        os.environ["QODER_AUTH_DIR"] = auth_dir
    log("登录态目录：%s（存在=%s）" % (auth_dir, os.path.isdir(auth_dir)))

    if not os.environ.get("QODER_BASE_URL"):
        os.environ["QODER_BASE_URL"] = BASE_URL
    log("端点：%s" % os.environ["QODER_BASE_URL"])

    # doctor 是离线检查，不需要连通性，避免探测本身误导排查
    if action != "doctor":
        if ensure_direct("openapi.qoder.sh"):
            log("系统代理不可用，本次直连")
        log("出口：%s" % setup_egress())

    sys.argv = [UPSTREAM] + args
    try:
        runpy.run_path(UPSTREAM, run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else code
    except Exception as exc:
        return _fail("上游脚本运行异常（%s）" % type(exc).__name__, 2)


if __name__ == "__main__":
    _buf = _ensure_streams()
    try:
        rc = main()
    finally:
        _dump(_buf)
    sys.exit(rc)
