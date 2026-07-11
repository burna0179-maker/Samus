<#
.SYNOPSIS
  Duty-cycle the always-on Samus Cloud Run services to control off-hours cost.

.DESCRIPTION
  Twelve Samus Cloud Run services run with min-instances=1 so they stay warm
  24/7 -- the 9 SQS pull-workers (CPU always allocated, cpu-throttling=false)
  plus gateway/finance/voice. That is continuous billing even when no business
  is happening. This script flips their min-instances between 1 (business hours)
  and 0 (off-hours). At min=0 a worker scales to zero, stops polling SQS, and
  costs nothing; the gateway's control-tick loop stops. They cold-start again on
  the next business-hours request (or when scaled back to 1).

  Paired with two Windows Scheduled Tasks:
    Samus Cloud ScaleUp    Mon-Fri 06:45 PT  ->  -Min 1
    Samus Cloud ScaleDown  Mon-Fri 19:00 PT  ->  -Min 0   (after the 18:30 EOD)
  Weekends: no scale-up runs, so the fleet stays at 0 from Fri 19:00 to Mon 06:45.

  The 18:30 EOD review Cloud Scheduler job stays DAILY -- on weekends / after
  scale-down it briefly cold-starts the gateway to produce the report, then the
  service idles back to zero. That is the one allowed off-hours cost.

.PARAMETER Min
  Target min-instances for all duty-cycled services: 0 (off) or 1 (on).

.PARAMETER Project
  GCP project id. Default: ${GCP_PROJECT}.

.PARAMETER Region
  Cloud Run region. Default: us-west1.

.EXAMPLE
  .\Set-SamusCloudDutyCycle.ps1 -Min 0    # scale down (off-hours)
  .\Set-SamusCloudDutyCycle.ps1 -Min 1    # scale up (business hours)
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet(0, 1)][int]$Min,
    [Parameter()][string]$Project = '${GCP_PROJECT}',
    [Parameter()][string]$Region  = 'us-west1'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'  # gcloud writes progress to stderr; don't let it abort the loop

# The 12 services that run min-instances=1 (warm 24/7). Keep in sync with
# `gcloud run services list ... minScale`. Everything else is already min=0.
$services = @(
    'samus-gateway',
    'samus-finance',
    'samus-voice',
    'samus-feedback-worker',
    'samus-fulfillment-worker',
    'samus-leadgen-worker',
    'samus-optimizer-worker',
    'samus-outreach-worker',
    'samus-proposal-worker',
    'samus-prospecting-worker',
    'samus-scaffold-worker',
    'samus-seo-worker'
)

$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
Write-Host "[$stamp] Samus Cloud Run duty-cycle -> min-instances=$Min ($($services.Count) services, $Project/$Region)"

$ok = 0; $fail = 0
foreach ($svc in $services) {
    Write-Host "  $svc -> min=$Min ..." -NoNewline
    & gcloud run services update $svc `
        --project $Project `
        --region $Region `
        --min-instances $Min `
        --quiet 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host " ok"; $ok++ }
    else { Write-Host " FAILED (exit $LASTEXITCODE)"; $fail++ }
}

Write-Host "[$stamp] done: $ok ok, $fail failed."
if ($fail -gt 0) { exit 1 }
