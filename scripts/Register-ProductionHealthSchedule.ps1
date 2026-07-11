<#
.SYNOPSIS
  Register (or refresh) the Windows Scheduled Task that runs the production-
  failure monitor every 15 minutes and emails alerts to the brief recipient.

.DESCRIPTION
  Idempotent -- an existing task with the same name is unregistered and
  re-created. Runs in the operator's user context (NOT SYSTEM, NOT elevated)
  so the DPAPI Scope=Samus secrets the alert email needs are decryptable, and
  per the project's no-elevated-agents rule.

  Task behavior:
    Name              Samus Production Health
    Trigger           Every 15 minutes, indefinitely (starts on registration)
    Action            powershell.exe (hidden) -> Run-ProductionHealth.ps1
    Run as            Current Windows user (interactive token)
    Start when avail. YES (catches up after sleep/off)
    Overlap           IgnoreNew (a slow run never stacks)
    Time limit        4 minutes (the check is fast; a hang is a failure)

  The alert itself is state-change throttled inside the notify module, so a
  15-minute cadence does NOT mean 96 emails/day -- a persistent failure emails
  once, then stays quiet until it changes or recovers.

.PARAMETER TaskName
  Override the task name. Default: 'Samus Production Health'.

.PARAMETER IntervalMinutes
  Override the repetition interval in minutes. Default: 15.

.PARAMETER EmailTo
  Alert recipient, passed through to Run-ProductionHealth.ps1. Default:
  env SAMUS_PRODUCTION_HEALTH_EMAIL_TO (matches the morning brief).

.EXAMPLE
  .\Register-ProductionHealthSchedule.ps1

.EXAMPLE
  # See what got registered
  Get-ScheduledTask -TaskName 'Samus Production Health' | Format-List *

.EXAMPLE
  # Fire once now (smoke test)
  Start-ScheduledTask -TaskName 'Samus Production Health'

.EXAMPLE
  # Remove the task
  Unregister-ScheduledTask -TaskName 'Samus Production Health' -Confirm:$false
#>

[CmdletBinding()]
param(
    [Parameter()][string]$TaskName = 'Samus Production Health',
    [Parameter()][int]$IntervalMinutes = 15,
    [Parameter()][string]$EmailTo$EmailTo = $env:SAMUS_PRODUCTION_HEALTH_EMAIL_TO
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($IntervalMinutes -lt 1 -or $IntervalMinutes -gt 1440) {
    throw "IntervalMinutes must be 1..1440; got $IntervalMinutes."
}

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$runScript  = Join-Path $here 'Run-ProductionHealth.ps1'

if (-not (Test-Path $runScript)) {
    throw "Run-ProductionHealth.ps1 not found at $runScript -- register from the scripts directory of a checkout that has it."
}

# Idempotency: remove any existing task with this name.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Unregistering existing task '$TaskName' to refresh settings..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Action: hidden powershell running the wrapper via the call operator (&),
# forwarding -EmailTo. NOT `-File`: empirically, `powershell -File <this wrapper>`
# fails on this host (no transcript, nonzero exit, Python never runs) -- the
# host's ConstrainedLanguage / script-loading policy. `-Command "& <script>"`
# runs it correctly (verified via the live task). Propagate the wrapper's exit
# code so Task Scheduler's Last Result reflects real success.
$cmd = "& '$runScript' -EmailTo '$EmailTo'; exit `$LASTEXITCODE"
$pwshArgs = "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command `"$cmd`""
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $pwshArgs `
    -WorkingDirectory (Split-Path -Parent $runScript)

# Every-N-minutes trigger. PS 5.1 cannot build an indefinite repeating trigger
# directly, so borrow the Repetition block from a helper trigger (the standard
# workaround). A 3650-day duration is effectively indefinite.
$anchor = (Get-Date).Date
$trigger = New-ScheduledTaskTrigger -Once -At $anchor
$helper  = New-ScheduledTaskTrigger -Once -At $anchor `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$trigger.Repetition = $helper.Repetition

# Run as the current user, interactive, unelevated. Interactive means it fires
# while the operator is logged on (the daily-driver norm) and, crucially, DPAPI
# secrets decrypt under the same profile that encrypted them. UserId is built
# from env vars (not [WindowsIdentity]) so this registrar also runs under the
# host's WDAC-enforced ConstrainedLanguage mode.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Catch up if missed, tolerate battery, never overlap, hard 4-minute cap.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8

$description = @"
Samus production-failure monitor -- runs backend.observability.production_health_notify
every $IntervalMinutes minutes and emails an alert to $EmailTo when the health state
changes (new failure or recovery). Alerts are state-change throttled, so a persistent
failure emails once. Source: $runScript
Registered by: $here\Register-ProductionHealthSchedule.ps1
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
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "Registered '$TaskName' successfully." -ForegroundColor Green
Write-Host "  State      : $($task.State)"
Write-Host "  Trigger    : every $IntervalMinutes min (indefinite)"
Write-Host "  Run as     : $($task.Principal.UserId) (Limited / Interactive)"
Write-Host "  Alert to   : $EmailTo (state-change throttled)"
Write-Host "  Next run   : $($info.NextRunTime)"
Write-Host ""
Write-Host "Fire now (smoke):  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Cancel:            Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
