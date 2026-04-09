#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  VENV_PYTHON="/home/.venv/bin/python"
fi
PYTHON_BIN="${PYTHON_BIN:-$VENV_PYTHON}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
export PYTHONPATH="${PYTHONPATH:-$ROOT_DIR}"

echo "==> Running self_check"
"$PYTHON_BIN" "$ROOT_DIR/scripts/self_check.py"

echo "==> Running runtime_smoke"
"$PYTHON_BIN" "$ROOT_DIR/scripts/runtime_smoke.py" --project-dir "$ROOT_DIR"

echo "==> Running doctor (informational only; failures do not block validate.sh)"
"$PYTHON_BIN" "$ROOT_DIR/scripts/doctor.py" --project-dir "$ROOT_DIR" || true

echo "==> Validation completed successfully"
