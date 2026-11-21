#!/bin/bash
# Restart the live ZERO STUDIO services on the Mac.
# IMPORTANT: booking-site reads ADMIN_PASSWORD / INTERNAL_API_KEY from the process
# environment, so .env MUST be loaded before starting it (otherwise it falls back to
# the insecure default "change-me" and every admin save returns 401).
export PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
ROOT="$HOME/zero-studio-automation"
cd "$ROOT" || exit 1

if [ ! -f "$ROOT/.env" ]; then
  echo "[FATAL] $ROOT/.env not found"; exit 1
fi

# Load .env into the environment (strip CR, ignore comments/blank lines)
set -a
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in
    ''|\#*) continue ;;
  esac
  key="${line%%=*}"
  val="${line#*=}"
  # trim surrounding whitespace and optional quotes
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  case "$val" in
    \"*\") val="${val#\"}"; val="${val%\"}" ;;
    \'*\') val="${val#\'}"; val="${val%\'}" ;;
  esac
  export "$key=$val"
done < "$ROOT/.env"
set +a

echo "=== env sanity ==="
echo "ADMIN_USER=$ADMIN_USER"
echo "ADMIN_PASSWORD length=${#ADMIN_PASSWORD}"
echo "INTERNAL_API_KEY length=${#INTERNAL_API_KEY}"

echo "=== stopping booking-site / control-center ==="
if [ -f logs/mac_booking.pid ]; then kill "$(cat logs/mac_booking.pid)" 2>/dev/null; fi
if [ -f logs/mac_control_center.pid ]; then kill "$(cat logs/mac_control_center.pid)" 2>/dev/null; fi
for pid in $(pgrep -f "node server.js"); do
  cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | cut -c2-)
  if [ "$cwd" = "$ROOT/apps/booking-site" ]; then kill "$pid" 2>/dev/null; echo "killed booking $pid"; fi
done
for pid in $(pgrep -f "uvicorn main:app --host 0.0.0.0 --port 3080"); do
  kill "$pid" 2>/dev/null; echo "killed cc $pid"
done
sleep 2

echo "=== starting booking-site :3000 (with .env) ==="
(
  cd "$ROOT/apps/booking-site" || exit 1
  nohup node server.js > "$ROOT/logs/mac_booking.log" 2>&1 &
  echo $! > "$ROOT/logs/mac_booking.pid"
)
sleep 3

echo "=== starting control-center :3080 (with .env) ==="
(
  cd "$ROOT/apps/control-center" || exit 1
  nohup "$ROOT/.venv-mac/bin/python" -m uvicorn main:app --host 0.0.0.0 --port 3080 > "$ROOT/logs/mac_control_center.log" 2>&1 &
  echo $! > "$ROOT/logs/mac_control_center.pid"
)
sleep 5

echo "=== health ==="
curl -s -o /dev/null -w "booking:3000 services=%{http_code}\n" http://127.0.0.1:3000/api/services
curl -s -o /dev/null -w "cc:3080 login=%{http_code}\n" http://127.0.0.1:3080/login

echo "=== admin auth check (must be 200, not 401) ==="
curl -s -o /dev/null -w "admin /api/bookings=%{http_code}\n" -u "$ADMIN_USER:$ADMIN_PASSWORD" http://127.0.0.1:3000/api/bookings

echo "=== ensuring mac_watchdog is running ==="
if [ -f "$ROOT/deploy-mac/mac_watchdog.sh" ]; then
  chmod +x "$ROOT/deploy-mac/mac_watchdog.sh"
  if [ ! -f "$ROOT/logs/mac_watchdog.pid" ] || ! kill -0 "$(cat "$ROOT/logs/mac_watchdog.pid" 2>/dev/null)" 2>/dev/null; then
    nohup bash "$ROOT/deploy-mac/mac_watchdog.sh" >/dev/null 2>&1 &
    echo "mac_watchdog started"
  else
    echo "mac_watchdog already running (PID=$(cat "$ROOT/logs/mac_watchdog.pid"))"
  fi
fi
