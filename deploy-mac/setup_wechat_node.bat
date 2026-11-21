@echo off
chcp 65001 >nul
title ZERO STUDIO - WeChat AI Node Setup
setlocal enabledelayedexpansion

echo ============================================================
echo   ZERO STUDIO . WeChat AI Node (Windows VM)
echo   Connects this Windows to the Mac-hosted ZERO STUDIO
echo ============================================================
echo.

REM ---- Mac host (UTM gateway address) ----
set "MAC_HOST=192.168.64.1"
set "BOOKING_API_URL=http://%MAC_HOST%:3000"
REM INTERNAL_API_KEY 从 im-gateway 目录的 .env 或当前环境变量读取，禁止硬编码。
set "LOCAL_LLM_BASE_URL=http://%MAC_HOST%:1234/v1"
set "LOCAL_LLM_MODEL=auto"
set "IM_GATEWAY_PORT=8090"
set "WECHAT_MODE=desktop"

echo [1/6] Checking Python...
where python >nul 2>&1
if errorlevel 1 (
    echo       [FAIL] Python not found.
    echo       Install Python 3.11 from https://www.python.org/downloads/
    echo       IMPORTANT: check "Add python.exe to PATH" during install.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo       %%v

echo.
echo [2/6] Checking WeChat process...
tasklist /FI "IMAGENAME eq Weixin.exe" 2>nul | find /I "Weixin.exe" >nul && (
    echo       WeChat 4.x running ^(Weixin.exe^)
    goto :wx_ok
)
tasklist /FI "IMAGENAME eq WeChat.exe" 2>nul | find /I "WeChat.exe" >nul && (
    echo       WeChat 3.x running ^(WeChat.exe^)
    goto :wx_ok
)
echo       [WARN] WeChat not running.
echo       Please open WeChat and log in first, then run this script again.
echo.
pause
exit /b 1

:wx_ok
echo.
echo [3/6] Installing Python dependencies (first run may take 2-3 min)...
python -c "import wxauto4" >nul 2>&1
if errorlevel 1 (
    echo       Installing wxauto4 / fastapi / uvicorn ...
    python -m pip install --upgrade pip -q
    python -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple ^
        wxauto4 fastapi uvicorn httpx openai python-dotenv pydantic ^
        websockets python-multipart cryptography pillow
    if errorlevel 1 (
        echo       [FAIL] pip install failed. Check network.
        pause
        exit /b 1
    )
) else (
    echo       wxauto4 already installed
)

echo.
echo [4/6] Testing connection to Mac...
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri '%BOOKING_API_URL%/api/settings' -TimeoutSec 8 -UseBasicParsing; Write-Host ('       Mac booking-site OK (HTTP ' + $r.StatusCode + ')') } catch { Write-Host '       [FAIL] Cannot reach Mac. Check VM network.'; exit 1 }"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%LOCAL_LLM_BASE_URL%/models' -TimeoutSec 5 -UseBasicParsing | Out-Null; Write-Host '       LM Studio OK' } catch { Write-Host '       [INFO] LM Studio not reachable, will fall back to cloud model' }"

echo.
echo [5/6] Preparing gateway files...
set "APP_DIR=%LOCALAPPDATA%\ZeroStudio\im-gateway"
if not exist "%APP_DIR%" mkdir "%APP_DIR%"
echo       App dir: %APP_DIR%

echo.
echo [6/6] Starting WeChat AI gateway...
echo.
echo ============================================================
echo   Gateway: http://0.0.0.0:%IM_GATEWAY_PORT%
echo   Mac API: %BOOKING_API_URL%
echo.
echo   Keep WeChat main window VISIBLE (not minimized).
echo   Press Ctrl+C to stop.
echo ============================================================
echo.

set "PYTHONPATH=%APP_DIR%"
python -m uvicorn main:app --host 0.0.0.0 --port %IM_GATEWAY_PORT% --app-dir "%APP_DIR%"
pause
