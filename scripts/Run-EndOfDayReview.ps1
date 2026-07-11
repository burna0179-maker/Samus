<#
.SYNOPSIS
  Trigger Samus's End-of-Day Operational Review (intelligence cycle, evening leg).

.DESCRIPTION
  Runs run_end_of_day_review() INSIDE the samus-gateway container via docker
  exec — the container already holds every credential the review needs
  (OPENAI_API_KEY for the advisor leg, ANTHROPIC_API_KEY for triangulation
  leg C, the finance HMAC key for /snapshot) and writes its artifacts to the
  canonical state root:

    cognition/eod_review_<date>.md      (full three-way triangulation record)
    cognition/gameplan_<date>.json      (tomorrow's gameplan)
    cognition/guidance_ledger.jsonl     (corroborated findings, ingested)

  This is the LOCAL trigger the stack was missing: the gateway route
  (/api/gateway/end_of_day_review) was designed for a Cloud Scheduler cadence
  and no host-side schedule ever existed, so the EOD review never ran and no
  gameplan was ever produced (discovered 2026-07-03).

  Exit code: 0 when the review completed (even with a degraded LLM leg — the
  review is fail-safe by design); 1 when the container is unreachable or the
  review raised.

.PARAMETER NoOpenAI
  Skip the OpenAI tomorrow-guidance consult (local synthesis only).

.EXAMPLE
  .\Run-EndOfDayReview.ps1
#>

[CmdletBinding()]
param(
    [switch]$NoOpenAI
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# svc_Samus (the task principal) has only (RX) on D:\...\.data — it cannot
# create/write a log dir there, so the wrapper died before docker exec (exit 1,
# no review). E:\Hustleforge\Samus\logs is svc_Samus (F), same place the boot
# logs go. 2026-07-08.
$logDir = 'E:\Hustleforge\Samus\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$logFile = Join-Path $logDir ("eod_review_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))

$consult = if ($NoOpenAI) { 'False' } else { 'True' }
$py = @"
import json
from backend.cognitive.intelligence_cycle import run_end_of_day_review
result = run_end_of_day_review(consult_openai=$consult)
print(json.dumps({
    'review_id': result.get('review_id'),
    'ingested': result.get('ingested', 0),
    'effectiveness': result.get('effectiveness', {}),
    'error': result.get('error'),
}))
"@

"[{0}] EOD review starting (consult_openai={1})" -f (Get-Date -Format 'o'), $consult |
    Tee-Object -FilePath $logFile -Append

# Piped via stdin (python -) — `python -c $py` loses inner double quotes to
# Windows native-arg parsing (same bug bit Update-StripeWebhookEvents.ps1).
$out = $py | & docker exec -i samus-gateway python - 2>&1
$exit = $LASTEXITCODE
$out | Tee-Object -FilePath $logFile -Append

if ($exit -ne 0) {
    "[{0}] EOD review FAILED (docker exec exit $exit)" -f (Get-Date -Format 'o') |
        Tee-Object -FilePath $logFile -Append
    exit 1
}
"[{0}] EOD review done" -f (Get-Date -Format 'o') | Tee-Object -FilePath $logFile -Append
exit 0
