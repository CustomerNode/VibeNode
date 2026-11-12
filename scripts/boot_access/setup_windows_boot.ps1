<#
.SYNOPSIS
  Make VibeNode's phone Start-page survive reboots on Windows — even before
  anyone logs in.

.DESCRIPTION
  Registers a Scheduled Task that launches reviver.py (windowless, pythonw) at
  SYSTEM STARTUP, running as YOUR account "whether user is logged on or not".
  That is the one Windows mechanism that runs your code pre-login without a
  Windows service, and it requires your account password ONCE at registration
  (Windows stores it in the Task Scheduler's credential vault — this script
  never writes it anywhere).

  After a reboot, before any login: tailscaled (a system service) brings the
  tailnet up, the reviver serves the Start page on 127.0.0.1:5050, and your
  phone can tap "Start VibeNode" as usual.

  Idempotent — run again any time (e.g. after moving the checkout).

.PARAMETER Uninstall
  Remove the boot task and exit.

.NOTES
  Run from anywhere; paths are derived from this script's location:
    powershell -ExecutionPolicy Bypass -File setup_windows_boot.ps1
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# Repo root = two levels up from scripts/boot_access/. Salt the task name with
# the checkout path (same idea as reviver.py) so multiple clones never fight.
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Reviver  = Join-Path $RepoRoot 'reviver.py'
$md5      = [System.Security.Cryptography.MD5]::Create()
$saltHex  = -join ($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($RepoRoot)) |
                   ForEach-Object { $_.ToString('x2') })
$TaskName = "VibeNode Boot Access $($saltHex.Substring(0,8))"

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed task '$TaskName'." -ForegroundColor Green
    } else {
        Write-Host "Task '$TaskName' not found — nothing to remove."
    }
    exit 0
}

if (-not (Test-Path $Reviver)) {
    throw "reviver.py not found at '$Reviver' — run this script from inside the VibeNode checkout."
}

# Windowless interpreter so boot never flashes a console.
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    # Fall back to python.exe's sibling (common when only python.exe is on PATH).
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($python) {
        $candidate = Join-Path (Split-Path $python) 'pythonw.exe'
        if (Test-Path $candidate) { $pythonw = $candidate }
    }
}
if (-not $pythonw) {
    throw "pythonw.exe not found on PATH. Install Python (python.org) with 'Add to PATH' checked, then re-run."
}

Write-Host ""
Write-Host "This registers a boot-time task running as your account." -ForegroundColor Cyan
Write-Host "Windows needs your account password once to allow pre-login runs;"
Write-Host "it goes directly into Task Scheduler's vault, nowhere else."
Write-Host ""
$cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
    -Message "Password for '$env:USERNAME' (grants the task pre-login rights)"
if (-not $cred) { throw "Cancelled — no task registered." }

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument ('"{0}"' -f $Reviver) -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtStartup
# Give tailscaled and the network stack a moment before the reviver polls.
$trigger.Delay = 'PT30S'

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Limited `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
        -Force | Out-Null
} catch {
    if ($_.Exception.Message -match 'batch logon|logon type|0x80070569') {
        throw ("Registration failed: your account lacks the 'Log on as a batch job' right " +
               "(common on managed/domain machines). Ask an admin to grant it via " +
               "secpol.msc > Local Policies > User Rights Assignment, then re-run. " +
               "Original error: $($_.Exception.Message)")
    }
    throw
}

Write-Host ""
Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host "  Runs: `"$pythonw`" `"$Reviver`""
Write-Host "  Trigger: at system startup (30s delay), logged in or not."
Write-Host ""

# Verify it can actually launch (also proves the stored credential works).
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$info = Get-ScheduledTaskInfo -TaskName $TaskName
if ($info.LastTaskResult -in 0, 0x41301) {   # 0x41301 = still running (expected: reviver loops forever)
    Write-Host "Verified: task started (reviver singleton logic dedupes if one is already running)." -ForegroundColor Green
} else {
    Write-Warning ("Task start returned 0x{0:X} - check Task Scheduler > '{1}' > History." -f $info.LastTaskResult, $TaskName)
}

# Fast Startup makes "shut down + power on" skip at-startup triggers (it's a
# hibernate resume, not a boot). Restart always works. Warn if it's on.
try {
    $fs = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
        -Name HiberbootEnabled -ErrorAction Stop
    if ($fs.HiberbootEnabled -eq 1) {
        Write-Host ""
        Write-Warning ("Fast Startup is ON: after a 'Shut down' + power-on, at-startup tasks may NOT run " +
            "(they always run after 'Restart'). For full cold-boot coverage, disable Fast Startup: " +
            "Control Panel > Power Options > 'Choose what the power buttons do', or run 'powercfg /h off' as admin.")
    }
} catch { }

Write-Host ""
Write-Host "Test it for real: reboot without logging in, then open VibeNode on your phone." -ForegroundColor Cyan
