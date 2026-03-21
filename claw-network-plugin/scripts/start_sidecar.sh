#!/usr/bin/env bash
set -euo pipefail

ENDPOINT="${ENDPOINT:?ENDPOINT is required}"
RUNTIME_ID="${RUNTIME_ID:?RUNTIME_ID is required}"
LOBSTER_NAME="${LOBSTER_NAME:?LOBSTER_NAME is required}"
OWNER_NAME="${OWNER_NAME:?OWNER_NAME is required}"

PROJECT_DIR="${PROJECT_DIR:-/home/openclaw-a2a-mvp}"
PYTHON_BIN="${PYTHON_BIN:-/home/.venv/bin/python}"
DATA_DIR="${DATA_DIR:-${PROJECT_DIR}/agent_data}"
SIDECAR_SCRIPT="${PROJECT_DIR}/claw-network-plugin/scripts/sidecar_runner.py"

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  PYTHONPATH="${PROJECT_DIR}" \
  "${PYTHON_BIN}" "${SIDECAR_SCRIPT}" \
  --endpoint "${ENDPOINT}" \
  --runtime-id "${RUNTIME_ID}" \
  --name "${LOBSTER_NAME}" \
  --owner-name "${OWNER_NAME}" \
  --data-dir "${DATA_DIR}"
