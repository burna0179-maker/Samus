<#
.SYNOPSIS
  Register (or refresh) the Windows Scheduled Task that fires the Samus
  morning brief daily at 08:00.

.DESCRIPTION
  Idempotent -- if a task with the same name already exists, it is unregistered
  first and re-created with the current settings. Runs in the operator's user
  context (NOT SYSTEM) so DPAPI-encrypted secrets in Scope=Samus are
  decryptable by the task action.

  Task behavior:
    Name              Samus Morning Brief
    Trigger           Daily at 08:00 local time
    Action            powershell.exe (hidden window) -> Send-Morning.ps1
    Run as            Current Windows user (interactive token)
    Wake to run       NO (politely waits for next wake)
    Start when avail. YES (catches up if PC was off or asleep at 08:00)
    Time limit        5 minutes (sensible cap on a daily push)

  Per the project's no-elevated-agents rule, the task does NOT request
  elevation. PowerShell's Register-ScheduledTask cmdlet does NOT need
  elevation to create a task in the current user's context -- but it does
  need the ScheduledTasks module (built into Windows 8+; present on Win 11).

.PARAMETER TaskName
  Override the task name. Default: 'Samus Morning Brief'.

.PARAMETER TimeOfDay
  Override the daily trigger time. Default: '08:00'. Format 'HH:mm' (24h).

.PARAMETER Force
  Unregister an existing task with the same name without prompting.
  Default behavior also re-creates, but Force suppresses any future prompt
  that might be added.

.PARAMETER NoConsole
  Register a brief-only task. By DEFAULT the task opens the operator voice
  console in the browser after the brief is pushed (it runs Send-Morning.ps1
  with -OpenConsole). Pass -NoConsole to omit that and keep the task as a
  pure email/Discord push.

.EXAMPLE
  .\Register-MorningBriefSchedule.ps1
  # Registers / refreshes "Samus Morning Brief" to fire daily at 08:00

.EXAMPLE
  # Pick a different time
  .\Register-MorningBriefSchedule.ps1 -TimeOfDay '07:30'

.EXAMPLE
  # See what got registered after running
  Get-ScheduledTask -TaskName 'Samus Morning Brief' | Format-List *

.EXAMPLE
  # Cancel / remove the task
  Unregister-ScheduledTask -TaskName 'Samus Morning Brief' -Confirm:$false
#>

[CmdletBinding()]
param(
    [Parameter()][string]$TaskName = 'Samus Morning Brief',
    [Parameter()][string]$TimeOfDay = '08:00',
    [Parameter()][switch]$Force,
    [Parameter()][switch]$NoConsole
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Locate the wrapper script using this script's own location (not CWD).
$here              = Split-Path -Parent $MyInvocation.MyCommand.Path
$sendMorningScript = Join-Path $here 'Send-Morning.ps1'

if (-not (Test-Path $sendMorningScript)) {
    throw "Send-Morning.ps1 not found at $sendMorningScript -- register from the scripts directory of a checkout that has it."
}

# Parse the time-of-day argument into a [DateTime] anchor today.
$timeParts = $TimeOfDay -split ':'
if ($timeParts.Count -ne 2) {
    throw "TimeOfDay must be 'HH:mm' (e.g. '08:00'); got '$TimeOfDay'."
}
$hour   = [int]$timeParts[0]
$minute = [int]$timeParts[1]
if ($hour -lt 0 -or $hour -gt 23) { throw "Hour must be 0..23; got $hour." }
if ($minute -lt 0 -or $minute -gt 59) { throw "Minute must be 0..59; got $minute." }
$triggerTime = (Get-Date).Date.AddHours($hour).AddMinutes($minute)

# Idempotency: remove any existing task with this name.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Unregistering existing task '$TaskName' to refresh settings..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Build the action via -Command (not -File) so the task runs under this host's
# ConstrainedLanguage / WDAC policy. Propagate exit code for Task Scheduler.
$cmd = "& '$sendMorningScript'"
if (-not $NoConsole) {
    $cmd += ' -OpenConsole'
}
$cmd += '; exit $LASTEXITCODE'
$pwshArgs = "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command `"$cmd`""
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $pwshArgs `
    -WorkingDirectory (Split-Path -Parent $sendMorningScript)

# Daily trigger at the chosen time.
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime

# Run as the current user, interactive logon token (NOT SYSTEM, NOT elevated).
# LogonType=Interactive means the task fires only when the user is logged on
# (the typical state for a daily-driver workstation) -- and importantly, DPAPI
# secrets decrypt correctly because we're under the same user profile that
# encrypted them.
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

# Settings: catch up if missed, don't wake the box, sensible time cap.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8

$description = @"
Samus morning brief -- pushes the daily situational-awareness brief to email
(via SendGrid) and Discord (via webhook). Fires at $TimeOfDay daily.
Source: $sendMorningScript
Registered by: $($here)\Register-MorningBriefSchedule.ps1
"@

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description $description | Out-Null

# Confirm + report
$task = Get-ScheduledTask -TaskName $TaskName
$nextRun = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
Write-Host ""
Write-Host "Registered '$TaskName' successfully." -ForegroundColor Green
Write-Host "  State        : $($task.State)"
Write-Host "  Trigger      : Daily at $TimeOfDay"
Write-Host "  Run as       : $($task.Principal.UserId) (Limited / Interactive)"
Write-Host "  Action       : powershell.exe $pwshArgs"
Write-Host ("  Voice console: {0}" -f $(if ($NoConsole) { 'not opened (brief-only task)' } else { 'opens in the browser after the push (starts the stack if down)' }))
Write-Host "  Next run     : $nextRun"
Write-Host ""
Write-Host "Manage from the GUI:  taskschd.msc  ->  Task Scheduler Library  ->  '$TaskName'"
Write-Host "Cancel:               Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host "Fire now (smoke):     Start-ScheduledTask -TaskName '$TaskName'"
