@echo off
setlocal
cd /d "%~dp0"
python pipelines\retouch-engine\test_ps_interactive.py
if %errorlevel% neq 0 pause
