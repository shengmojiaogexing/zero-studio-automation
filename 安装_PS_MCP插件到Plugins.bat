@echo off
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges to install into Photoshop Plug-ins...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "SRC=%~dp0infra\adb-mcp\uxp\ps"
if not exist "%SRC%\manifest.json" (
    echo [ERROR] Source plugin folder not found: %SRC%
    pause
    exit /b 1
)

for %%V in (2024 2025 2026) do (
    if exist "C:\Program Files\Adobe\Adobe Photoshop %%V\Plug-ins" (
        echo Installing Photoshop-MCP-Agent into Photoshop %%V...
        robocopy "%SRC%" "C:\Program Files\Adobe\Adobe Photoshop %%V\Plug-ins\Photoshop-MCP-Agent" /MIR /NFL /NDL /NJH /NJS >nul
        echo [OK] Installed: C:\Program Files\Adobe\Adobe Photoshop %%V\Plug-ins\Photoshop-MCP-Agent
    )
)

echo.
echo Done! Please restart Photoshop and open:
echo   Plugins -^> Photoshop MCP Agent
echo (Auto-connect to http://localhost:3001 is enabled by default.)
pause
