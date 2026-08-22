# SSH Tunnel Manager v2.0
# CLI tool for managing persistent SSH reverse tunnel to cloud server
# Usage: powershell -ExecutionPolicy Bypass -File tunnel.ps1 <command>
# Commands: start, stop, status, restart, install, uninstall, log

param(
    [Parameter(Position=0)]
    [ValidateSet("start", "stop", "status", "restart", "install", "uninstall", "log")]
    [string]$Command = "status"
)

$ErrorActionPreference = "Stop"
$ScriptDir   = "C:\ssh-tunnel"
$TaskName    = "SSH-Reverse-Tunnel"
$LogFile     = "$ScriptDir\tunnel.log"
$PidFile     = "$ScriptDir\tunnel.pid"

$SshExe       = "D:\Git\usr\bin\ssh.exe"
$SshKey       = "$ScriptDir\id_rsa"
$SshKnownHosts = "$ScriptDir\known_hosts"
$SshServer    = "YOUR_USER@YOUR_SERVER_IP"
$SshOpts      = "-o", "StrictHostKeyChecking=accept-new",
                "-o", "UserKnownHostsFile=$SshKnownHosts",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "TCPKeepAlive=yes"
$SshForwards  = "-N", "-R", "23334:localhost:2222", "-R", "17897:localhost:7897"

# ═══════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $Message" | Out-File -Append -FilePath $LogFile -Encoding utf8
    Write-Host "$ts $Message"
}

function Get-TunnelPid {
    if (Test-Path $PidFile) {
        $storedPid = Get-Content $PidFile -Raw
        $storedPid = $storedPid.Trim()
        if ($storedPid -match '^\d+$') {
            try {
                $proc = Get-Process -Id ([int]$storedPid) -ErrorAction Stop
                if ($proc.ProcessName -eq "ssh") { return [int]$storedPid }
            } catch { }
        }
    }
    # Fallback: find ssh.exe process with our specific tunnel args
    $procs = Get-Process -Name "ssh" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)").CommandLine
            if ($cmd -match "23334.*2222.*17897.*7897") {
                $p.Id | Out-File -FilePath $PidFile -NoNewline
                return $p.Id
            }
        } catch { }
    }
    return $null
}

function Test-TunnelAlive {
    $pid = Get-TunnelPid
    if (-not $pid) { return $false }
    try {
        $result = & $SshExe -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$SshKnownHosts" -o ConnectTimeout=5 -i $SshKey $SshServer "ss -tlnp | grep -c ':23334 '" 2>$null
        return [int]$result -gt 0
    } catch {
        return $false
    }
}

# ═══════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════

function Start-Tunnel {
    $existingPid = Get-TunnelPid
    if ($existingPid) {
        Write-Log "Tunnel already running (PID: $existingPid)"
        return
    }

    Write-Log "Starting SSH tunnel..."

    $allArgs = $SshOpts + @("-i", $SshKey) + $SshForwards + @($SshServer)
    $proc = Start-Process -FilePath $SshExe -ArgumentList $allArgs -WindowStyle Hidden -PassThru
    $proc.Id | Out-File -FilePath $PidFile -NoNewline

    Start-Sleep -Seconds 3

    if (Test-TunnelAlive) {
        Write-Log "Tunnel started successfully (PID: $($proc.Id))"
    } else {
        Write-Log "Tunnel process started but health check may need a moment"
    }
}

function Stop-Tunnel {
    $pid = Get-TunnelPid
    if ($pid) {
        Write-Log "Stopping tunnel (PID: $pid)..."
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Write-Log "Tunnel stopped"
    } else {
        Write-Host "No tunnel process found"
    }
}

function Show-Status {
    Write-Host "═══════════════════════════════════════════"
    Write-Host "  SSH Tunnel Status"
    Write-Host "═══════════════════════════════════════════"
    Write-Host ""

    # Local process
    $pid = Get-TunnelPid
    if ($pid) {
        try {
            $proc = Get-Process -Id $pid -ErrorAction Stop
            $runtime = (Get-Date) - $proc.StartTime
            $uptime = $runtime.ToString('hh\:mm\:ss')
            Write-Host "  Local Process  : RUNNING (PID: $pid, uptime: $uptime)"
        } catch {
            Write-Host "  Local Process  : STALE (PID $pid not found)"
        }
    } else {
        Write-Host "  Local Process  : NOT RUNNING"
    }

    # Remote ports
    try {
        $output = & $SshExe -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$SshKnownHosts" -o ConnectTimeout=5 -i $SshKey $SshServer "ss -tlnp | grep -E '23334|17897'" 2>$null
        if ($output) {
            Write-Host "  Remote Ports   : ACTIVE"
            Write-Host "    $output"
        } else {
            Write-Host "  Remote Ports   : NOT LISTENING"
        }
    } catch {
        Write-Host "  Remote Check   : FAILED (cannot connect to server)"
    }

    # Scheduled Task
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $info = $task | Get-ScheduledTaskInfo
        Write-Host "  Scheduled Task : $($task.State.ToUpper()) (LastRun: $($info.LastRunTime), Result: $($info.LastTaskResult))"
    } catch {
        Write-Host "  Scheduled Task : NOT INSTALLED"
    }

    Write-Host ""
    Write-Host "  Log file: $LogFile"
    Write-Host "═══════════════════════════════════════════"
}

function Restart-Tunnel {
    Stop-Tunnel
    Start-Sleep -Seconds 2
    Start-Tunnel
}

function Write-WrapperScript {
    # Write the wrapper script as a separate file (avoids here-string escaping issues)
    $wrapperPath = "$ScriptDir\tunnel-wrapper.ps1"
    $lines = @(
        '# SSH Tunnel Auto-Restart Wrapper',
        '# Generated by tunnel.ps1 install',
        '',
        '$sshExe       = "D:\Git\usr\bin\ssh.exe"',
        '$sshKey       = "C:\ssh-tunnel\id_rsa"',
        '$sshKnownHosts = "C:\ssh-tunnel\known_hosts"',
        '$sshServer    = "YOUR_USER@YOUR_SERVER_IP"',
        '$logFile      = "C:\ssh-tunnel\tunnel.log"',
        '',
        '$sshOpts = @(',
        '    "-o", "StrictHostKeyChecking=accept-new",',
        '    "-o", "UserKnownHostsFile=$sshKnownHosts",',
        '    "-o", "ServerAliveInterval=30",',
        '    "-o", "ServerAliveCountMax=3",',
        '    "-o", "ExitOnForwardFailure=yes",',
        '    "-o", "TCPKeepAlive=yes"',
        ')',
        '',
        '$sshForwards = @("-N", "-R", "23334:localhost:2222", "-R", "17897:localhost:7897")',
        '',
        'while ($true) {',
        '    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"',
        '    "$ts Starting SSH tunnel..." | Out-File -Append -FilePath $logFile -Encoding utf8',
        '',
        '    try {',
        '        $allArgs = $sshOpts + @("-i", $sshKey) + $sshForwards + @($sshServer)',
        '        $proc = Start-Process -FilePath $sshExe -ArgumentList $allArgs -WindowStyle Hidden -PassThru -Wait',
        '        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"',
        '        "$ts SSH exited with code $($proc.ExitCode), restarting in 10s..." | Out-File -Append -FilePath $logFile -Encoding utf8',
        '    } catch {',
        '        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"',
        '        "$ts SSH failed: $_ - restarting in 10s..." | Out-File -Append -FilePath $logFile -Encoding utf8',
        '    }',
        '',
        '    Start-Sleep -Seconds 10',
        '}'
    )
    $lines | Out-File -FilePath $wrapperPath -Encoding utf8
    Write-Host "  Wrapper script: $wrapperPath"
    return $wrapperPath
}

function Install-Task {
    Write-Log "Installing Scheduled Task '$TaskName'..."

    $wrapperPath = Write-WrapperScript

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$wrapperPath`""

    $trigger = New-ScheduledTaskTrigger -AtLogon

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfOnBatteries `
        -DontStopOnIdleEnd `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 365) `
        -MultipleInstances IgnoreNew

    try {
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($existing) {
            Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings
            Write-Host "  Task updated"
        } else {
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest
            Write-Host "  Task created"
        }
    } catch {
        Write-Log "ERROR: Failed to register task: $_"
        Write-Host "  Try running as Administrator"
        return
    }

    # Start the task now
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    Write-Host "  Task started"

    # Verify
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "  LastTaskResult: $($info.LastTaskResult) (0 = OK)"
    Write-Log "Install complete. Tunnel will auto-start at logon and auto-restart on failure."
}

function Uninstall-Task {
    Write-Log "Removing Scheduled Task '$TaskName'..."
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "  Task removed"
    } catch {
        Write-Host "  Task not found or already removed"
    }
    Stop-Tunnel
    Write-Log "Uninstall complete"
}

function Show-Log {
    param([int]$Lines = 30)
    if (Test-Path $LogFile) {
        Get-Content $LogFile -Tail $Lines
    } else {
        Write-Host "No log file found"
    }
}

# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

switch ($Command) {
    "start"     { Start-Tunnel }
    "stop"      { Stop-Tunnel }
    "status"    { Show-Status }
    "restart"   { Restart-Tunnel }
    "install"   { Install-Task }
    "uninstall" { Uninstall-Task }
    "log"       { Show-Log }
}