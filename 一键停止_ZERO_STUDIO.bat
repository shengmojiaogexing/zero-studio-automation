@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  ZERO STUDIO 一键停止全部服务
echo ==========================================================
powershell -ExecutionPolicy Bypass -File "%~dp0stop_studio.ps1"
timeout /t 3 >nul
