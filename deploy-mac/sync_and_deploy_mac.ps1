param(
    [string]$MacHost = "192.168.2.148",
    [string]$MacUser = "zero",
    [string]$RemoteRoot = "~/zero-studio-automation"
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
$Target = "$MacUser@$MacHost"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " ZERO STUDIO · 一键同步更新到 Mac 并开启爆满自动重启监控" -ForegroundColor Cyan
Write-Host " 目标 Mac: $Target ($RemoteRoot)" -ForegroundColor White
Write-Host "==========================================================" -ForegroundColor Cyan

# 复用单条 SSH ControlMaster 连接或直接打包 tar 经由一次 ssh 传输，只需输一次 Mac 密码！
$Files = @(
    "apps/booking-site/server.js",
    "apps/booking-site/review.html",
    "apps/control-center/main.py",
    "apps/control-center/static/index.html",
    "apps/control-center/static/mobile.html",
    "apps/control-center/static/login.html",
    "services/im-gateway/brain.py",
    "services/im-gateway/main.py",
    "services/im-gateway/conversation_store.py",
    "services/im-gateway/test_private_intake.py",
    "deploy-mac/restart_mac_services.sh",
    "deploy-mac/mac_watchdog.sh",
    "deploy-mac/install_mac_launchd.sh"
)

$TmpTar = Join-Path $env:TEMP "zs_mac_sync_$(Get-Date -Format 'yyyyMMdd_HHmmss').tar.gz"
Push-Location $RootDir
try {
    & tar -czf $TmpTar @Files
    if ($LASTEXITCODE -ne 0) { throw "打包同步文件失败" }

    Write-Host "[1/2] 正在上传更新包到 Mac..." -ForegroundColor Yellow
    $KeyFile = Join-Path $env:USERPROFILE ".ssh\zs_vps_ed25519"
    $SshArgs = @("-o", "StrictHostKeyChecking=no")
    if (Test-Path $KeyFile) { $SshArgs += @("-i", $KeyFile) }
    $RemoteTar = "/tmp/zs_mac_sync.tar.gz"
    & scp @SshArgs $TmpTar "${Target}:${RemoteTar}"
    if ($LASTEXITCODE -ne 0) { throw "SCP 上传更新包失败" }

    Write-Host "[2/2] 正在 Mac 上解压、重启服务并开启自动监控..." -ForegroundColor Yellow
    $RemoteCmd = "mkdir -p $RemoteRoot && tar -xzf $RemoteTar -C $RemoteRoot && rm -f $RemoteTar && sed -i '' 's/\r$//' $RemoteRoot/deploy-mac/*.sh && chmod +x $RemoteRoot/deploy-mac/*.sh && bash $RemoteRoot/deploy-mac/restart_mac_services.sh && bash $RemoteRoot/deploy-mac/install_mac_launchd.sh && if [ -f $RemoteRoot/deploy-mac/restart_im_gateway.sh ]; then bash $RemoteRoot/deploy-mac/restart_im_gateway.sh; fi"
    & ssh @SshArgs $Target $RemoteCmd
    if ($LASTEXITCODE -ne 0) { throw "SSH 解压或重启失败" }

    Write-Host ""
    Write-Host "✅ Mac 主站同步、服务重启与后台清理/爆满自动重启监控已全部部署完成！" -ForegroundColor Green
} finally {
    Pop-Location
    Remove-Item -Path $TmpTar -Force -ErrorAction SilentlyContinue
}
