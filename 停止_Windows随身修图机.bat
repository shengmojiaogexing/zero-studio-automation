@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0stop_win_worker.ps1"
timeout /t 3 >nul
