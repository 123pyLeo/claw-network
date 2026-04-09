#!/usr/bin/env bash
# Install the daily cleanup cron job for anonymous lobsters.
# Runs the cleanup_anonymous_lobsters.py script every day at 04:00 UTC.
#
# Usage: bash scripts/install-cleanup-cron.sh
#
# Idempotent — safe to re-run.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/home/.venv/bin/python}"
SCRIPT="${ROOT_DIR}/scripts/cleanup_anonymous_lobsters.py"
LOG_FILE="/var/log/sandpile-cleanup.log"

if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: cleanup script not found at $SCRIPT" >&2
  exit 1
fi

CRON_LINE="0 4 * * * ${PYTHON_BIN} ${SCRIPT} >> ${LOG_FILE} 2>&1"
MARKER="# sandpile-anonymous-cleanup"

# Build new crontab: take existing entries minus our marker line, then append fresh
EXISTING=$(crontab -l 2>/dev/null || true)
FILTERED=$(echo "$EXISTING" | grep -v "${MARKER}" || true)

(
  echo "${FILTERED}"
  echo "${CRON_LINE} ${MARKER}"
) | crontab -

echo "✓ Installed cron job:"
echo "  ${CRON_LINE}"
echo
echo "Verify with:  crontab -l | grep sandpile"
echo "Logs at:      ${LOG_FILE}"
