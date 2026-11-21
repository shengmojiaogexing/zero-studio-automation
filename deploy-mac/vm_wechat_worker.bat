@echo off
chcp 65001 >nul
title ZERO STUDIO - WeChat VM Worker Node (Win11 ARM)
setlocal enabledelayedexpansion

echo ============================================================
echo   ZERO STUDIO 微信监听节点（Win11 ARM 虚拟机）
echo   自动连接 M1 Mac 上的 预约系统 / 数据库 / LM Studio
echo ============================================================
echo.

REM ---------- M1 Mac 宿主机 ----------
REM UTM 虚拟机的网关地址就是 Mac 侧网卡（192.168.64.1），
REM 虚拟机访问不到 Mac 的局域网 IP（192.168.2.148），必须用这个。
set "MAC_HOST=192.168.64.1"
set "BOOKING_API_URL=http://%MAC_HOST%:3000"
REM INTERNAL_API_KEY 从根目录 .env 或当前环境变量读取，禁止硬编码。
set "IM_GATEWAY_PORT=8090"
set "WECHAT_MODE=desktop"

REM ---------- 本地小模型守门员（0 成本拦截闲聊） ----------
set "LOCAL_LLM_ENABLED=true"
set "LOCAL_LLM_BASE_URL=http://%MAC_HOST%:1234/v1"
set "LOCAL_LLM_MODEL=auto"

REM ---------- 代码目录 ----------
set "APP_DIR=%~dp0..\services\im-gateway"
if not exist "%APP_DIR%\main.py" (
    echo [错误] 未找到 %APP_DIR%\main.py
    echo 请确认已把整个 zero-studio-automation 文件夹复制到本虚拟机中。
    pause
    exit /b 1
)
cd /d "%APP_DIR%"

REM ---------- 1. Python ----------
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python。请先安装 Python 3.11 并勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)
echo [1/4] Python 版本：
python --version

REM ---------- 2. 依赖 ----------
echo [2/4] 检查依赖（首次会自动用清华源安装）...
python -c "import wxauto4, fastapi, uvicorn, httpx, openai, cryptography, dotenv" >nul 2>&1
if errorlevel 1 (
    echo     首次运行，正在安装 wxauto4 / fastapi 等依赖，请稍候...
    python -m pip install --upgrade pip -q
    python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ^
        wxauto4 fastapi uvicorn httpx openai python-dotenv pydantic websockets python-multipart cryptography
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络。
        pause
        exit /b 1
    )
)

REM ---------- 3. 检查微信是否登录 ----------
echo [3/4] 检查 PC 微信是否已登录...
tasklist /FI "IMAGENAME eq Weixin.exe" 2>nul | find /I "Weixin.exe" >nul
if errorlevel 1 (
    tasklist /FI "IMAGENAME eq WeChat.exe" 2>nul | find /I "WeChat.exe" >nul
    if errorlevel 1 (
        echo     [提示] 未检测到微信进程，请先登录 PC 微信并保持主窗口打开。
    ) else (
        echo     检测到微信已运行（WeChat.exe）
    )
) else (
    echo     检测到微信已运行（Weixin.exe）
)

REM ---------- 4. 连通性自检 ----------
echo [4/4] 检测与 M1 Mac 的连通性...
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://%MAC_HOST%:3000/api/settings' -TimeoutSec 6 -UseBasicParsing; Write-Host ('    Mac 预约系统 OK (HTTP ' + $r.StatusCode + ')') } catch { Write-Host '    [警告] 连不上 Mac 的 3000 端口，请检查是否同一局域网' }"
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://%MAC_HOST%:1234/v1/models' -TimeoutSec 6 -UseBasicParsing; Write-Host '    LM Studio 本地模型 OK' } catch { Write-Host '    [提示] LM Studio 未连通，将自动降级到云端模型' }"

echo.
echo ============================================================
echo   正在启动微信监听网关 http://0.0.0.0:8090
echo   启动后请在 Mac 中控台「智能客服与拓客」里开启微信监控
echo ============================================================
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8090
echo.
echo 网关已退出。按任意键关闭窗口。
pause
