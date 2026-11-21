<#
.SYNOPSIS
    ZERO STUDIO 一键启动脚本：通过 studio_supervisor.py 统一管理 booking-site、im-gateway、control-center 及 frpc 穿透隧道。
#>

$ErrorActionPreference = "Stop"
$RootDir = $PSScriptRoot
$LogsDir = Join-Path $RootDir "logs"
$PidFile = Join-Path $LogsDir "studio_pids.json"
$EnvFile = Join-Path $RootDir ".env"

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

# 1. 加载根目录 .env 到当前进程环境
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content -Path $EnvFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -gt 0) {
            $k = $trimmed.Substring(0, $idx).Trim()
            $v = $trimmed.Substring($idx + 1).Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($k, $v, "Process")
        }
    }
}

$BookingPort = if ($env:PORT) { $env:PORT } else { "3000" }
$GatewayPort = if ($env:IM_GATEWAY_PORT) { $env:IM_GATEWAY_PORT } else { "8090" }
$ControlPort = if ($env:CONTROL_CENTER_PORT) { $env:CONTROL_CENTER_PORT } else { "3080" }

function Wait-HttpHealthy([string]$Name, [string]$Url, [int]$TimeoutSec = 15) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $req = [System.Net.HttpWebRequest]::Create($Url)
            $req.Method = "GET"
            $req.Timeout = 2000
            $req.Proxy = $null
            $resp = $req.GetResponse()
            $code = [int]$resp.StatusCode
            $resp.Close()
            if ($code -ge 200 -and $code -lt 500) {
                Write-Host "  [OK] $Name 已就绪 ($Url)" -ForegroundColor Green
                return $true
            }
        } catch {
            Start-Sleep -Milliseconds 400
        }
    }
    Write-Warning "  [WARN] $Name 检查超时 ($Url)"
    return $false
}

# 2. 如果守护进程未运行，则启动 supervisor
$supervisorRunning = $false
if (Test-Path $PidFile) {
    try {
        $pids = Get-Content -Path $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($pids.supervisor) {
            $sproc = Get-Process -Id ([int]$pids.supervisor) -ErrorAction SilentlyContinue
            if ($sproc) { $supervisorRunning = $true }
        }
    } catch {}
}

if (-not $supervisorRunning) {
    Write-Host "[启动] Zero Studio 全栈常驻守护进程..." -ForegroundColor Cyan
    Start-Process -FilePath "python" -ArgumentList @("studio_supervisor.py") -WorkingDirectory $RootDir -WindowStyle Hidden
    Start-Sleep -Seconds 3
} else {
    Write-Host "[运行中] Zero Studio 守护进程已处于运行状态" -ForegroundColor DarkCyan
}

# 3. 核心接口就绪检查
Wait-HttpHealthy "booking-site" "http://127.0.0.1:$BookingPort/api/settings" 15 | Out-Null
Wait-HttpHealthy "im-gateway" "http://127.0.0.1:$GatewayPort/health" 15 | Out-Null
Wait-HttpHealthy "control-center" "http://127.0.0.1:$ControlPort/api/auth/status" 15 | Out-Null

$LocalControlUrl = "http://127.0.0.1:$ControlPort"
Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " ZERO STUDIO 全链路服务已就绪！" -ForegroundColor Green
Write-Host " • 本地统一中控台: $LocalControlUrl" -ForegroundColor White
Write-Host " • 后期自动修图引擎: retouch-worker (XMP + 零干预PS自动启停)" -ForegroundColor Cyan
Write-Host " • 插卡自动拷卡分类: card-watcher (SD卡热插拔 -> 入库 -> 推客)" -ForegroundColor Cyan
if ($env:PUBLIC_BASE_URL) {
    Write-Host " • 客户外网选图端: $($env:PUBLIC_BASE_URL)" -ForegroundColor White
}
if ($env:VPS_HOST -and $env:FRP_PUBLIC_STUDIO_PORT) {
    Write-Host " • 手机外网中控台: http://$($env:VPS_HOST):$($env:FRP_PUBLIC_STUDIO_PORT)" -ForegroundColor White
}
Write-Host " • 停止全部服务请双击: 一键停止_ZERO_STUDIO.bat" -ForegroundColor DarkGray
Write-Host "==========================================================" -ForegroundColor Green

Start-Process $LocalControlUrl
