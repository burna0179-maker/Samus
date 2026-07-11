<#
.SYNOPSIS
  Post-batch call auditor (Framework Agent self-improving capability).

.DESCRIPTION
  Wraps backend.voice.call_batch_analyzer: pulls a dial batch's Vapi calls
  (READ-ONLY, GET /call), buckets outcomes, runs the P0 reasoning-leak and
  gatekeeper-finding-leak REGRESSION GUARDS, measures time-to-hangup, compares
  to the prior persisted batch, and prints the next-remediation signal the
  machine computes for itself. Appends this batch's metrics to the durable
  cross-batch trend store so "is this cycle better than the last?" is answerable
  from disk.

  READ-ONLY: only GET /call. Never places, modifies, or ends a call. Vapi does
  not bill for API reads.

  Exit code mirrors the analyzer: 0 = clean, 3 = a hard-guard regression
  (reasoning leak or finding-leaked-to-gatekeeper > 0) so a scheduled run can
  alert. 2 = VapiApiKey/config problem.

.PARAMETER DialRun   Path to a dial_run_<id>.json artifact; scopes the batch to
                     that run's PLACED call_ids. Preferred over -Since.
.PARAMETER Since      ISO timestamp; audit only calls created at/after it.
.PARAMETER NoPersist  Preview only; do not append to the durable trend store.
.PARAMETER AsJson     Emit batch metrics as JSON instead of the report.

.EXAMPLE
  # Audit the most recent dial run (after a batch completes):
  .\scripts\Analyze-DialBatch.ps1 -DialRun D:\Hustleforge\Samus\.data\host_artifacts\voice\dial_run_<id>.json

.EXAMPLE
  # Audit everything since 9am, preview without persisting:
  .\scripts\Analyze-DialBatch.ps1 -Since '2026-07-02T09:00:00Z' -NoPersist
#>
[CmdletBinding()]
param(
    [string]$DialRun,
    [string]$Since,
    [switch]$NoPersist,
    [switch]$AsJson
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1 -Force
$vapiKey = Get-HfSecret -Scope Samus -Name VapiApiKey
if (-not $vapiKey) { Write-Error 'VapiApiKey not in Samus DPAPI store.'; exit 2 }

$repo = 'D:\Hustleforge\Samus'
Set-Location $repo

# The analyzer reads VAPI_API_KEY from settings (env). SAMUS_ENV=production
# silences the dev-env warning that would otherwise be raised on import.
$env:VAPI_API_KEY = $vapiKey
$env:SAMUS_ENV    = 'production'

$pyArgs = @('-m', 'backend.voice.call_batch_analyzer')
if ($DialRun)   { $pyArgs += @('--dial-run', $DialRun) }
if ($Since)     { $pyArgs += @('--since', $Since) }
if ($NoPersist) { $pyArgs += '--no-persist' }
if ($AsJson)    { $pyArgs += '--json' }

try {
    & python @pyArgs
    $code = $LASTEXITCODE
}
finally {
    Remove-Item Env:\VAPI_API_KEY -ErrorAction SilentlyContinue
}

if ($code -eq 3) {
    Write-Host ''
    Write-Host 'HARD-GUARD REGRESSION: a reasoning leak or finding-leaked-to-gatekeeper was detected this batch. See the report above.' -ForegroundColor Red
}
exit $code
