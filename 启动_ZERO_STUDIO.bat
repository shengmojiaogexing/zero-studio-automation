@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  ZERO STUDIO 一键启动（含插卡自动分类 + AI零干预PS修图）
echo ==========================================================
echo [1/2] 正在清理旧后台进程...
powershell -ExecutionPolicy Bypass -File "%~dp0stop_studio.ps1"
echo [2/2] 正在启动全链路服务并等待就绪...
powershell -ExecutionPolicy Bypass -File "%~dp0start_studio.ps1"
echo.
echo 服务已在后台常驻运行，可安全关闭此窗口。
timeout /t 4 >nul
