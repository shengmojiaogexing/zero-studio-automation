#!/bin/bash
# ==============================================================================
# ZERO STUDIO · Apple Silicon (M1/M2/M3/M4) Mac 监控机一键启动脚本
# 功能：原生启动 预约网站(:3000) + 中控台(:3080) + IM网关(:8090) + FRP穿透 + 防休眠
# ==============================================================================
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.lmstudio/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs infra/tunnel/bin

echo "============================================================"
echo "  ZERO STUDIO · M1 Mac 监控机一键启动"
echo "============================================================"

# 1. 加载 .env 环境变量
if [ ! -f "$ROOT_DIR/.env" ]; then
  echo "[错误] 未找到 $ROOT_DIR/.env，请先从 Windows 电脑把 .env 一起拷过来！"
  exit 1
fi
set -a
source "$ROOT_DIR/.env"
set +a

# 2. 检查 Node.js 与 Python3
if ! command -v node >/dev/null 2>&1; then
  echo "[错误] 未检测到 Node.js，请先运行: brew install node"
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未检测到 python3，请先运行: brew install python@3.11"
  exit 1
fi

# 3. 安装 booking-site 依赖（首次运行）
if [ ! -d "$ROOT_DIR/apps/booking-site/node_modules" ]; then
  echo "[1/5] 首次安装 booking-site Node 依赖..."
  (cd "$ROOT_DIR/apps/booking-site" && npm install)
fi

# 4. 创建 Mac 专用轻量 Python 虚拟环境（优先 python3.11，自动跳过 Windows 专用的 wxauto4）
PY_SYS="$(command -v python3.11 || command -v python3.12 || command -v python3)"
VENV_DIR="$ROOT_DIR/.venv-mac"
if [ ! -d "$VENV_DIR" ]; then
  echo "[2/5] 首次创建 Mac Python 虚拟环境 ($PY_SYS) 并安装核心依赖..."
  "$PY_SYS" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip -q
  "$VENV_DIR/bin/pip" install -q \
    fastapi uvicorn httpx openai python-dotenv pydantic \
    websockets python-multipart qrcode pillow cryptography
fi
PY="$VENV_DIR/bin/python"

# 5. 检查 macOS ARM64 版 frpc
FRPC_BIN="$ROOT_DIR/infra/tunnel/bin/frpc_darwin_arm64"
if [ ! -x "$FRPC_BIN" ]; then
  if command -v frpc >/dev/null 2>&1; then
    FRPC_BIN="$(command -v frpc)"
  else
    echo "[3/5] 正在自动下载 frpc v0.61.1 (macOS M1 ARM64)..."
    TMP_TGZ="/tmp/frp_0.61.1_darwin_arm64.tar.gz"
    curl -fsSL "https://github.com/fatedier/frp/releases/download/v0.61.1/frp_0.61.1_darwin_arm64.tar.gz" -o "$TMP_TGZ"
    tar -xzf "$TMP_TGZ" -C /tmp
    cp /tmp/frp_0.61.1_darwin_arm64/frpc "$FRPC_BIN"
    chmod +x "$FRPC_BIN"
    rm -rf "$TMP_TGZ" /tmp/frp_0.61.1_darwin_arm64
  fi
fi

# 6. 停止旧进程（防止端口占用）
"$SCRIPT_DIR/stop_mac_monitor.sh" >/dev/null 2>&1 || true

# 7. 启动四大常驻服务
echo "[4/5] 启动 booking-site (:3000)..."
(cd "$ROOT_DIR/apps/booking-site" && nohup node server.js > "$ROOT_DIR/logs/mac_booking.log" 2>&1 & echo $! > "$ROOT_DIR/logs/mac_booking.pid")

sleep 1

# 如果 .env 中配置了远程 Win11 虚拟机的 IM_GATEWAY_URL（如 http://192.168.x.x:8090），则不在 Mac 重复起 im-gateway
if [[ "${IM_GATEWAY_URL:-http://127.0.0.1:8090}" == *"127.0.0.1"* ]] || [[ "${IM_GATEWAY_URL:-http://127.0.0.1:8090}" == *"localhost"* ]]; then
  echo "[4/5] 启动 im-gateway (:8090 · QQ NapCatQQ 通道)..."
  (cd "$ROOT_DIR/services/im-gateway" && nohup "$PY" -m uvicorn main:app --host 0.0.0.0 --port 8090 > "$ROOT_DIR/logs/mac_im_gateway.log" 2>&1 & echo $! > "$ROOT_DIR/logs/mac_im_gateway.pid")
else
  echo "[4/5] 检测到外部 IM_GATEWAY_URL=$IM_GATEWAY_URL（如 Win11 虚拟机微信节点），跳过本地 im-gateway。"
fi

echo "[4/5] 启动 control-center (:3080)..."
(cd "$ROOT_DIR/apps/control-center" && nohup "$PY" -m uvicorn main:app --host 0.0.0.0 --port 3080 > "$ROOT_DIR/logs/mac_control_center.log" 2>&1 & echo $! > "$ROOT_DIR/logs/mac_control_center.pid")

echo "[4/5] 启动 FRP 公网穿透 (156.238.228.39:8080 & :8081)..."
(cd "$ROOT_DIR" && nohup "$FRPC_BIN" -c "$ROOT_DIR/infra/tunnel/frpc.toml" > "$ROOT_DIR/logs/mac_frpc.log" 2>&1 & echo $! > "$ROOT_DIR/logs/mac_frpc.pid")

# 8. 开启 macOS 防休眠（合盖/息屏不掉线）
echo "[5/5] 启动 macOS 防休眠守护 (caffeinate)..."
nohup caffeinate -dims > /dev/null 2>&1 &
echo $! > "$ROOT_DIR/logs/mac_caffeinate.pid"

sleep 2
echo ""
echo "============================================================"
echo "  ✅ M1 Mac 监控机已全部启动！"
echo "  • 本机中控台：http://127.0.0.1:3080"
echo "  • 本机预约页：http://127.0.0.1:3000"
echo "  • 公网预约页：http://156.238.228.39:8080"
echo "  • 公网中控台：http://156.238.228.39:8081"
echo "  • 停止服务请运行：bash deploy-mac/stop_mac_monitor.sh"
echo "============================================================"
