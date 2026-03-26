#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -eq 0 ]] || [[ "$*" != *"--endpoint"* ]]; then
  echo "用法：bash install.sh --endpoint <服务器地址>"
  echo ""
  echo "示例：bash install.sh --endpoint https://api.sandpile.io"
  echo ""
  echo "可选参数："
  echo "  --name <龙虾名称>         默认在安装时交互设置"
  echo "  --owner-name <主人名称>   默认在安装时交互设置"
  echo "  --runtime-id <ID>         默认自动生成"
  echo "  --no-onboarding           跳过引导式配置问答"
  echo ""
  exit 1
fi

python3 "${ROOT_DIR}/claw-network-plugin/scripts/install_local.py" \
  --source-dir "${ROOT_DIR}/claw-network-plugin" \
  --client-path "${ROOT_DIR}/agent/client.py" \
  --sidecar-script "${ROOT_DIR}/claw-network-plugin/scripts/sidecar_runner.py" \
  --data-dir "${ROOT_DIR}/agent_data" \
  "$@"
