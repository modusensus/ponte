# SSH Tunnel Setup — One-click deploy
# Run as Administrator for Scheduled Task registration
# Usage: powershell -ExecutionPolicy Bypass -File setup.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = "C:\ssh-tunnel"

Write-Host "═══════════════════════════════════════════"
Write-Host "  SSH Tunnel Setup v2.0"
Write-Host "═══════════════════════════════════════════"
Write-Host ""

# 1. Copy SSH keys from user profile
Write-Host "[1/4] Copying SSH keys..."
$srcDir = "$env:USERPROFILE\.ssh"
if (Test-Path "$srcDir\id_rsa") {
    Copy-Item -Path "$srcDir\id_rsa" -Destination "$ScriptDir\id_rsa" -Force
    Write-Host "  id_rsa copied"
} else {
    Write-Host "  WARNING: $srcDir\id_rsa not found — using existing key in $ScriptDir"
}
Copy-Item -Path "$srcDir\id_rsa.pub" -Destination "$ScriptDir\id_rsa.pub" -Force -ErrorAction SilentlyContinue

# 2. Regenerate .pub from private key (ensures match)
Write-Host "[2/4] Regenerating public key..."
$pubKey = ssh-keygen -y -f "$ScriptDir\id_rsa" 2>$null
if ($pubKey) {
    $pubKey | Out-File -FilePath "$ScriptDir\id_rsa.pub" -Encoding ascii -NoNewline
    Write-Host "  Public key regenerated"
} else {
    Write-Host "  WARNING: Could not regenerate public key"
}

# 3. Verify SSH connection
Write-Host "[3/4] Testing SSH connection..."
$testResult = & "D:\Git\usr\bin\ssh.exe" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$ScriptDir\known_hosts" -o ConnectTimeout=10 -i "$ScriptDir\id_rsa" YOUR_USER@YOUR_SERVER_IP "echo OK" 2>&1
if ($testResult -match "OK") {
    Write-Host "  SSH connection: OK"
} else {
    Write-Host "  ERROR: SSH connection failed: $testResult"
    Write-Host "  Check: key copied to server? Firewall? Network?"
    Write-Host "  Run: type C:\ssh-tunnel\id_rsa.pub | ssh YOUR_USER@YOUR_SERVER_IP 'cat >> ~/.ssh/authorized_keys'"
    exit 1
}

# 4. Install CLI and Scheduled Task
Write-Host "[4/4] Installing tunnel service..."
powershell -ExecutionPolicy Bypass -File "$ScriptDir\tunnel.ps1" install

Write-Host ""
Write-Host "═══════════════════════════════════════════"
Write-Host "  Setup complete!"
Write-Host ""
Write-Host "  CLI commands:"
Write-Host "    powershell -File C:\ssh-tunnel\tunnel.ps1 status"
Write-Host "    powershell -File C:\ssh-tunnel\tunnel.ps1 restart"
Write-Host "    powershell -File C:\ssh-tunnel\tunnel.ps1 log"
Write-Host "    powershell -File C:\ssh-tunnel\tunnel.ps1 stop"
Write-Host "    powershell -File C:\ssh-tunnel\tunnel.ps1 uninstall"
Write-Host "═══════════════════════════════════════════"