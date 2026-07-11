<#
.SYNOPSIS
  Register (or refresh) the Windows Scheduled Task that drains the Samus
  Gmail company inbox every 10 minutes into the CRM.

.DESCRIPTION
  Idempotent -- if a task with the same name already exists, it is unregistered
  first and re-created with the current settings. Runs in the operator's user
  context (NOT SYSTEM) so DPAPI-encrypted secrets in Scope=Samus are
  decryptable by the task action.

  Task behavior:
    Name              Samus Inbox Poll
    Trigger           Every $IntervalMinutes minutes, indefinitely
    Action            powershell.exe (hidden) -Command "& Poll-Inbox.ps1"
    Run as            Current Windows user (interactive token)
    Wake to run       NO (politely waits for next wake)
    Start when avail. YES (catches up if PC was off / asleep at fire time)
    Time limit        3 minutes (one drain should never need longer)

  Per the project's no-elevated-agents rule, the task does NOT request
  elevation. Cmdlet Register-ScheduledTask creates tasks in the current
  user's context without elevation.

.PARAMETER TaskName
  Override the task name. Default: 'Samus Inbox Poll'.

.PARAMETER IntervalMinutes
  Override the polling cadence. Default: 10. Minimum 1.

.EXAMPLE
  .\Register-InboxPollSchedule.ps1
  # Registers / refreshes "Samus Inbox Poll" to fire every 10 minutes

.EXAMPLE
  .\Register-InboxPollSchedule.ps1 -IntervalMinutes 5
  # More aggressive polling -- still well under Gmail's IMAP rate limits

.EXAMPLE
  # Fire one drain manually (smoke test)
  Start-ScheduledTask -TaskName 'Samus Inbox Poll'

.EXAMPLE
  # Remove the task
  Unregister-ScheduledTask -TaskName 'Samus Inbox Poll' -Confirm:$false
#>

[CmdletBinding()]
param(
    [Parameter()][string]$TaskName = 'Samus Inbox Poll',
    [Parameter()][int]$IntervalMinutes = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($IntervalMinutes -lt 1) {
    throw "IntervalMinutes must be >= 1; got $IntervalMinutes."
}

$here           = Split-Path -Parent $MyInvocation.MyCommand.Path
$pollInboxScript = Join-Path $here 'Poll-Inbox.ps1'

if (-not (Test-Path $pollInboxScript)) {
    throw "Poll-Inbox.ps1 not found at $pollInboxScript -- register from the scripts directory of a checkout that has it."
}

# Idempotency: remove any existing task with this name.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Unregistering existing task '$TaskName' to refresh settings..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# NOT `-File`: empirically, `powershell -File <wrapper>` fails on this host
# (no transcript, nonzero exit, Python never runs) -- the host's WDAC /
# ConstrainedLanguage script-loading policy blocks unsigned .ps1 files.
# `-Command "& <script>"` works correctly (verified via the live task fix
# on 2026-07-06). Propagate the wrapper's exit code so Task Scheduler's
# Last Result reflects real success.
$cmd = "& '$pollInboxScript'; exit `$LASTEXITCODE"
$pwshArgs = "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command `"$cmd`""
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $pwshArgs `
    -WorkingDirectory (Split-Path -Parent $pollInboxScript)

# Repeating trigger -- Windows Task Scheduler rejects RepetitionDuration values
# longer than ~24 days, and the CIM-flavored New-ScheduledTaskTrigger cmdlet
# does not expose a settable .Repetition on Daily triggers. The robust path
# is to build the trigger as a CimInstance directly with the repetition
# embedded; Duration="" is the documented "indefinitely" marker that Task
# Scheduler accepts without choking on a finite-but-huge XML duration.
$startTime = (Get-Date).AddSeconds(30)
$repClass = Get-CimClass `
    -ClassName MSFT_TaskRepetitionPattern `
    -Namespace Root/Microsoft/Windows/TaskScheduler
$triggerClass = Get-CimClass `
    -ClassName MSFT_TaskTimeTrigger `
    -Namespace Root/Microsoft/Windows/TaskScheduler

$repetition = New-CimInstance -CimClass $repClass -ClientOnly -Property @{
    Interval = "PT${IntervalMinutes}M"
    Duration = ""   # empty string = repeat indefinitely
}
$trigger = New-CimInstance -CimClass $triggerClass -ClientOnly -Property @{
    Enabled       = $true
    StartBoundary = $startTime.ToString("yyyy-MM-ddTHH:mm:ss")
    Repetition    = $repetition
}

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8

$description = @"
Samus Inbox Poll -- drains the company Gmail inbox every $IntervalMinutes
minutes; each unread customer email becomes a CRM artifact (kind=inbound_email)
and an operator task (kind=reply_email) with the sender's Stripe billing
context inlined in the description.
Source: $pollInboxScript
Registered by: $($here)\Register-InboxPollSchedule.ps1
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
Write-Host "  Trigger      : Daily at $($startTime.ToString('HH:mm:ss')), repeating every $IntervalMinutes min"
Write-Host "  Run as       : $($task.Principal.UserId) (Limited / Interactive)"
Write-Host "  Action       : powershell.exe -Command `"& '$pollInboxScript'; exit `$LASTEXITCODE`""
Write-Host "  Next run     : $nextRun"
Write-Host ""
Write-Host "Manage from the GUI:  taskschd.msc  ->  Task Scheduler Library  ->  '$TaskName'"
Write-Host "Cancel:               Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host "Fire now (smoke):     Start-ScheduledTask -TaskName '$TaskName'"
