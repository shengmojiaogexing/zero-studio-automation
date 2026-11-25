@echo off
setlocal
echo ====================================================================
echo ZERO STUDIO - One-Click OpenClaw WeChat Official QR Login Setup
echo ====================================================================
echo [Step 1/5] Checking Node.js (requires Node.js >= 22)...
node -v
if %errorlevel% neq 0 (
    echo [ERROR] Node.js not found. Please install Node.js 22+ first.
    pause
    exit /b 1
)

echo.
echo [Step 2/5] Checking openclaw CLI...
where openclaw >nul 2>&1
if %errorlevel% neq 0 (
    echo openclaw not found. Installing globally via npm install -g openclaw ...
    call npm install -g openclaw
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install openclaw. Check npm network or proxy.
        pause
        exit /b 1
    )
) else (
    echo [OK] openclaw is already installed.
)

echo.
echo [Step 3/5] Installing @tencent-weixin/openclaw-weixin-cli ...
call npx -y @tencent-weixin/openclaw-weixin-cli install
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install openclaw-weixin plugin.
    pause
    exit /b 1
)

echo.
echo [Step 4/5] Enabling openclaw-weixin plugin ...
call openclaw config set plugins.entries.openclaw-weixin.enabled true

echo.
echo [Step 5/5] Starting QR-code login (scan with your WeChat)...
call openclaw channels login --channel openclaw-weixin

echo.
echo Restarting OpenClaw gateway...
call openclaw gateway restart
echo.
echo Done! Set OPENCLAW_CLI_ENABLED=true or OPENCLAW_NOTIFY_WEBHOOK_URL in .env
pause
