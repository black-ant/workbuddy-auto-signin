#!/usr/bin/env python3
"""Windows 系统代理探测：端口通不等于链路通。

计划任务里经常出现「代理进程在、端口也监听，但 CONNECT/TLS 已经坏了」的情况。
此时 urllib 仍会读到注册表里的系统代理，签到就会整轮 SSL EOF。这里做两件事：

  1. 用 CONNECT + TLS 实测目标域名，而不是只探测端口；
  2. 实测失败时把目标域名写进 NO_PROXY，让本次进程直连，不影响其他应用。

代理正常时不做任何改动，保留走代理的部署方式。
"""

import http.client
import os

INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _system_proxy():
    """返回注册表里的代理 (host, port)；未启用或格式异常时返回 None。"""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            if not enabled:
                return None
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except OSError:
        return None

    if not server:
        return None
    # 形如 "http=127.0.0.1:7890;https=127.0.0.1:7890" 或 "127.0.0.1:7890"
    for part in str(server).split(";"):
        if "=" in part:
            scheme, value = part.split("=", 1)
            if scheme.strip().lower() in ("https", "http"):
                server = value
                break
    server = str(server).replace("http://", "").replace("https://", "")
    server = server.strip().strip("/")
    if ":" not in server:
        return None
    host, port = server.rsplit(":", 1)
    try:
        return host, int(port)
    except ValueError:
        return None


def proxy_works(host, timeout=3.0):
    """CONNECT 到 host:443 并完成 TLS 握手；没有代理时视为不可用。"""
    proxy = _system_proxy()
    if not proxy:
        return False
    conn = None
    try:
        conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout)
        conn.set_tunnel(host, 443)
        conn.request("HEAD", "/")
        conn.getresponse().read(0)
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def ensure_direct(host):
    """代理不可用时把 host 加进 NO_PROXY，返回 True 表示已切直连。"""
    if not _system_proxy():
        return False
    if proxy_works(host):
        return False

    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    items = [x.strip() for x in current.split(",") if x.strip()]
    if host not in items:
        items.append(host)
    os.environ["NO_PROXY"] = ",".join(items)
    return True


def system_proxy_label():
    """诊断用：返回 "host:port" 或空串，不打印凭据。"""
    proxy = _system_proxy()
    return "%s:%d" % proxy if proxy else ""
