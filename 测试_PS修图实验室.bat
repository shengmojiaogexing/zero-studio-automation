@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
echo ====================================================================
echo ZERO STUDIO - AI Retouch & Selectable Skill Test Lab
echo Loading configs\retouch_skills\*.md ...
echo ====================================================================
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.10+ not found in PATH.
    pause
    exit /b 1
)
python pipelines\retouch-engine\test_ps_interactive.py
if %errorlevel% neq 0 pause
