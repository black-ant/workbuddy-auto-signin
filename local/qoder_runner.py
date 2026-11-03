#!/usr/bin/env python3
"""给上游 qoder_signin.py 补设备身份请求头，不修改上游文件。

坑：每日 100 Credits 活动要求 Cosy-MachineId / MachineToken / MachineCode /
MachineType 四个头，上游只在 Windows 生成。Linux 什么都不发，活动就不下发。

本脚本导入上游后替换其 risk_headers()，从客户端缓存的 machine_token.json
补齐这四个头。Cosy-Version 非必需，有就带。

用法：由 run-qoder.sh 调用，参数原样透传给上游。
"""

import json
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

import qoder_signin  # noqa: E402  上游模块


def machine_token_path():
    return os.environ.get("QODER_MACHINE_TOKEN") or os.path.expanduser(
        "~/.config/Qoder/SharedClientCache/cache/machine_token.json")


def load_machine_identity():
    """读设备身份缓存。字段：id / token / code / type / os / hostname / version。"""
    path = machine_token_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def patched_risk_headers(auth_dir=None):
    """上游 risk_headers 的替代实现：保留其已有结果，再补齐设备身份四头。"""
    try:
        headers = dict(_orig_risk_headers(auth_dir))
    except Exception:
        headers = {}

    ident = load_machine_identity()
    if not ident:
        return headers

    # 四个必需头。上游在 Linux 上一个都不会生成。
    if ident.get("token") and not headers.get("Cosy-MachineToken"):
        headers["Cosy-MachineToken"] = str(ident["token"])
    if ident.get("code") and not headers.get("Cosy-MachineCode"):
        headers["Cosy-MachineCode"] = str(ident["code"])
    if ident.get("type") and not headers.get("Cosy-MachineType"):
        headers["Cosy-MachineType"] = str(ident["type"])
    if ident.get("id") and not headers.get("Cosy-MachineId"):
        headers["Cosy-MachineId"] = str(ident["id"])

    # 客户端缓存里还有 os / hostname，上游在 Linux 分支不会生成这两个头；
    # 真实客户端会带，补齐能让设备指纹与登录时一致，少挨风控。
    if ident.get("os") and not headers.get("Cosy-MachineOS"):
        headers["Cosy-MachineOS"] = str(ident["os"])
    if ident.get("hostname") and not headers.get("Cosy-MachineHostname"):
        headers["Cosy-MachineHostname"] = str(ident["hostname"])

    # 非必需，但真实客户端会带
    if ident.get("version") and not headers.get("Cosy-Version"):
        headers["Cosy-Version"] = str(ident["version"])

    return headers


_orig_risk_headers = qoder_signin.risk_headers
qoder_signin.risk_headers = patched_risk_headers


def main():
    return qoder_signin.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
