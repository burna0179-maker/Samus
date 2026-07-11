<#
.SYNOPSIS
  Register GCP Cloud Scheduler jobs for the Samus Cloud Run stack.

.DESCRIPTION
  Creates 4 Cloud Scheduler HTTP jobs that trigger Samus daily operations
  on Cloud Run. Each job POSTs to the target workcell's Cloud Run URL with
  an OIDC id-token for authentication.

  Jobs:
    samus-prospecting-daily   07:30 PT  POST /api/prospecting/run_daily
    samus-outreach-daily      09:00 PT  POST /api/outreach/run_daily
    samus-inbox-poll          */15 min   POST /api/intake/poll_inbox
    samus-morning-brief       08:00 PT  POST /api/gateway/morning_brief

  Idempotent -- existing jobs are updated in place (--update is implicit in
  gcloud scheduler jobs update). Re-run safely after URL or schedule changes.

  Prerequisites:
    - Cloud Scheduler API enabled (gcloud services enable cloudscheduler.googleapis.com)
    - samus-runtime SA has roles/run.invoker on the target services
    - Target services are deployed and have stable URLs

.PARAMETER Project
  GCP project ID. Default: ${GCP_PROJECT}

.PARAMETER Region
  GCP region. Default: us-west1

.PARAMETER RuntimeSa
  Service account email for OIDC tokens. The scheduler impersonates this SA
  to call internal Cloud Run services.

.PARAMETER GatewayUrl
  Base URL of the samus-gateway Cloud Run service.

.PARAMETER ProspectingUrl
  Base URL of the samus-prospecting Cloud Run service.

.PARAMETER OutreachUrl
  Base URL of the samus-outreach Cloud Run service.

.PARAMETER IntakeUrl
  Base URL of the samus-intake Cloud Run service.

.PARAMETER DryRun
  Print the gcloud commands without executing them.
#>

[CmdletBinding()]
param(
    [string]$Project      = '${GCP_PROJECT}',
    [string]$Region       = 'us-west1',
    [string]$RuntimeSa    = 'samus-runtime@${GCP_PROJECT}.iam.gserviceaccount.com',
    [string]$GatewayUrl,
    [string]$ProspectingUrl,
    [string]$OutreachUrl,
    [string]$IntakeUrl,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$gcloud = 'C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd'
if (-not (Test-Path $gcloud)) {
    $gcloud = (Get-Command gcloud -ErrorAction SilentlyContinue).Source
    if (-not $gcloud) { throw "gcloud not found." }
}

# Auto-discover URLs from deployed services if not provided.
function Get-ServiceUrl([string]$ServiceName) {
    $url = & $gcloud run services describe $ServiceName `
        --region=$Region --project=$Project `
        --format='value(status.url)' 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not discover URL for $ServiceName -- pass it explicitly."
        return $null
    }
    return $url.Trim()
}

if (-not $GatewayUrl)     { $GatewayUrl     = Get-ServiceUrl 'samus-gateway' }
if (-not $ProspectingUrl) { $ProspectingUrl = Get-ServiceUrl 'samus-prospecting' }
if (-not $OutreachUrl)    { $OutreachUrl    = Get-ServiceUrl 'samus-outreach' }
if (-not $IntakeUrl)      { $IntakeUrl      = Get-ServiceUrl 'samus-intake' }

$jobs = @(
    @{
        Name     = 'samus-pre-shift-briefing'
        Schedule = '0 7 * * *'
        TimeZone = 'America/Los_Angeles'
        Url      = "$GatewayUrl/api/gateway/pre_shift_briefing"
        Method   = 'POST'
        Desc     = 'Pre-Shift Strategic Briefing -- compose intel, consult OpenAI, ingest guidance (before production)'
        Timeout  = '300s'
    }
    @{
        Name     = 'samus-end-of-day-review'
        Schedule = '30 18 * * *'
        TimeZone = 'America/Los_Angeles'
        Url      = "$GatewayUrl/api/gateway/end_of_day_review"
        Method   = 'POST'
        Desc     = 'End-of-Day Operational Review -- score guidance effectiveness + plan tomorrow (after production)'
        Timeout  = '300s'
    }
    @{
        Name     = 'samus-prospecting-daily'
        Schedule = '30 7 * * *'
        TimeZone = 'America/Los_Angeles'
        Url      = "$ProspectingUrl/api/prospecting/run_daily"
        Method   = 'POST'
        Desc     = 'Daily prospecting pipeline -- generates call list before morning brief'
        Timeout  = '1200s'
    }
    @{
        Name     = 'samus-outreach-daily'
        Schedule = '0 9 * * *'
        TimeZone = 'America/Los_Angeles'
        Url      = "$OutreachUrl/api/outreach/run_daily"
        Method   = 'POST'
        Desc     = 'Daily outreach -- sends governed email campaigns'
        Timeout  = '900s'
    }
    @{
        Name     = 'samus-inbox-poll'
        Schedule = '0 8,10,12,14,16,18 * * *'
        TimeZone = 'America/Los_Angeles'
        Url      = "$IntakeUrl/api/intake/poll_inbox"
        Method   = 'POST'
        Desc     = 'Poll Gmail inbox every 2h during business hours (8a-6p PT)'
        Timeout  = '300s'
    }
    @{
        Name     = 'samus-morning-brief'
        Schedule = '0 8 * * *'
        TimeZone = 'America/Los_Angeles'
        Url      = "$GatewayUrl/api/gateway/morning_brief"
        Method   = 'POST'
        Desc     = 'Morning situational-awareness brief -- email + Discord'
        Timeout  = '300s'
    }
)

Write-Host "=== Samus Cloud Scheduler Jobs ===" -ForegroundColor Cyan
Write-Host "  Project  : $Project"
Write-Host "  Region   : $Region"
Write-Host "  SA       : $RuntimeSa"
Write-Host ""

$results = @()
foreach ($job in $jobs) {
    if (-not $job.Url -or $job.Url -match '^\s*$' -or $job.Url -match '/\s*$') {
        Write-Host "SKIP $($job.Name) -- target URL not resolved" -ForegroundColor Red
        $results += [PSCustomObject]@{ Job=$job.Name; Status='skipped_no_url' }
        continue
    }

    Write-Host "--- $($job.Name) ---" -ForegroundColor Cyan
    Write-Host "  Schedule : $($job.Schedule) ($($job.TimeZone))"
    Write-Host "  URL      : $($job.Url)"

    $args = @(
        'scheduler', 'jobs', 'create', 'http', $job.Name,
        "--schedule=$($job.Schedule)",
        "--time-zone=$($job.TimeZone)",
        "--uri=$($job.Url)",
        "--http-method=$($job.Method)",
        "--oidc-service-account-email=$RuntimeSa",
        "--oidc-token-audience=$($job.Url)",
        "--attempt-deadline=$($job.Timeout)",
        "--description=$($job.Desc)",
        "--location=$Region",
        "--project=$Project",
        '--quiet'
    )

    if ($DryRun) {
        Write-Host "  [DRY RUN] gcloud $($args -join ' ')" -ForegroundColor DarkYellow
        $results += [PSCustomObject]@{ Job=$job.Name; Status='dry_run' }
    } else {
        $out = & $gcloud @args 2>&1
        if ($LASTEXITCODE -ne 0) {
            if ($out -match 'ALREADY_EXISTS|already exists') {
                Write-Host "  Already exists -- updating..." -ForegroundColor Yellow
                $args[2] = 'update'
                $out = & $gcloud @args 2>&1
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "  Update failed: $out" -ForegroundColor Red
                    $results += [PSCustomObject]@{ Job=$job.Name; Status='update_failed' }
                    continue
                }
                $results += [PSCustomObject]@{ Job=$job.Name; Status='updated' }
            } else {
                Write-Host "  Create failed: $out" -ForegroundColor Red
                $results += [PSCustomObject]@{ Job=$job.Name; Status='create_failed' }
                continue
            }
        } else {
            $results += [PSCustomObject]@{ Job=$job.Name; Status='created' }
        }
        Write-Host "  OK" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
$results | Format-Table -AutoSize | Out-String | Write-Host

$failed = $results | Where-Object { $_.Status -match 'failed|skipped' }
if ($failed.Count -eq 0) {
    Write-Host "All $($jobs.Count) scheduler jobs registered." -ForegroundColor Green
} else {
    Write-Host "$($failed.Count) job(s) failed -- see above." -ForegroundColor Red
}

Write-Host ""
Write-Host "List jobs:     gcloud scheduler jobs list --location=$Region --project=$Project"
Write-Host "Fire now:      gcloud scheduler jobs run <job-name> --location=$Region --project=$Project"
Write-Host "Pause:         gcloud scheduler jobs pause <job-name> --location=$Region --project=$Project"
Write-Host "Delete:        gcloud scheduler jobs delete <job-name> --location=$Region --project=$Project"
