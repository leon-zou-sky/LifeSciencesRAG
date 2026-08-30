#!/bin/bash
# 安装每日持续验证定时任务（launchd）
# 用法：bash scripts/install_daily_verify.sh
# 卸载：launchctl bootout gui/$(id -u)/com.lsrag.daily-verify && rm ~/Library/LaunchAgents/com.lsrag.daily-verify.plist
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONDA_BIN="$(command -v conda || true)"
if [ -z "$CONDA_BIN" ]; then
  echo "❌ 找不到 conda——先装好 conda 再运行"; exit 1
fi

PLIST=~/Library/LaunchAgents/com.lsrag.daily-verify.plist
LOG_DIR="$REPO_ROOT/reports/scheduled"
mkdir -p "$LOG_DIR"

# 每天 07:23 本地时间跑（笔记本合盖错过的话，launchd 会在唤醒后补跑一次）
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.lsrag.daily-verify</string>
  <key>ProgramArguments</key>
  <array>
    <string>$CONDA_BIN</string>
    <string>run</string><string>-n</string><string>py311</string><string>--no-capture-output</string>
    <string>python</string>
    <string>$REPO_ROOT/scripts/scheduled_verification.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>23</integer></dict>
  <key>StandardOutPath</key><string>$LOG_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchd.err.log</string>
</dict>
</plist>
EOF

# 已装过先卸再装（幂等）
launchctl bootout "gui/$(id -u)/com.lsrag.daily-verify" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✅ 已安装：每天 07:23 自动跑持续验证"
echo "   手动触发一次：launchctl kickstart gui/\$(id -u)/com.lsrag.daily-verify"
echo "   查看留痕：$LOG_DIR/verify_runs.jsonl"
echo "   注意：MySQL/Milvus 容器与 Ollama 需在运行时刻在线，否则记 incomplete（不是红，但也不是绿）"
