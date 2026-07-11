<#
.SYNOPSIS
  Register (or refresh) the Windows Scheduled Task that fires the Apollo-sourced
  email outreach campaign daily at 08:30 — after the 07:30 prospecting run and
  the 08:00 morning brief, so the outbound-email lane runs once the day's
  read-side work is done.

.DESCRIPTION
  Idempotent -- if a task with the same name exists, it is unregistered + recreated.
  Runs in the operator's user context (interactive logon, NOT SYSTEM, NOT elevated)
  so DPAPI-encrypted secrets in Scope=Samus (ApolloApiKey, AnthropicApiKey, AWS
  creds, SesFromEmail) decrypt correctly.

  Task behavior:
    Name              Samus Outreach Daily
    Trigger           Daily at 08:30 local time
    Action            powershell.exe (hidden window) -> Run-OutreachDaily.ps1
    Run as            Current Windows user (interactive token)
    Start when avail. YES (catches up if PC was off at 08:30)
    Time limit        30 minutes (Apollo search + unlock + SES sends)

  Per project's no-elevated-agents rule, the task does NOT request elevation.

  NOTE: the registered task runs Run-OutreachDaily.ps1 with NO -PostalAddress.
  The campaign build fails closed without a CAN-SPAM postal address, so set
  SAMUS_OUTREACH_POSTAL_ADDRESS as a user/machine env var (or pass -PostalAddress
  and re-register with your own argument string) before the first live run.

.PARAMETER TaskName
  Override the task name. Default: 'Samus Outreach Daily'.

.PARAMETER TimeOfDay
  Override the daily trigger time. Default: '08:30'. Format 'HH:mm' (24h).

.PARAMETER ExtraArgs
  Extra argument string appended to the Run-OutreachDaily.ps1 invocation, e.g.
  "-MaxSend 10 -Industries 'hvac,roofing' -PostalAddress '123 Main St, ...'".

.EXAMPLE
  .\Register-OutreachDailySchedule.ps1
  # registers 'Samus Outreach Daily' to fire daily at 08:30

.EXAMPLE
  # Smoke-test the wiring first (spends Apollo search quota, sends nothing):
  .\Run-OutreachDaily.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [Parameter()][string]$TaskName = 'Samus Outreach Daily',
    [Parameter()][string]$TimeOfDay = '08:30',
    [Parameter()][string]$ExtraArgs = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here          = Split-Path -Parent $MyInvocation.MyCommand.Path
$runDailyScript = Join-Path $here 'Run-OutreachDaily.ps1'

if (-not (Test-Path $runDailyScript)) {
    throw "Run-OutreachDaily.ps1 not found at $runDailyScript -- register from the same scripts directory."
}

$timeParts = $TimeOfDay -split ':'
if ($timeParts.Count -ne 2) { throw "TimeOfDay must be 'HH:mm' (e.g. '08:30'); got '$TimeOfDay'." }
$hour   = [int]$timeParts[0]
$minute = [int]$timeParts[1]
if ($hour -lt 0 -or $hour -gt 23) { throw "Hour must be 0..23; got $hour." }
if ($minute -lt 0 -or $minute -gt 59) { throw "Minute must be 0..59; got $minute." }
$triggerTime = (Get-Date).Date.AddHours($hour).AddMinutes($minute)

# Idempotency: remove existing.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Unregistering existing task '$TaskName' to refresh settings..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$cmd = "& '$runDailyScript'"
if (-not [string]::IsNullOrWhiteSpace($ExtraArgs)) { $cmd += " $ExtraArgs" }
$cmd += '; exit $LASTEXITCODE'
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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8

$description = @"
Samus outreach daily -- runs backend.outreach.run_campaign: pulls prospects from
Apollo.io (LinkedIn-derived people + business emails), composes a CAN-SPAM
compliant cold email per contact, sends via SES, and records each send to a
JSONL campaign ledger + emailed-suppression file under
<SAMUS_ARTIFACT_ROOT>/outreach/. Fires at $TimeOfDay daily.
Source: $runDailyScript
Registered by: $($here)\Register-OutreachDailySchedule.ps1
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
Write-Host "  Next run     : $nextRun"
Write-Host ""
Write-Host "Smoke-test now (Apollo search quota, no send):  .\Run-OutreachDaily.ps1 -DryRun"
Write-Host "Fire-now (real send):                           Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Cancel:                                         Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
