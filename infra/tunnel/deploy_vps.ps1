<#
.SYNOPSIS
    ZERO STUDIO 公网 VPS FRP 服务端一键自动化部署脚本 (frps + systemd + 防火墙放行)
.DESCRIPTION
    从根目录 .env 读取 VPS_HOST、VPS_SSH_PORT、VPS_SSH_USER、FRP_BIND_PORT、FRP_TOKEN 等配置，
    通过 OpenSSH 在远端 VPS 自动下载安装 frps、写入 /etc/frp/frps.toml、注册 systemd 常驻服务并放行端口。
    安全原则：绝不在脚本或任何文件中硬编码 SSH 密码！请使用 SSH 密钥，或交互式输入密码。
#>

param(
    [string]$FrpVersion = "0.61.1",
    [string]$EnvFile = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) ".env")
)

$ErrorActionPreference = "Stop"

function Load-DotEnv([string]$Path) {
    $map = @{}
    if (-not (Test-Path $Path)) {
        Write-Warning "未找到 .env 文件 ($Path)，将使用当前环境变量或默认值。"
        return $map
    }
    foreach ($line in Get-Content -Path $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -gt 0) {
            $k = $trimmed.Substring(0, $idx).Trim()
            $v = $trimmed.Substring($idx + 1).Trim().Trim('"').Trim("'")
            $map[$k] = $v
        }
    }
    return $map
}

function Get-ConfigValue($map, [string]$key, [string]$defaultVal = "") {
    $envVal = [Environment]::GetEnvironmentVariable($key)
    if ($envVal) { return $envVal }
    if ($map.ContainsKey($key) -and $map[$key]) { return $map[$key] }
    return $defaultVal
}

$envMap = Load-DotEnv $EnvFile
$VpsHost = Get-ConfigValue $envMap "VPS_HOST" ""
$VpsPort = Get-ConfigValue $envMap "VPS_SSH_PORT" "22"
$VpsUser = Get-ConfigValue $envMap "VPS_SSH_USER" "root"
$FrpBindPort = Get-ConfigValue $envMap "FRP_BIND_PORT" "7000"
$FrpToken = Get-ConfigValue $envMap "FRP_TOKEN" ""
$BookingPort = Get-ConfigValue $envMap "FRP_PUBLIC_BOOKING_PORT" "8080"
$StudioPort = Get-ConfigValue $envMap "FRP_PUBLIC_STUDIO_PORT" "8081"

if (-not $FrpToken -or $FrpToken -like "change-me*") {
    $secToken = Read-Host -Prompt "请输入用于 FRP 鉴权的强密钥 FRP_TOKEN（不会明文回显）" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secToken)
    try {
        $FrpToken = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if (-not $FrpToken) {
    throw "FRP_TOKEN 不能为空！"
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " ZERO STUDIO VPS FRPS 自动化部署" -ForegroundColor Cyan
Write-Host " 目标节点: $VpsUser@${VpsHost}:$VpsPort" -ForegroundColor White
Write-Host " FRP 端口: $FrpBindPort (TCP+UDP) | 选图: $BookingPort | 中控台: $StudioPort" -ForegroundColor White
Write-Host "==========================================================" -ForegroundColor Cyan

# 在远端执行的标准化 bash 部署脚本（通过标准输入传递，避免命令行泄露 Token）
$remoteScript = @"
set -euo pipefail
FRP_VERSION="$FrpVersion"
ARCH=\$(uname -m)
case "\$ARCH" in
  x86_64|amd64) FRP_ARCH="amd64" ;;
  aarch64|arm64) FRP_ARCH="arm64" ;;
  *) echo "Unsupported arch: \$ARCH"; exit 1 ;;
esac

echo "[1/4] 下载并安装 frps v\${FRP_VERSION} (\${FRP_ARCH})..."
TMP_DIR=\$(mktemp -d)
trap 'rm -rf "\$TMP_DIR"' EXIT
PKG="frp_\${FRP_VERSION}_linux_\${FRP_ARCH}"
URL="https://github.com/fatedier/frp/releases/download/v\${FRP_VERSION}/\${PKG}.tar.gz"
curl -fL "\$URL" -o "\$TMP_DIR/frp.tar.gz"
tar -xzf "\$TMP_DIR/frp.tar.gz" -C "\$TMP_DIR"
install -m 755 "\$TMP_DIR/\${PKG}/frps" /usr/local/bin/frps

echo "[2/4] 写入 /etc/frp/frps.toml..."
mkdir -p /etc/frp
cat > /etc/frp/frps.toml << 'EOF_TOML'
bindAddr = "0.0.0.0"
bindPort = $FrpBindPort
quicBindPort = $FrpBindPort

[auth]
method = "token"
token = "$FrpToken"

[webServer]
addr = "127.0.0.1"
port = 7500
user = "admin"
password = "$FrpToken"

[log]
to = "/var/log/frps.log"
level = "info"
maxDays = 7
EOF_TOML
chmod 600 /etc/frp/frps.toml

echo "[3/4] 注册并启动 systemd 常驻服务 frps.service..."
cat > /etc/systemd/system/frps.service << 'EOF_SVC'
[Unit]
Description=Zero Studio FRP Server (frps)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=on-failure
RestartSec=5s
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF_SVC

systemctl daemon-reload
systemctl enable --now frps
systemctl restart frps
systemctl is-active --quiet frps

echo "[4/4] 配置系统防火墙放行端口 ($FrpBindPort/tcp+udp, $BookingPort/tcp, $StudioPort/tcp)..."
if command -v ufw >/dev/null 2>&1; then
  ufw allow ${FrpBindPort}/tcp || true
  ufw allow ${FrpBindPort}/udp || true
  ufw allow ${BookingPort}/tcp || true
  ufw allow ${StudioPort}/tcp || true
elif command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=${FrpBindPort}/tcp || true
  firewall-cmd --permanent --add-port=${FrpBindPort}/udp || true
  firewall-cmd --permanent --add-port=${BookingPort}/tcp || true
  firewall-cmd --permanent --add-port=${StudioPort}/tcp || true
  firewall-cmd --reload || true
fi

echo "✅ ZERO STUDIO frps 部署并启动完成！"
systemctl status frps --no-pager -l | head -n 12
"@

# 转换为 LF 换行并经 SSH 标准输入下发（SSH 密码由 OpenSSH 交互提示或密钥完成，绝不硬编码）
$lfScript = $remoteScript -replace "`r`n", "`n"
Write-Host "正在通过 SSH 连接 $VpsUser@${VpsHost}:$VpsPort（若未配置公钥，请在下方交互输入 SSH 密码）..." -ForegroundColor Yellow
$lfScript | ssh -p $VpsPort -o StrictHostKeyChecking=accept-new "$VpsUser@$VpsHost" "bash -s"
if ($LASTEXITCODE -ne 0) {
    throw "VPS 部署失败 (SSH ExitCode=$LASTEXITCODE)"
}
Write-Host "🎉 VPS 服务端 frps 已成功部署并常驻运行！" -ForegroundColor Green
