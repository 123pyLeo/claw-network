#!/usr/bin/env bash
set -euo pipefail

PUBLIC_ENDPOINT="${PUBLIC_ENDPOINT:-https://api.sandpile.io}"
INTERNAL_ENDPOINT="${INTERNAL_ENDPOINT:-http://127.0.0.1:8787}"
USE_INTERNAL_ENDPOINT="${USE_INTERNAL_ENDPOINT:-0}"
if [[ -n "${ENDPOINT:-}" ]]; then
  RESOLVED_ENDPOINT="${ENDPOINT}"
elif [[ "${USE_INTERNAL_ENDPOINT}" == "1" ]]; then
  RESOLVED_ENDPOINT="${INTERNAL_ENDPOINT}"
else
  RESOLVED_ENDPOINT="${PUBLIC_ENDPOINT}"
fi

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
AUTONOMOUS_ROUNDTABLES="${AUTONOMOUS_ROUNDTABLES:-0}"
ROUNDTABLE_MAX_TURNS="${ROUNDTABLE_MAX_TURNS:-20}"
ROUNDTABLE_MAX_DURATION_SECONDS="${ROUNDTABLE_MAX_DURATION_SECONDS:-300}"
ROUNDTABLE_IDLE_TIMEOUT_SECONDS="${ROUNDTABLE_IDLE_TIMEOUT_SECONDS:-120}"
ROUNDTABLE_POLL_SECONDS="${ROUNDTABLE_POLL_SECONDS:-8}"
CONNECTION_REQUEST_POLICY="${CONNECTION_REQUEST_POLICY:-}"
COLLABORATION_POLICY="${COLLABORATION_POLICY:-}"
OFFICIAL_LOBSTER_POLICY="${OFFICIAL_LOBSTER_POLICY:-}"
SESSION_LIMIT_POLICY="${SESSION_LIMIT_POLICY:-}"
ROUNDTABLE_NOTIFICATION_MODE="${ROUNDTABLE_NOTIFICATION_MODE:-}"

EXTRA_ARGS=()
if [[ "${OPENCLAW_BRIDGE}" == "1" ]]; then
  EXTRA_ARGS+=(--bridge-openclaw --openclaw-bin "${OPENCLAW_BIN}" --openclaw-agent-id "${OPENCLAW_AGENT_ID}")
fi
if [[ "${AUTONOMOUS_ROUNDTABLES}" == "1" ]]; then
  EXTRA_ARGS+=(
    --autonomous-roundtables
    --roundtable-max-turns "${ROUNDTABLE_MAX_TURNS}"
    --roundtable-max-duration-seconds "${ROUNDTABLE_MAX_DURATION_SECONDS}"
    --roundtable-idle-timeout-seconds "${ROUNDTABLE_IDLE_TIMEOUT_SECONDS}"
    --roundtable-poll-seconds "${ROUNDTABLE_POLL_SECONDS}"
  )
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
if [[ -n "${ROUNDTABLE_NOTIFICATION_MODE}" ]]; then
  EXTRA_ARGS+=(--roundtable-notification-mode "${ROUNDTABLE_NOTIFICATION_MODE}")
fi

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  PYTHONPATH="${PROJECT_DIR}" \
  "${PYTHON_BIN}" "${SIDECAR_SCRIPT}" \
  --endpoint "${RESOLVED_ENDPOINT}" \
  --runtime-id "${RUNTIME_ID}" \
  --name "${LOBSTER_NAME}" \
  --owner-name "${OWNER_NAME}" \
  --data-dir "${DATA_DIR}" \
  "${EXTRA_ARGS[@]}"
