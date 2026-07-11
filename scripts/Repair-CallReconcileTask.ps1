<#
.SYNOPSIS
  Re-register the \Hustleforge\Samus-CallReconcile scheduled task cleanly.

.DESCRIPTION
  Background (2026-07-03): the task was launching every 20 min but its action
  returned 0x80070002 ("system cannot find the file specified") and wrote NO
  log — so the post-call reconcile sweep went dark from 2026-07-01 to
  2026-07-03 while looking "healthy" (Ready state, task launching). Every
  dropped Vapi end-of-call webhook in that window (e.g. all of 2026-07-02's
  outbound batch) was never backfilled into the CRM / operator journal, so the
  forge-ui Samus card showed 0 calls even on days calls were placed.

  The reconcile CLI itself is healthy (running it directly in samus-voice exits
  0). The failure was purely in the host wrapper's launch/environment. This
  script re-registers the task with:
    * a WorkingDirectory (was blank) so relative resolution can't misfire,
    * -NonInteractive (NOT the invalid -NoInteractive, which itself yields
      0x80070002), and
    * S4U principal (runs whether logged on or not), RunLevel Highest.

  Paired with the hardened Run-CallReconcile.ps1 (which now ALWAYS writes a
  JSON log incl. captured docker STDERR), the very next run after this repair
  will either succeed or leave a precise error in
  D:\Hustleforge\Samus\logs\call_reconcile\ — no more silent dark.

  REQUIRES an elevated (Administrator) PowerShell. Run:
    powershell -File D:\Hustleforge\Samus\scripts\Repair-CallReconcileTask.ps1

  After it registers, it kicks one run and prints the resulting log so you can
  confirm the sweep is actually working end-to-end.
#>
[CmdletBinding()]
param(
    [Parameter()][string]$TaskName   = '\Hustleforge\Samus-CallReconcile',
    [Parameter()][int]   $IntervalMin = 20,
    [Parameter()][string]$StartAt     = '06:45'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# NOTE: no upfront [Security.Principal] elevation check — this host runs WDAC
# ConstrainedLanguage, where those .NET static/method calls are blocked. Instead
# the Register-ScheduledTask call below is wrapped in try/catch and surfaces the
# "Access is denied" from a non-elevated run with a clear re-run instruction.

$scriptPath = Join-Path $PSScriptRoot 'Run-CallReconcile.ps1'
if (-not (Test-Path $scriptPath)) { throw "reconcile script not found: $scriptPath" }

# --- Build the task definition ---------------------------------------------
# WorkingDirectory set to the scripts dir (was blank on the broken task).
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath) `
    -WorkingDirectory $PSScriptRoot

# Daily base trigger + 20-min repetition for 24h -> re-arms each day (a plain
# -Once trigger's RepetitionDuration would expire after one day and go quiet).
$trigger = New-ScheduledTaskTrigger -Daily -At $StartAt
$rep = (New-ScheduledTaskTrigger -Once -At $StartAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMin) `
    -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
$trigger.Repetition = $rep

# S4U principal: runs whether the operator is logged on or not, no stored
# password. RunLevel Highest so `docker` (and the Docker engine pipe) resolve.
$principal = New-ScheduledTaskPrincipal -UserId 'PC' -LogonType S4U -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

# Register via -InputObject (a full task object) to sidestep the
# Register-ScheduledTask -Principal+-Password AmbiguousParameterSet trap.
$task = New-ScheduledTask -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'Post-call reconcile sweep: backfill dropped Vapi end-of-call webhooks into CRM + operator journal. Every 20 min. Hardened 2026-07-03 to always write a log.'

$leaf = $TaskName -replace '^\\Hustleforge\\',''
try {
    # Idempotent replace.
    if (Get-ScheduledTask -TaskName $leaf -TaskPath '\Hustleforge\' -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $leaf -TaskPath '\Hustleforge\' -Confirm:$false
        Write-Host "removed existing task $TaskName" -ForegroundColor DarkGray
    }
    Register-ScheduledTask -TaskName $leaf -TaskPath '\Hustleforge\' -InputObject $task | Out-Null
} catch {
    if ("$($_.Exception.Message)" -like '*Access is denied*') {
        throw "Access is denied registering the task — run this in an ELEVATED (Administrator) PowerShell."
    }
    throw
}
Write-Host "registered $TaskName (every $IntervalMin min from $StartAt, S4U/PC, Highest)" -ForegroundColor Green

# --- Kick one run + show the log so we confirm it actually works -----------
Write-Host "starting one run to verify..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $leaf -TaskPath '\Hustleforge\'
Start-Sleep -Seconds 25

$info = Get-ScheduledTask -TaskName $leaf -TaskPath '\Hustleforge\' | Get-ScheduledTaskInfo
Write-Host ("LastRunTime={0}  LastTaskResult=0x{1:X8}" -f $info.LastRunTime, $info.LastTaskResult)

$logDir = 'D:\Hustleforge\Samus\logs\call_reconcile'
$latest = Get-ChildItem -Path $logDir -Filter '*.json' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) {
    Write-Host "--- newest reconcile log ($($latest.Name)) ---" -ForegroundColor Cyan
    Get-Content -Raw -Path $latest.FullName
} else {
    Write-Warning "no reconcile log written yet — check the task history / Docker Desktop is running."
}
