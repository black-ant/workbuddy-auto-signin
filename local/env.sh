#!/usr/bin/env bash
# 本机配置：所有与机器相关的路径集中在这里，其余脚本不写死路径。
# 换机器只改这个文件。

# WorkBuddy 桌面端登录凭据（绝对路径）
# 上游自动探测只认 CLI 凭据名，与桌面端文件名不同，必须显式指定。
export WORKBUDDY_AUTH_FILE="$HOME/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"

# WorkBuddy 可执行文件（用于提供 Electron 运行时解密凭据，不需要启动界面）
export WORKBUDDY_EXE="/opt/WorkBuddy/workbuddy"

# Qoder API 端点。国际版 openapi.qoder.sh，国区 openapi.qoder.com.cn。
# 依据：~/.config/Qoder/logs 下客户端日志实际请求的域名。
export QODER_BASE_URL="https://openapi.qoder.sh"

# Qoder 桌面端登录态（Linux 与 macOS/Windows 位置不同）
export QODER_LINUX_AUTH_FILE="$HOME/.config/com.qoder.app.stable/auth.v1.dat"

# Qoder 设备身份缓存（每日 100 Credits 活动必需）
export QODER_MACHINE_TOKEN="$HOME/.config/Qoder/SharedClientCache/cache/machine_token.json"

# 显式指定解释器：cryptography / dbus-next 装在独立 venv 里，不要用系统 python。
export SIGNIN_PYTHON="${SIGNIN_PYTHON:-$HOME/.venvs/workbuddy-signin/bin/python}"

# 日志目录（systemd 下也写这里，便于人工排查）
export LOCAL_LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs"
mkdir -p "$LOCAL_LOG_DIR"
