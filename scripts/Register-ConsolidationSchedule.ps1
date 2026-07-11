<#
.SYNOPSIS
  Register (or refresh) the Windows Scheduled Task that fires the Samus
  nightly memory consolidation at 02:00.

.DESCRIPTION
  Idempotent -- if a task with the same name already exists, it is
  unregistered first and re-created with the current settings. Mirrors
  Register-MorningBriefSchedule.ps1 (current-user context, not SYSTEM,
  not elevated -- per the project's no-elevated-agents rule).

  This is the HOST fallback for the in-container consolidation timer
  (backend/cognitive/consolidation_task.py, default ON inside the gateway).
  Register it when the container stack is routinely down overnight.

  Task behavior:
    Name              Samus Nightly Consolidation
    Trigger           Daily at 02:00 local time
    Action            python -m backend.cognitive.consolidator
    Run as            Current Windows user (interactive token)
    Start when avail. YES (catches up if the PC was asleep at 02:00)
    Time limit        15 minutes

.PARAMETER TaskName
  Override the task name. Default: 'Samus Nightly Consolidation'.

.PARAMETER TimeOfDay
  Override the daily trigger time. Default: '02:00'. Format 'HH:mm' (24h).

.PARAMETER PythonExe
  Path to the python interpreter. Default: the repo venv python if present
  (<repo>\.venv\Scripts\python.exe), else 'python' from PATH.

.EXAMPLE
  .\Register-ConsolidationSchedule.ps1
  # Registers / refreshes "Samus Nightly Consolidation" to fire daily at 02:00

.EXAMPLE
  Unregister-ScheduledTask -TaskName 'Samus Nightly Consolidation' -Confirm:$false
#>

[CmdletBinding()]
param(
    [Parameter()][string]$TaskName = 'Samus Nightly Consolidation',
    [Parameter()][string]$TimeOfDay = '02:00',
    [Parameter()][string]$PythonExe = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Resolve the repo root from this script's own location (scripts\ -> root).
$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $here

# Resolve the python interpreter: explicit param > repo venv > PATH.
if (-not $PythonExe) {
    $venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) {
        $PythonExe = $venvPython
    } else {
        $PythonExe = 'python'
    }
}

# Parse the time-of-day argument.
$timeParts = $TimeOfDay -split ':'
if ($timeParts.Count -ne 2) {
    throw "TimeOfDay must be 'HH:mm' (e.g. '02:00'); got '$TimeOfDay'."
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

# Action: run the consolidator module from the repo root (hidden window).
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument '-m backend.cognitive.consolidator' `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime

# Current user, interactive logon token (NOT SYSTEM, NOT elevated).
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8

$description = @"
Samus nightly memory consolidation -- distill (lessons -> guidance ledger),
promote (experiment winners/losers), calibrate (closed-loop probabilities),
compress (ledger age-rotation). Fires at $TimeOfDay daily.
Host fallback for the in-container timer (consolidation_task.py).
Registered by: $here\Register-ConsolidationSchedule.ps1
"@

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description $description | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$nextRun = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
Write-Host ""
Write-Host "Registered '$TaskName' successfully." -ForegroundColor Green
Write-Host "  State    : $($task.State)"
Write-Host "  Trigger  : Daily at $TimeOfDay"
Write-Host "  Run as   : $($task.Principal.UserId) (Limited / Interactive)"
Write-Host "  Action   : $PythonExe -m backend.cognitive.consolidator"
Write-Host "  Next run : $nextRun"
Write-Host ""
Write-Host "Manage from the GUI:  taskschd.msc  ->  Task Scheduler Library  ->  '$TaskName'"
Write-Host "Cancel:               Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host "Fire now (smoke):     Start-ScheduledTask -TaskName '$TaskName'"
