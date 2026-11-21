@echo off
echo ==========================================================
echo  Block WeChat PC 4.1.8 Silent Auto-Update
echo ==========================================================
set "UPD1=%APPDATA%\Tencent\xwechat\update"
set "UPD2=%APPDATA%\Tencent\WeChat\Update"

if exist "%UPD1%" (
    attrib -r -s -h "%UPD1%" >nul 2>&1
    rmdir /s /q "%UPD1%" >nul 2>&1
    del /f /q "%UPD1%" >nul 2>&1
)
mkdir "%APPDATA%\Tencent\xwechat" >nul 2>&1
type nul > "%UPD1%"
attrib +r +s "%UPD1%"

if exist "%UPD2%" (
    attrib -r -s -h "%UPD2%" >nul 2>&1
    rmdir /s /q "%UPD2%" >nul 2>&1
    del /f /q "%UPD2%" >nul 2>&1
)
mkdir "%APPDATA%\Tencent\WeChat" >nul 2>&1
type nul > "%UPD2%"
attrib +r +s "%UPD2%"

echo [OK] WeChat auto-update folders have been locked as read-only!
echo WeChat 4.1.8.107 will NEVER silently upgrade back to 4.2.1.8.
pause
