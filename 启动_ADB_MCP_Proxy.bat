@echo off
setlocal
cd /d "%~dp0infra\adb-mcp\adb-proxy-socket"
echo ============================================================
echo ZERO STUDIO - Starting mikechambers/adb-mcp Proxy (Port 3001)
echo Open Photoshop UXP plugin panel and click Connect
echo ============================================================
node proxy.js
pause
