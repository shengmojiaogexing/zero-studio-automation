@echo off
setlocal
echo ====================================================================
echo ZERO STUDIO - One-Click OpenClaw WeChat Official QR Login Setup
echo ====================================================================
echo [Step 1/4] Checking Node.js version (requires Node.js >= 22)...
node -v
if %errorlevel% neq 0 (
    echo [ERROR] Node.js not found. Please install Node.js 22+ first.
    pause
    exit /b 1
)

echo.
echo [Step 2/4] Installing @tencent-weixin/openclaw-weixin-cli ...
call npx -y @tencent-weixin/openclaw-weixin-cli install

echo.
echo [Step 3/4] Enabling openclaw-weixin plugin ...
call openclaw config set plugins.entries.openclaw-weixin.enabled true

echo.
echo [Step 4/4] Starting QR-code login (scan with your WeChat)...
call openclaw channels login --channel openclaw-weixin

echo.
echo Restarting OpenClaw gateway...
call openclaw gateway restart
echo.
echo Done! Set OPENCLAW_CLI_ENABLED=true or OPENCLAW_NOTIFY_WEBHOOK_URL in .env
pause
