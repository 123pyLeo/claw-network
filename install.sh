#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "${ROOT_DIR}/claw-network-plugin/scripts/install_local.py" \
  --source-dir "${ROOT_DIR}/claw-network-plugin" \
  --client-path "${ROOT_DIR}/agent/client.py" \
  --sidecar-script "${ROOT_DIR}/claw-network-plugin/scripts/sidecar_runner.py" \
  --data-dir "${ROOT_DIR}/agent_data" \
  "$@"
