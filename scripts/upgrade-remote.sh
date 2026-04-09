#!/usr/bin/env bash
#
# claw-network 远程一键升级脚本。
#
# 使用方法(用户视角):
#   curl -fsSL https://sandpile.io/upgrade.sh | bash
#
# 它会做的事:
#   1. 读 ~/.openclaw/openclaw.json,找到当前 claw-network 的本地 git 仓库目录
#   2. 在那个目录里 git fetch + git pull --ff-only origin main
#   3. exec ./upgrade.sh,由它完成 plugin 重装 + gateway 重启
#
# 设计原则:
#   - 只走 fast-forward,不会 rebase 也不会 merge,避免破坏用户的本地修改
#   - 任何步骤失败立即停下并打印中文原因
#   - 不需要 sudo:所有写操作都在 ~/.openclaw 和用户的 git 工作树里
#   - 跟仓库自带的 upgrade.sh 互补:upgrade.sh 假设源代码已经是新的,
#     这个脚本只是在它前面套一层 git pull

set -euo pipefail

# 让中文输出在各种终端都不乱码
export LANG="${LANG:-C.UTF-8}"

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CONFIG_PATH="$OPENCLAW_HOME/openclaw.json"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

die() {
  red "❌ $*"
  exit 1
}

command -v git >/dev/null 2>&1 || die "未找到 git 命令,请先安装 git。"
command -v python3 >/dev/null 2>&1 || die "未找到 python3 命令。"

if [[ ! -f "$CONFIG_PATH" ]]; then
  die "未找到 OpenClaw 配置:$CONFIG_PATH
  这说明你还没有装过 claw-network。请先按官方安装步骤跑一次 install.sh,
  之后再用这个一键升级脚本。"
fi

# 从 openclaw.json 反推出 claw-network 的本地 git 工作树位置。
# clientPath 字段指向 <repo>/agent/client.py,所以 repo 根 = dirname(dirname(clientPath))。
REPO_DIR="$(python3 - "$CONFIG_PATH" <<'PY'
import json, os, sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
client_path = (
    config.get("plugins", {})
    .get("entries", {})
    .get("claw-network", {})
    .get("config", {})
    .get("clientPath")
)
if not client_path:
    print("")
    sys.exit(0)
# 期望:.../<repo>/agent/client.py → <repo>
candidate = Path(client_path).resolve().parent.parent
print(str(candidate))
PY
)"

if [[ -z "$REPO_DIR" || ! -d "$REPO_DIR/.git" ]]; then
  die "在 $CONFIG_PATH 里找不到 claw-network 的本地仓库路径,
  或者那个目录不是一个 git 仓库。
  你可能是手动把文件拷进来的,而不是 git clone。
  这种情况下,请回到你最初装 claw-network 的终端记录里找一下源目录,
  或者在你常用的位置重新 git clone:
    git clone https://github.com/123pyLeo/claw-network.git
    cd claw-network
    bash install.sh --endpoint https://api.sandpile.io"
fi

green "📂 找到 claw-network 仓库:$REPO_DIR"
echo

# 检查工作树干净 —— 如果用户在自己的目录里改过文件,我们不能盖掉
cd "$REPO_DIR"
if ! git diff --quiet || ! git diff --cached --quiet; then
  yellow "⚠️  你的本地有未提交的改动:"
  git status --short
  die "升级会拉远端代码,但本地有改动,可能会冲突。
  如果你不在意这些改动,先 git stash 或 git checkout -- . 清掉,再重新跑这个脚本。
  如果你想保留,请人工合并。"
fi

# 拉远端
green "⬇️  拉取最新代码 ..."
git fetch origin main || die "git fetch 失败,检查网络或仓库地址。"

LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git rev-parse origin/main)"

if [[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]]; then
  green "✅ 你已经是最新版本($LOCAL_HEAD),不需要升级。"
  exit 0
fi

git pull --ff-only origin main || die "git pull --ff-only 失败。
  这通常意味着你的本地分支跟远端分叉了,无法直接快进。
  解决方法:
    cd $REPO_DIR
    git log --oneline HEAD..origin/main   # 看看远端有什么新提交
    git reset --hard origin/main          # 警告:会丢掉你的本地提交
  然后再跑 bash upgrade.sh"

green "📦 已更新到最新提交:$(git rev-parse --short HEAD)"
echo

# 把控制权交给仓库自带的 upgrade.sh
if [[ ! -x "$REPO_DIR/upgrade.sh" ]]; then
  chmod +x "$REPO_DIR/upgrade.sh" 2>/dev/null || true
fi

if [[ ! -f "$REPO_DIR/upgrade.sh" ]]; then
  die "新版本里没找到 upgrade.sh,无法继续。请联系沙堆维护者。"
fi

green "🔧 开始本地重装(plugin + gateway 重启)..."
echo
exec bash "$REPO_DIR/upgrade.sh"
