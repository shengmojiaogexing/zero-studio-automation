#!/bin/bash
# ==============================================================================
# ZERO STUDIO · M1 Mac 监控机一键停止脚本
# ==============================================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

for name in booking im_gateway control_center frpc caffeinate; do
  PID_FILE="$ROOT_DIR/logs/mac_${name}.pid"
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID" 2>/dev/null || true
      echo "已停止 mac_${name} (PID $PID)"
    fi
    rm -f "$PID_FILE"
  fi
done
echo "M1 Mac 监控机服务已全部停止。"
