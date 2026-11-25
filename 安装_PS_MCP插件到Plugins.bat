@echo off
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges to install into Photoshop Plug-ins...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "SRC_MCP=%~dp0infra\adb-mcp\uxp\ps"
set "SRC_FUSED=%~dp0work\ZERO-STUDIO-PS-Plugin"

for %%V in (2024 2025 2026) do (
    if exist "C:\Program Files\Adobe\Adobe Photoshop %%V\Plug-ins" (
        if exist "%SRC_FUSED%\manifest.json" (
            echo Installing ZERO-STUDIO-AI-MCP (Fused Chinese Plugin) into Photoshop %%V...
            robocopy "%SRC_FUSED%" "C:\Program Files\Adobe\Adobe Photoshop %%V\Plug-ins\ZERO-STUDIO-AI-MCP" /MIR /NFL /NDL /NJH /NJS >nul
            echo [OK] Installed: C:\Program Files\Adobe\Adobe Photoshop %%V\Plug-ins\ZERO-STUDIO-AI-MCP
        )
        if exist "%SRC_MCP%\manifest.json" (
            echo Installing Photoshop-MCP-Agent (Standalone Chinese MCP) into Photoshop %%V...
            robocopy "%SRC_MCP%" "C:\Program Files\Adobe\Adobe Photoshop %%V\Plug-ins\Photoshop-MCP-Agent" /MIR /NFL /NDL /NJH /NJS >nul
            echo [OK] Installed: C:\Program Files\Adobe\Adobe Photoshop %%V\Plug-ins\Photoshop-MCP-Agent
        )
    )
)

echo.
echo Done! Please restart Photoshop and open:
echo   Plugins -^> ZERO STUDIO AI MCP  (Fused all-in-one Chinese plugin)
echo   or Plugins -^> ZERO STUDIO PS MCP Agent (Lightweight Chinese MCP)
pause
