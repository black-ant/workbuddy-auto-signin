#!/usr/bin/env bash
# CodeArts Agent 入口：加载集中配置后转调 codearts_signin.py。
# 用法：./run-codearts.sh doctor | status | auto
set -uo pipefail

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=env.sh
. "$LOCAL_DIR/env.sh"

ACTION="${1:-auto}"
case "$ACTION" in
  auto|status|doctor) ;;
  *)
    echo "{\"result\":\"ERROR\",\"report\":\"未知子命令：$ACTION\"}"
    exit 1
    ;;
esac

# 凭据只在 Python 进程内解出并使用，不落盘、不打印、不经环境变量传递
exec "$SIGNIN_PYTHON" "$LOCAL_DIR/codearts_signin.py" "$ACTION"
