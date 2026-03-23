#!/usr/bin/env bash
set -euo pipefail

ENDPOINT="${ENDPOINT:?ENDPOINT is required}"
RUNTIME_ID="${RUNTIME_ID:?RUNTIME_ID is required}"
LOBSTER_NAME="${LOBSTER_NAME:?LOBSTER_NAME is required}"
OWNER_NAME="${OWNER_NAME:?OWNER_NAME is required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PROJECT_DIR="${PROJECT_DIR:-${DEFAULT_PROJECT_DIR}}"
PYTHON_BIN="${PYTHON_BIN:-/home/.venv/bin/python}"
DATA_DIR="${DATA_DIR:-${PROJECT_DIR}/agent_data}"
SIDECAR_SCRIPT="${PROJECT_DIR}/claw-network-plugin/scripts/sidecar_runner.py"
OPENCLAW_BRIDGE="${OPENCLAW_BRIDGE:-0}"
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
OPENCLAW_AGENT_ID="${OPENCLAW_AGENT_ID:-main}"
CONNECTION_REQUEST_POLICY="${CONNECTION_REQUEST_POLICY:-}"
COLLABORATION_POLICY="${COLLABORATION_POLICY:-}"
OFFICIAL_LOBSTER_POLICY="${OFFICIAL_LOBSTER_POLICY:-}"
SESSION_LIMIT_POLICY="${SESSION_LIMIT_POLICY:-}"

EXTRA_ARGS=()
if [[ "${OPENCLAW_BRIDGE}" == "1" ]]; then
  EXTRA_ARGS+=(--bridge-openclaw --openclaw-bin "${OPENCLAW_BIN}" --openclaw-agent-id "${OPENCLAW_AGENT_ID}")
fi
if [[ -n "${CONNECTION_REQUEST_POLICY}" ]]; then
  EXTRA_ARGS+=(--connection-request-policy "${CONNECTION_REQUEST_POLICY}")
fi
if [[ -n "${COLLABORATION_POLICY}" ]]; then
  EXTRA_ARGS+=(--collaboration-policy "${COLLABORATION_POLICY}")
fi
if [[ -n "${OFFICIAL_LOBSTER_POLICY}" ]]; then
  EXTRA_ARGS+=(--official-lobster-policy "${OFFICIAL_LOBSTER_POLICY}")
fi
if [[ -n "${SESSION_LIMIT_POLICY}" ]]; then
  EXTRA_ARGS+=(--session-limit-policy "${SESSION_LIMIT_POLICY}")
fi

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  PYTHONPATH="${PROJECT_DIR}" \
  "${PYTHON_BIN}" "${SIDECAR_SCRIPT}" \
  --endpoint "${ENDPOINT}" \
  --runtime-id "${RUNTIME_ID}" \
  --name "${LOBSTER_NAME}" \
  --owner-name "${OWNER_NAME}" \
  --data-dir "${DATA_DIR}" \
  "${EXTRA_ARGS[@]}"
