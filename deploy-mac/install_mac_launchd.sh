#!/bin/bash
# 一键注册 macOS LaunchAgent：Mac 开机/登录后自动启动全栈服务与后台监控清理守护
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.zerostudio.watchdog.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
chmod +x "$ROOT/deploy-mac/mac_watchdog.sh" "$ROOT/deploy-mac/start_mac_monitor.sh" "$ROOT/deploy-mac/restart_mac_services.sh"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.zerostudio.watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$ROOT/deploy-mac/mac_watchdog.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$ROOT/logs/mac_watchdog_launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/mac_watchdog_launchd.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ 已注册 macOS 开机自启监控守护: $PLIST"
