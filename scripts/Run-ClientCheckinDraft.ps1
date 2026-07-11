<#
.SYNOPSIS
  Client check-in DRAFT cadence. Runs weekly; drafts + queues a personal
  check-in for any active client whose last check-in was >= 49 days ago
  (~7 weeks; operator: every 6-8 weeks). Each draft enters the 24h-hold send
  queue and auto-sends unless stopped (see Samus-ClientCheckinSend).

.DESCRIPTION
  Registered as \Hustleforge\Samus-ClientCheckinDraft: weekly (Mon 08:00),
  PC/S4U, RunLevel Highest. Switch is -NonInteractive (not -NoInteractive).

  Generation uses the local model (LM Studio). If no model is loaded (or the
  LLM path is down), the generator skips fail-soft — no draft, no crash — and
  the next weekly run retries. Load a model in LM Studio to enable.
#>
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root   = 'D:\Hustleforge\Samus'
$gen    = Join-Path $root 'scripts\gen_client_checkin.py'
$logDir = Join-Path $root 'logs\client_checkin'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir ("checkin_draft_" + (Get-Date -Format 'yyyy-MM-dd_HHmmss') + ".log")

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'D:\Hustleforge\Samus\.venv\Scripts\python.exe' }
if (-not (Test-Path $py)) { throw "python not found." }

$ErrorActionPreference = 'Continue'   # generator writes progress to stdout; don't die on native stderr
$out = & $py $gen 2>&1 | Out-String
Write-Host $out.Trim()
$out | Out-File -FilePath $logPath -Encoding utf8
exit 0
