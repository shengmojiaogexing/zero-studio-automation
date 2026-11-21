<#
.SYNOPSIS
    ZERO STUDIO 一键停止脚本：安全关闭 start_studio.ps1 启动的全部本地服务与穿透进程。
#>

$RootDir = $PSScriptRoot
$PidFile = Join-Path $RootDir "logs\studio_pids.json"

if (-not (Test-Path $PidFile)) {
    Write-Host "未发现运行中的服务 PID 文件 ($PidFile)。" -ForegroundColor Yellow
    exit 0
}

$pids = Get-Content -Path $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($prop in $pids.PSObject.Properties) {
    $svcName = $prop.Name
    $procId = [int]$prop.Value
    try {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Write-Host "[已停止] $svcName (PID=$procId)" -ForegroundColor Green
        } else {
            Write-Host "[已退出] $svcName (PID=$procId)" -ForegroundColor DarkGray
        }
    } catch {
        Write-Warning "停止 $svcName (PID=$procId) 失败: $_"
    }
}

Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue

# 彻底清理可能残留的 frpc 与占用 3000 / 8090 / 3080 端口的孤儿服务进程
Get-Process | Where-Object { $_.ProcessName -like "frpc*" } | ForEach-Object {
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

foreach ($port in @(3000, 8090, 3080)) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            if ($c.OwningProcess -and $c.OwningProcess -gt 4) {
                Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
                Write-Host "[已清理端口残留] Port $port (PID=$($c.OwningProcess))" -ForegroundColor DarkYellow
            }
        }
    } catch {}
}

Write-Host "ZERO STUDIO 全部服务已停止。" -ForegroundColor Cyan
