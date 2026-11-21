@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0deploy-mac\sync_and_deploy_mac.ps1"
pause
