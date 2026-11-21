#!/bin/bash
# ==============================================================================
# ZERO STUDIO · M1 Mac (ARM64) 一键启动 NapCatQQ Docker 容器
# 前置：Mac 已安装 Docker Desktop 或 OrbStack（推荐 OrbStack，超省内存）
# ==============================================================================
set -e

if ! command -v docker >/dev/null 2>&1; then
  echo "[错误] 未检测到 docker，建议在 M1 Mac 上安装 OrbStack (https://orbstack.dev) —— 比 Docker Desktop 省 70% 内存！"
  exit 1
fi

echo "正在启动 NapCatQQ (ARM64)..."
docker rm -f zero-napcat >/dev/null 2>&1 || true

docker run -d \
  --name zero-napcat \
  --restart always \
  -p 3002:3002 \
  -p 6100:6099 \
  -e NAPCAT_UID=0 \
  -e NAPCAT_GID=0 \
  -v "$HOME/zero-studio-napcat/config:/app/napcat/config" \
  -v "$HOME/zero-studio-napcat/qq:/app/.config/QQ" \
  mlikiowa/napcat-docker:latest

echo ""
echo "✅ NapCatQQ 已启动！"
echo "1. 浏览器打开 WebUI 扫码登录 QQ：http://127.0.0.1:6100/webui"
echo "   （初始密码在日志中查看：docker logs zero-napcat | grep token）"
echo "2. 在 NapCatQQ WebUI → 网络配置中启用："
echo "   • HTTP Server：端口 3002"
echo "   • WebSocket Client：ws://host.docker.internal:8090/ws/napcat"
echo "   • HTTP Client：http://host.docker.internal:8090/webhook/napcat"
