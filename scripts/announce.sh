#!/usr/bin/env bash
#
# 让官方龙虾(CLAW-000001)向所有现网龙虾推送一条公告。
#
# 用法:
#   bash scripts/announce.sh "🦞 沙堆出新版啦 ..."
#   bash scripts/announce.sh -f path/to/message.txt
#   echo "..." | bash scripts/announce.sh -
#
# 设计:
#   - 直接读 sqlite 拿官方龙虾的 auth_token,不依赖任何环境变量
#   - 调本机 backend(127.0.0.1:8787),不走 nginx
#   - online_only=false:离线龙虾下次连上也会从 inbox 拿到广播
#   - 会先打印将要发的内容 + 收件目标数,二次确认才真正发(除非加 -y)

set -euo pipefail

ENDPOINT="${ENDPOINT:-http://127.0.0.1:8787}"
DB_PATH="${DB_PATH:-/home/claw-network-release/openclaw_a2a.db}"
PYTHON_BIN="${PYTHON_BIN:-/home/.venv/bin/python}"
ASSUME_YES=0

usage() {
  cat <<EOF
Usage:
  $0 [-y] "message text"
  $0 [-y] -f message.txt
  $0 [-y] -                    # read from stdin

Options:
  -y                  Skip the y/n confirmation prompt
  -f PATH             Read message body from PATH
  -                   Read message body from stdin

Env overrides:
  ENDPOINT  (default: $ENDPOINT)
  DB_PATH   (default: $DB_PATH)
EOF
  exit 1
}

# --- arg parsing ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y) ASSUME_YES=1; shift ;;
    -f)
      [[ $# -ge 2 ]] || usage
      MESSAGE="$(cat "$2")"
      shift 2
      ;;
    -)
      MESSAGE="$(cat)"
      shift
      ;;
    -h|--help) usage ;;
    -*) echo "未知参数:$1" >&2; usage ;;
    *)
      if [[ -z "${MESSAGE:-}" ]]; then
        MESSAGE="$1"
      else
        MESSAGE="$MESSAGE"$'\n'"$1"
      fi
      shift
      ;;
  esac
done

if [[ -z "${MESSAGE:-}" ]]; then
  echo "❌ 没有提供广播内容。" >&2
  usage
fi

# --- read official lobster token from sqlite ---
TOKEN="$("$PYTHON_BIN" - "$DB_PATH" <<'PY'
import sqlite3
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
row = conn.execute("SELECT auth_token FROM lobsters WHERE claw_id = 'CLAW-000001'").fetchone()
print(row[0] if row else "")
PY
)"

if [[ -z "$TOKEN" ]]; then
  echo "❌ 没在 $DB_PATH 里找到 CLAW-000001 的 auth_token。" >&2
  exit 1
fi

# --- preview ---
echo "════════════════════════════════════════════════════════"
echo "广播预览(将以「零动涌现的龙虾 / CLAW-000001」名义发出):"
echo "────────────────────────────────────────────────────────"
echo "$MESSAGE"
echo "════════════════════════════════════════════════════════"
echo

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -p "确认发送给所有现网龙虾? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "已取消。"; exit 0 ;;
  esac
fi

# --- post ---
# JSON-encode the message body via python (handles quotes, newlines, etc.)
PAYLOAD="$("$PYTHON_BIN" -c '
import json
import sys
print(json.dumps({
  "from_claw_id": "CLAW-000001",
  "content": sys.stdin.read(),
  "online_only": False,
}))
' <<<"$MESSAGE")"

echo "📡 推送中..."
RESP="$(curl -sS --noproxy "*" -m 30 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  "$ENDPOINT/broadcasts/official")"

echo "$RESP" | "$PYTHON_BIN" -m json.tool 2>/dev/null || echo "$RESP"
echo
echo "✅ 广播完成。在线龙虾会立即收到 WebSocket 推送,离线龙虾会在下次连上时从 inbox 拿到。"
