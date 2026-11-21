$ErrorActionPreference = "Stop"
$RootDir = $PSScriptRoot
$LogsDir = Join-Path $RootDir "logs"
$PidFile = Join-Path $LogsDir "win_worker.pid"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

# 清理旧的随身修图机进程
if (Test-Path $PidFile) {
    $oldPid = (Get-Content -Path $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if ($oldPid) {
        $oldProc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($oldProc) {
            Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
            Write-Host "[已停止旧进程] win-retouch-worker (PID=$oldPid)" -ForegroundColor DarkYellow
        }
    }
}

$OutLog = Join-Path $LogsDir "win-retouch-worker.stdout.log"
$ErrLog = Join-Path $LogsDir "win-retouch-worker.stderr.log"
$WorkerScript = Join-Path $RootDir "pipelines\retouch-engine\worker.py"

$proc = Start-Process -FilePath "python" `
    -ArgumentList @($WorkerScript, "--interval", "10") `
    -WorkingDirectory $RootDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru

$proc.Id | Set-Content -Path $PidFile -Encoding ASCII
Start-Sleep -Seconds 3

if (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) {
    Write-Host "==========================================================" -ForegroundColor Green
    Write-Host " [OK] Windows 随身修图机已启动 (PID=$($proc.Id))" -ForegroundColor Green
    Write-Host " • 模式: 公网直连 Mac 主站 + 本机 Photoshop COM 零干预启停" -ForegroundColor Cyan
    Write-Host " • 日志: logs\win-retouch-worker.stderr.log" -ForegroundColor White
    Write-Host " • 随时停止请双击: 停止_Windows随身修图机.bat" -ForegroundColor DarkGray
    Write-Host "==========================================================" -ForegroundColor Green
    if (Test-Path $ErrLog) {
        Get-Content -Path $ErrLog -Tail 10 -Encoding UTF8 -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "[ERROR] Windows 随身修图机启动失败，请检查日志:" -ForegroundColor Red
    if (Test-Path $ErrLog) {
        Get-Content -Path $ErrLog -Tail 20 -Encoding UTF8 -ErrorAction SilentlyContinue
    }
    exit 1
}
