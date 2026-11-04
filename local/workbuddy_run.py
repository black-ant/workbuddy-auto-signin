#!/usr/bin/env python3
"""WorkBuddy Windows 入口：钉住客户端可执行文件与凭据路径后转调上游 signin.py。

为什么要这一层：
  上游 signin.py 在 Windows 上会自己探测 `%LOCALAPPDATA%\\Programs\\WorkBuddy\\
  WorkBuddy.exe` 与 `%LOCALAPPDATA%\\workbuddy-desktop.info`，但计划任务以
  `pythonw` 启动时，环境里未必有完整的用户变量（LOCALAPPDATA 缺失、被改成
  System32 等）。显式钉死这两项，可以避免「手动跑成功、计划任务跑 NO_AUTH」。

不修改上游任何文件：只写环境变量，再用 runpy 以 `__main__` 身份执行 signin.py。
凭据路径只进环境变量，不打印、不落盘。

用法：
  workbuddy_run.py doctor | status | auto | silent | silent-poll | growth
"""

import io
import json
import os
import runpy
import sys

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(LOCAL_DIR)
UPSTREAM = os.path.join(REPO_DIR, "signin.py")
LOG_DIR = os.path.join(LOCAL_DIR, "logs")

# 与上游 _run() 的 known 列表保持一致，多一个少一个都会被上游判成未知命令
ACTIONS = ("auto", "silent", "growth", "silent-poll", "silent-growth",
           "status", "claim", "all", "doctor")


def log(msg):
    sys.stderr.write("[workbuddy] %s\n" % msg)


def _home():
    return os.environ.get("USERPROFILE") or os.path.expanduser("~")


def _local_appdata():
    return os.environ.get("LOCALAPPDATA") or os.path.join(
        _home(), "AppData", "Local")


def default_exe():
    """按上游同样的顺序找客户端，只用于给 WORKBUDDY_EXE 设默认值。"""
    local = _local_appdata()
    candidates = [os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe")]
    for name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(name)
        if base:
            candidates.append(os.path.join(base, "WorkBuddy", "WorkBuddy.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def default_auth_file():
    return os.path.join(_local_appdata(), "workbuddy-desktop.info")


# ---------------------------------------------------------------- 输出兜底
#
# pythonw.exe 下 sys.stdout is None，上游 emit() 里的 print 会直接抛异常，
# 当天的失败就无声消失了。这里把流换成内存缓冲，跑完再落盘。
# 只做兜底，不改变上游的 stdout 契约。

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
        path = os.path.join(LOG_DIR, "workbuddy_signin.log")
        with open(path, "a", encoding="utf-8") as f:
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
    action = args[0] if args else "auto"
    if action not in ACTIONS:
        return _fail("未知子命令：%s（可用：%s）"
                     % (action, " / ".join(ACTIONS)), 1)

    if not os.path.isfile(UPSTREAM):
        return _fail("未找到上游脚本：%s" % UPSTREAM, 1)

    # 只设默认值，显式传入的环境变量优先——换客户端版本时仍可覆盖
    if not os.environ.get("WORKBUDDY_EXE"):
        exe = default_exe()
        if exe:
            os.environ["WORKBUDDY_EXE"] = exe
        else:
            log("未探测到 WorkBuddy.exe，交给上游报 RUNTIME_NOT_FOUND")

    if not os.environ.get("WORKBUDDY_AUTH_FILE"):
        auth = default_auth_file()
        os.environ["WORKBUDDY_AUTH_FILE"] = auth
        log("凭据路径：%s（存在=%s）" % (auth, os.path.isfile(auth)))

    sys.argv = [UPSTREAM] + args
    try:
        runpy.run_path(UPSTREAM, run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else code
    except Exception as exc:
        # 上游 main() 已兜异常，走到这里说明是导入期失败
        return _fail("上游脚本运行异常（%s）" % type(exc).__name__, 2)


if __name__ == "__main__":
    _buf = _ensure_streams()
    try:
        rc = main()
    finally:
        _dump(_buf)
    sys.exit(rc)
