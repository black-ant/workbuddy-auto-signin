#!/usr/bin/env bash
# Qoder 入口：先取 token 注入 QODER_TOKEN，再调用 qoder_runner.py（带设备头补丁）。
# 用法：./run-qoder.sh doctor | status | auto | silent
set -uo pipefail

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$LOCAL_DIR")"

# shellcheck source=env.sh
. "$LOCAL_DIR/env.sh"

ACTION="${1:-auto}"

TOKEN="$("$SIGNIN_PYTHON" "$LOCAL_DIR/qoder_token.py")"
RC=$?
if [ $RC -ne 0 ] || [ -z "${TOKEN:-}" ]; then
  # token 取不到就直接退出，把原因留在标准错误里（qoder_token.py 已打印）
  echo "{\"result\":\"NO_AUTH\",\"report\":\"本地登录态不可解密（qoder_token.py 退出码 $RC）\"}"
  exit 2
fi

# token 只经环境变量交给上游，不落盘、不进日志
export QODER_TOKEN="$TOKEN"
unset TOKEN

exec "$SIGNIN_PYTHON" "$LOCAL_DIR/qoder_runner.py" "$ACTION"
