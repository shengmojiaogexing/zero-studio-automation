@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  ZERO STUDIO - NapCatQQ Launcher
echo  WebUI: http://127.0.0.1:6099/webui
echo ==========================================================
python "%~dp0infra\napcat\run_napcat.py"
pause
