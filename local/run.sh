#!/usr/bin/env bash
# WorkBuddy 入口：加载本机配置后调用上游 signin.py，透传所有子命令。
# 用法：./run.sh doctor | status | auto | silent-poll | growth
set -uo pipefail

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$LOCAL_DIR")"

# shellcheck source=env.sh
. "$LOCAL_DIR/env.sh"

# 子命令缺省为 auto，与上游一致
ACTION="${1:-auto}"

exec "$SIGNIN_PYTHON" "$REPO_DIR/signin.py" "$ACTION"
