#!/bin/bash
# 重启 Mac 上的 im-gateway（加载 .env 后启动）
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
ROOT="$HOME/zero-studio-automation"
cd "$ROOT" || exit 1

set -a
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|\#*) continue ;; esac
  key="$(printf '%s' "${line%%=*}" | tr -d '[:space:]')"
  val="$(printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  export "$key=$val"
done < "$ROOT/.env"
set +a

echo "=== stop old im-gateway ==="
for pid in $(pgrep -f "uvicorn main:app --host 0.0.0.0 --port 8090"); do
  kill "$pid" 2>/dev/null; echo "killed $pid"
done
sleep 2

echo "=== run new unit tests on Mac ==="
cd "$ROOT/services/im-gateway" && "$ROOT/.venv-mac/bin/python" test_private_intake.py 2>&1 | tail -8

echo "=== start im-gateway ==="
cd "$ROOT/services/im-gateway" || exit 1
nohup "$ROOT/.venv-mac/bin/python" -m uvicorn main:app --host 0.0.0.0 --port 8090 \
  > "$ROOT/logs/mac_im_gateway.log" 2>&1 &
echo $! > "$ROOT/logs/mac_im_gateway.pid"
sleep 5

echo "=== health ==="
curl -s -o /dev/null -w "im_gateway:8090=%{http_code}\n" http://127.0.0.1:8090/health
curl -s http://127.0.0.1:8090/health
echo
