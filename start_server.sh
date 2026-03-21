#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8787}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  PYTHONPATH="${ROOT_DIR}" \
  "${PYTHON_BIN}" -m uvicorn server.main:app --host "${HOST}" --port "${PORT}"
