#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CONFIG_PATH="$OPENCLAW_HOME/openclaw.json"
PLUGIN_DIR="$OPENCLAW_HOME/extensions/claw-network"
PLUGIN_KEY="plugins.entries.claw-network.config"
BACKUP_ROOT="$OPENCLAW_HOME/backups"
BACKUP_STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/claw-network-upgrade-$BACKUP_STAMP"
CONFIG_BACKUP_PATH="$BACKUP_DIR/openclaw.json"
PLUGIN_BACKUP_DIR="$BACKUP_DIR/claw-network"
ROLLBACK_NEEDED=0
ROLLBACK_MESSAGE=""

restore_backup() {
  local had_failure="${1:-0}"
  if [[ "$ROLLBACK_NEEDED" -ne 1 ]]; then
    return 0
  fi

  echo
  echo "升级没有完成，正在回滚到旧版本..."

  if [[ -f "$CONFIG_BACKUP_PATH" ]]; then
    cp -f "$CONFIG_BACKUP_PATH" "$CONFIG_PATH"
    echo "  已恢复配置文件：$CONFIG_PATH"
  fi

  if [[ -d "$PLUGIN_BACKUP_DIR" ]]; then
    mkdir -p "$(dirname "$PLUGIN_DIR")"
    rm -rf "$PLUGIN_DIR"
    cp -a "$PLUGIN_BACKUP_DIR" "$PLUGIN_DIR"
    echo "  已恢复插件目录：$PLUGIN_DIR"
  fi

  if command -v openclaw >/dev/null 2>&1; then
    if openclaw gateway restart >/dev/null 2>&1; then
      echo "  已重启 OpenClaw gateway 并切回旧版本"
    else
      echo "  警告：回滚后自动重启 gateway 失败，请手动执行：openclaw gateway restart"
    fi
  fi

  if [[ "$had_failure" -eq 1 ]]; then
    echo "$ROLLBACK_MESSAGE"
  fi
}

on_error() {
  local exit_code="$1"
  ROLLBACK_MESSAGE="升级失败，但已自动恢复旧版本。你当前的龙虾身份、好友关系和本地数据没有受影响。"
  restore_backup 1
  exit "$exit_code"
}

trap 'on_error $?' ERR

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "未找到现有 OpenClaw 配置：$CONFIG_PATH"
  echo "这看起来不是升级，而是首次安装。"
  echo "请先执行：bash \"$ROOT_DIR/install.sh\" --endpoint <服务器地址>"
  exit 1
fi

read_config_value() {
  local key="$1"
  python3 - "$CONFIG_PATH" "$key" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
key = sys.argv[2]
data = json.loads(config_path.read_text(encoding="utf-8"))
node = data
for part in key.split("."):
    if not isinstance(node, dict) or part not in node:
        print("")
        raise SystemExit(0)
    node = node[part]
if isinstance(node, str):
    print(node)
else:
    print(json.dumps(node, ensure_ascii=False))
PY
}

CURRENT_ENDPOINT="$(read_config_value "$PLUGIN_KEY.endpoint")"
CURRENT_RUNTIME_ID="$(read_config_value "$PLUGIN_KEY.runtimeId")"
CURRENT_NAME="$(read_config_value "$PLUGIN_KEY.name")"
CURRENT_OWNER_NAME="$(read_config_value "$PLUGIN_KEY.ownerName")"
CURRENT_PYTHON_BIN="$(read_config_value "$PLUGIN_KEY.pythonBin")"
CURRENT_CLIENT_PATH="$(read_config_value "$PLUGIN_KEY.clientPath")"
CURRENT_DATA_DIR="$(read_config_value "$PLUGIN_KEY.dataDir")"
CURRENT_SIDECAR_SCRIPT="$(read_config_value "$PLUGIN_KEY.sidecarScript")"

if [[ -z "$CURRENT_ENDPOINT" || -z "$CURRENT_RUNTIME_ID" || -z "$CURRENT_NAME" || -z "$CURRENT_OWNER_NAME" ]]; then
  echo "当前 claw-network 配置不完整，无法安全升级。"
  echo "至少缺少以下字段之一：endpoint / runtimeId / name / ownerName"
  echo "建议先执行：python3 \"$ROOT_DIR/scripts/repair_instance.py\""
  exit 1
fi

echo "开始升级现有 claw-network 安装"
echo "  openclaw-home : $OPENCLAW_HOME"
echo "  endpoint      : $CURRENT_ENDPOINT"
echo "  runtimeId     : $CURRENT_RUNTIME_ID"
echo "  name          : $CURRENT_NAME"
echo "  ownerName     : $CURRENT_OWNER_NAME"

mkdir -p "$BACKUP_DIR"
cp -f "$CONFIG_PATH" "$CONFIG_BACKUP_PATH"
if [[ -d "$PLUGIN_DIR" ]]; then
  cp -a "$PLUGIN_DIR" "$PLUGIN_BACKUP_DIR"
fi
ROLLBACK_NEEDED=1

echo "  backup        : $BACKUP_DIR"

python3 "$ROOT_DIR/claw-network-plugin/scripts/install_local.py" \
  --openclaw-home "$OPENCLAW_HOME" \
  --source-dir "$ROOT_DIR/claw-network-plugin" \
  --endpoint "$CURRENT_ENDPOINT" \
  --runtime-id "$CURRENT_RUNTIME_ID" \
  --name "$CURRENT_NAME" \
  --owner-name "$CURRENT_OWNER_NAME" \
  --python-bin "${CURRENT_PYTHON_BIN:-python3}" \
  --client-path "${CURRENT_CLIENT_PATH:-$ROOT_DIR/agent/client.py}" \
  --data-dir "${CURRENT_DATA_DIR:-$ROOT_DIR/agent_data}" \
  --sidecar-script "${CURRENT_SIDECAR_SCRIPT:-$ROOT_DIR/claw-network-plugin/scripts/sidecar_runner.py}" \
  --no-onboarding

python3 "$ROOT_DIR/claw-network-plugin/scripts/migrate_config.py" \
  --openclaw-home "$OPENCLAW_HOME"

python3 "$ROOT_DIR/scripts/repair_instance.py" \
  --openclaw-home "$OPENCLAW_HOME" \
  --project-dir "$ROOT_DIR"

python3 "$ROOT_DIR/scripts/smoke_test.py" \
  --openclaw-home "$OPENCLAW_HOME" \
  --project-dir "$ROOT_DIR" \
  --openclaw-bin openclaw

if command -v openclaw >/dev/null 2>&1; then
  openclaw gateway restart
else
  echo "警告：未找到 openclaw 命令，新插件暂未自动重启生效。"
fi

ROLLBACK_NEEDED=0
trap - ERR

echo
echo "升级完成。"
echo "本次升级默认保留了现有 runtimeId，因此不会重新分配龙虾 ID。"
echo "现有好友关系和本地数据已保留。"
echo "升级前备份保存在：$BACKUP_DIR"
