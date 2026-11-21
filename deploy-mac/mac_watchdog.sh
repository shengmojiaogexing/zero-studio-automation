#!/bin/bash
# ==============================================================================
# ZERO STUDIO · Mac 主站自动清理后台 + 爆满监控与自动重启守护 (mac_watchdog.sh)
# 功能：
#   1. 每 30 秒巡检 booking-site(:3000) / control-center(:3080) / frpc / caffeinate
#      连续异常自动拉起，保证 24h 不掉线；
#   2. 定时自动清理后台残留重复进程、超大日志（>30MB 自动截断）、过期临时缓存；
#   3. 监控 macOS 内存压力与磁盘使用率：
#      - 内存使用率 >= MAC_MEM_THRESHOLD_PCT (默认 88%) 时自动清理缓存并平滑重启服务释放内存；
#      - 磁盘使用率 >= MAC_DISK_THRESHOLD_PCT (默认 88%) 时自动清理临时目录并推送告警；
#   4. 异常恢复与资源告警支持通过 .env 中的 PUSHPLUS_TOKEN 自动微信通知。
# ==============================================================================

export PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

mkdir -p "$ROOT/logs"
LOG_FILE="$ROOT/logs/mac_watchdog.log"
PID_FILE="$ROOT/logs/mac_watchdog.pid"

# 防止重复启动多个 watchdog
if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && [ "$OLD_PID" != "$$" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[$(date '+%F %T')] mac_watchdog already running (PID=$OLD_PID), exiting." >> "$LOG_FILE"
    exit 0
  fi
fi
echo "$$" > "$PID_FILE"

# 加载 .env
if [ -f "$ROOT/.env" ]; then
  set -a
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    export "$key=$val"
  done < "$ROOT/.env"
  set +a
fi

CHECK_INTERVAL="${MAC_WATCHDOG_INTERVAL:-30}"
MEM_THRESHOLD="${MAC_MEM_THRESHOLD_PCT:-88}"
DISK_THRESHOLD="${MAC_DISK_THRESHOLD_PCT:-88}"
MAX_LOG_MB="${MAC_MAX_LOG_MB:-30}"
LAST_CLEAN_TS=0
LAST_ALERT_TS=0
BOOKING_FAILS=0
CC_FAILS=0
HIGH_MEM_STREAK=0

log() {
  echo "[$(date '+%F %T')] [watchdog] $*" >> "$LOG_FILE"
}

notify_pushplus() {
  local title="$1"
  local content="$2"
  local now
  now=$(date +%s)
  # 同一告警至少间隔 10 分钟，防刷屏
  if [ $((now - LAST_ALERT_TS)) -lt 600 ]; then return 0; fi
  if [ -z "${PUSHPLUS_TOKEN:-}" ]; then return 0; fi
  LAST_ALERT_TS=$now
  curl -sS -m 8 -X POST "https://www.pushplus.plus/send" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"$PUSHPLUS_TOKEN\",\"title\":\"$title\",\"content\":\"$content\"}" >/dev/null 2>&1 || true
}

get_mem_used_pct() {
  # macOS 精确计算活跃+联动+压缩内存占比
  python3 - <<'PY' 2>/dev/null || echo 0
import subprocess, re
vm = subprocess.check_output(["vm_stat"], text=True)
ps = int(re.search(r"page size of (\d+) bytes", vm).group(1))
def pages(key):
    m = re.search(rf"{re.escape(key)}:\s+(\d+)\.", vm)
    return int(m.group(1)) if m else 0
total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * ps
print(int(round(used * 100.0 / max(total, 1))))
PY
}

get_disk_used_pct() {
  df -P / | awk 'NR==2 {gsub("%","",$5); print int($5)}'
}

cleanup_background_and_logs() {
  log "执行定时后台与日志清理..."

  # 1. 截断超过 MAX_LOG_MB 的日志文件，保留最后 2000 行
  local max_bytes=$((MAX_LOG_MB * 1024 * 1024))
  for lf in "$ROOT"/logs/*.log; do
    [ -f "$lf" ] || continue
    local sz
    sz=$(stat -f%z "$lf" 2>/dev/null || echo 0)
    if [ "$sz" -gt "$max_bytes" ]; then
      tail -n 2000 "$lf" > "${lf}.trim" && mv "${lf}.trim" "$lf"
      log "已裁剪超大日志: $(basename "$lf") (原 $((sz / 1024 / 1024))MB)"
    fi
  done

  # 2. 清理 7 天以上的沙盒临时文件与 .tmp/.part 残留
  if [ -d "$ROOT/work/sandbox" ]; then
    find "$ROOT/work/sandbox" -mindepth 1 -mtime +7 -exec rm -rf {} + 2>/dev/null || true
  fi
  find "$ROOT/work" -name "*.tmp" -o -name "*.part" -mtime +2 -delete 2>/dev/null || true

  # 3. 清理多余重复的 caffeinate 进程（只留 1 个）
  local caff_pids
  caff_pids=($(pgrep -x caffeinate 2>/dev/null || true))
  if [ "${#caff_pids[@]}" -gt 1 ]; then
    for ((i=1; i<${#caff_pids[@]}; i++)); do
      kill "${caff_pids[$i]}" 2>/dev/null || true
    done
    log "已清理 ${#caff_pids[@]} 个重复 caffeinate，仅保留 1 个"
  elif [ "${#caff_pids[@]}" -eq 0 ]; then
    nohup caffeinate -dims >/dev/null 2>&1 &
    echo $! > "$ROOT/logs/mac_caffeinate.pid"
    log "caffeinate 未运行，已自动重新拉起"
  fi

  # 4. 确保 frpc 在线（不重复多开）
  local frpc_pids
  frpc_pids=($(pgrep -f "frpc.*frpc.toml" 2>/dev/null || true))
  if [ "${#frpc_pids[@]}" -eq 0 ]; then
    local frpc_bin="$ROOT/infra/tunnel/bin/frpc_darwin_arm64"
    [ -x "$frpc_bin" ] || frpc_bin="$(command -v frpc 2>/dev/null || true)"
    if [ -n "$frpc_bin" ] && [ -x "$frpc_bin" ]; then
      (cd "$ROOT" && nohup "$frpc_bin" -c "$ROOT/infra/tunnel/frpc.toml" >> "$ROOT/logs/mac_frpc.log" 2>&1 & echo $! > "$ROOT/logs/mac_frpc.pid")
      log "检测到 frpc 掉线，已自动拉起"
      notify_pushplus "ZERO STUDIO Mac 隧道恢复" "检测到 frpc 掉线，监控已自动重启恢复外网穿透。"
    fi
  elif [ "${#frpc_pids[@]}" -gt 1 ]; then
    for ((i=1; i<${#frpc_pids[@]}; i++)); do
      kill "${frpc_pids[$i]}" 2>/dev/null || true
    done
    log "已清理多余的重复 frpc 进程"
  fi
}

restart_web_services() {
  local reason="$1"
  log "触发服务平滑重启，原因: $reason"
  bash "$ROOT/deploy-mac/restart_mac_services.sh" >> "$LOG_FILE" 2>&1 || true
  notify_pushplus "ZERO STUDIO Mac 自动重启恢复" "原因：$reason\n已自动重启 booking-site 与 control-center 服务。"
}

log "Mac 守护监控已启动 (PID=$$, interval=${CHECK_INTERVAL}s, mem_limit=${MEM_THRESHOLD}%, disk_limit=${DISK_THRESHOLD}%)"

while true; do
  NOW=$(date +%s)

  # 1) 每小时自动做一次后台与日志清理
  if [ $((NOW - LAST_CLEAN_TS)) -ge 3600 ]; then
    cleanup_background_and_logs
    LAST_CLEAN_TS=$NOW
  fi

  # 2) 健康探活：booking-site (:3000) & control-center (:3080)
  B_CODE=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:3000/api/services || echo "000")
  if [ "$B_CODE" != "200" ]; then
    BOOKING_FAILS=$((BOOKING_FAILS + 1))
    log "booking-site 健康异常 (HTTP $B_CODE, 连续第 $BOOKING_FAILS 次)"
  else
    BOOKING_FAILS=0
  fi

  C_CODE=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:3080/login || echo "000")
  if [ "$C_CODE" != "200" ]; then
    CC_FAILS=$((CC_FAILS + 1))
    log "control-center 健康异常 (HTTP $C_CODE, 连续第 $CC_FAILS 次)"
  else
    CC_FAILS=0
  fi

  if [ "$BOOKING_FAILS" -ge 3 ] || [ "$CC_FAILS" -ge 3 ]; then
    restart_web_services "服务连续探活失败 (booking=$B_CODE, cc=$C_CODE)"
    BOOKING_FAILS=0
    CC_FAILS=0
  fi

  # 3) 内存爆满监控（连续 3 次超阈值自动清缓存并重启服务释放内存）
  MEM_PCT=$(get_mem_used_pct)
  if [ "$MEM_PCT" -ge "$MEM_THRESHOLD" ]; then
    HIGH_MEM_STREAK=$((HIGH_MEM_STREAK + 1))
    log "内存偏高告警: ${MEM_PCT}% >= ${MEM_THRESHOLD}% (连续第 $HIGH_MEM_STREAK 次)"
    if [ "$HIGH_MEM_STREAK" -ge 3 ]; then
      cleanup_background_and_logs
      restart_web_services "内存持续爆满 (${MEM_PCT}%)"
      HIGH_MEM_STREAK=0
    fi
  else
    HIGH_MEM_STREAK=0
  fi

  # 4) 磁盘爆满监控
  DISK_PCT=$(get_disk_used_pct)
  if [ "$DISK_PCT" -ge "$DISK_THRESHOLD" ]; then
    log "磁盘空间紧张: ${DISK_PCT}% >= ${DISK_THRESHOLD}%，执行紧急清理..."
    cleanup_background_and_logs
    # 清理 30 天以上的中转上传缓存（不影响原始 RAW）
    find "$ROOT/apps/booking-site/uploads/outputs/worker_uploads" -type f -mtime +30 -delete 2>/dev/null || true
    notify_pushplus "ZERO STUDIO Mac 磁盘告警" "Mac 系统盘使用率已达 ${DISK_PCT}%，已自动清理过期日志与临时缓存，请及时检查磁盘空间。"
  fi

  sleep "$CHECK_INTERVAL"
done
