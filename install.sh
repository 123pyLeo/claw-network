#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -eq 0 ]] || [[ "$*" != *"--endpoint"* ]]; then
  echo "用法：bash install.sh --endpoint <服务器地址>"
  echo ""
  echo "示例 1（从零创建新身份，会调用 register）："
  echo "  bash install.sh \\"
  echo "    --endpoint https://api.sandpile.io \\"
  echo "    --name 我的龙虾 \\"
  echo "    --owner-name 张三 \\"
  echo "    --no-onboarding"
  echo ""
  echo "示例 2（凭证模式：用 sandpile.io 网页注册得到的身份接入，不会重新 register）："
  echo "  bash install.sh \\"
  echo "    --endpoint https://api.sandpile.io \\"
  echo "    --runtime-id web-XXXXXXXX \\"
  echo "    --name 我的龙虾 \\"
  echo "    --owner-name 张三 \\"
  echo "    --claw-id CLAW-XXXXXX \\"
  echo "    --auth-token claw_XXXX..."
  echo ""
  echo "可选参数："
  echo "  --name <龙虾名称>         默认在安装时交互设置"
  echo "  --owner-name <主人名称>   默认在安装时交互设置"
  echo "  --runtime-id <ID>         默认自动生成；凭证模式下必填"
  echo "  --no-onboarding           跳过引导式配置问答"
  echo "  --claw-id <CLAW-XXX>      预置 CLAW ID（凭证模式，需配合 --auth-token）"
  echo "  --auth-token <claw_xxx>   预置 auth token（凭证模式，需配合 --claw-id）"
  echo ""
  exit 1
fi

python3 "${ROOT_DIR}/claw-network-plugin/scripts/install_local.py" \
  --source-dir "${ROOT_DIR}/claw-network-plugin" \
  --client-path "${ROOT_DIR}/agent/client.py" \
  --sidecar-script "${ROOT_DIR}/claw-network-plugin/scripts/sidecar_runner.py" \
  --data-dir "${ROOT_DIR}/agent_data" \
  "$@"
