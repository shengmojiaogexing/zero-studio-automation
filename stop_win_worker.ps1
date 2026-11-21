$RootDir = $PSScriptRoot
$PidFile = Join-Path $RootDir "logs\win_worker.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "未发现运行中的 Windows 随身修图机。" -ForegroundColor Yellow
    exit 0
}

$pidVal = (Get-Content -Path $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
if ($pidVal) {
    $proc = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id ([int]$pidVal) -Force -ErrorAction SilentlyContinue
        Write-Host "[已停止] Windows 随身修图机 (PID=$pidVal)" -ForegroundColor Green
    } else {
        Write-Host "[已退出] Windows 随身修图机 (PID=$pidVal)" -ForegroundColor DarkGray
    }
}
Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
