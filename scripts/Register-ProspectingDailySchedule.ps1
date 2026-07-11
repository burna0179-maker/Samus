<#
.SYNOPSIS
  Register (or refresh) the Windows Scheduled Task that fires the prospecting
  pipeline daily at 07:30 so the call list is on disk before the 08:00 brief.

.DESCRIPTION
  Idempotent -- if a task with the same name exists, it is unregistered + recreated.
  Runs in the operator's user context (interactive logon, NOT SYSTEM, NOT elevated)
  so DPAPI-encrypted secrets in Scope=Samus (GooglePlacesApiKey) decrypt correctly.

  Task behavior:
    Name              Samus Prospecting Daily
    Trigger           Daily at 07:30 local time (30 minutes before the 08:00 brief)
    Action            powershell.exe (hidden window) -> Run-ProspectingDaily.ps1
    Run as            Current Windows user (interactive token)
    Wake to run       NO
    Start when avail. YES (catches up if PC was off at 07:30)
    Time limit        20 minutes (Places API + scoring + CSV write should be ~1-3 min;
                      20 min is a generous cap that won't truncate a slow run)

  Per project's no-elevated-agents rule, the task does NOT request elevation.

.PARAMETER TaskName
  Override the task name. Default: 'Samus Prospecting Daily'.

.PARAMETER TimeOfDay
  Override the daily trigger time. Default: '07:30'. Format 'HH:mm' (24h).
  Must be earlier than the morning brief's trigger to make sense.

.EXAMPLE
  .\Register-ProspectingDailySchedule.ps1
  # registers / refreshes 'Samus Prospecting Daily' to fire daily at 07:30

.EXAMPLE
  # Run once before scheduling to confirm wiring works without burning quota:
  .\Run-ProspectingDaily.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [Parameter()][string]$TaskName = 'Samus Prospecting Daily',
    [Parameter()][string]$TimeOfDay = '07:30'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here          = Split-Path -Parent $MyInvocation.MyCommand.Path
$runDailyScript = Join-Path $here 'Run-ProspectingDaily.ps1'

if (-not (Test-Path $runDailyScript)) {
    throw "Run-ProspectingDaily.ps1 not found at $runDailyScript -- register from the same scripts directory."
}

$timeParts = $TimeOfDay -split ':'
if ($timeParts.Count -ne 2) { throw "TimeOfDay must be 'HH:mm' (e.g. '07:30'); got '$TimeOfDay'." }
$hour   = [int]$timeParts[0]
$minute = [int]$timeParts[1]
if ($hour -lt 0 -or $hour -gt 23) { throw "Hour must be 0..23; got $hour." }
if ($minute -lt 0 -or $minute -gt 59) { throw "Minute must be 0..59; got $minute." }
$triggerTime = (Get-Date).Date.AddHours($hour).AddMinutes($minute)

# Sanity: warn if scheduled AFTER the Morning Brief (08:00 by default).
$morningBrief = Get-ScheduledTask -TaskName 'Samus Morning Brief' -ErrorAction SilentlyContinue
if ($morningBrief) {
    $briefTrigger = $morningBrief.Triggers | Where-Object { $_.CimClass.CimClassName -match 'Daily' } | Select-Object -First 1
    if ($briefTrigger -and $briefTrigger.StartBoundary) {
        # Compare time-of-day only; StartBoundary carries the brief task's
        # original registration date, which would otherwise make a 07:30 today
        # look "later than" an 08:00 from days ago.
        $briefTime = [DateTime]$briefTrigger.StartBoundary
        if ($triggerTime.TimeOfDay -ge $briefTime.TimeOfDay) {
            Write-Warning "Prospecting trigger '$TimeOfDay' is at/after the Morning Brief '$($briefTime.ToString('HH:mm'))' -- the brief may miss today's call list. Pick a time at least 15 min earlier."
        }
    }
}

# Idempotency: remove existing.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Unregistering existing task '$TaskName' to refresh settings..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$cmd = "& '$runDailyScript'; exit `$LASTEXITCODE"
$pwshArgs = "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command `"$cmd`""
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $pwshArgs `
    -WorkingDirectory (Split-Path -Parent $runDailyScript)

$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8

$description = @"
Samus prospecting daily -- runs backend.prospecting.run_daily against the
currently-active geographic ring (state file at
<SAMUS_ARTIFACT_ROOT>/prospecting_geo_state.json). Writes
call_list_<date>.csv + morning_call_list_<date>.txt so the 08:00 Morning Brief
can attach the call list. Fires at $TimeOfDay daily.
Source: $runDailyScript
Registered by: $($here)\Register-ProspectingDailySchedule.ps1
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
Write-Host "  State        : $($task.State)"
Write-Host "  Trigger      : Daily at $TimeOfDay"
Write-Host "  Run as       : $($task.Principal.UserId) (Limited / Interactive)"
Write-Host "  Action       : powershell.exe $pwshArgs"
Write-Host "  Next run     : $nextRun"
Write-Host ""
Write-Host "Smoke-test now (no quota burn):  .\Run-ProspectingDaily.ps1 -DryRun"
Write-Host "Fire-now (real run):             Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Cancel:                          Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
