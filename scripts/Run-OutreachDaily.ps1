<#
.SYNOPSIS
  Fire one Apollo-sourced email outreach campaign. Pulls prospects from
  Apollo.io (LinkedIn-derived people + business emails) — NOT the Google-Places
  call list — composes a compliant cold email per contact, and sends via SES.

.DESCRIPTION
  This is the outbound-email counterpart to Run-ProspectingDaily.ps1. It runs
  in the host venv (no Docker / SQS dependency) and drives
  backend.outreach.run_campaign:

    Apollo People Search -> unlock locked emails (capped) -> suppress + compliance
    + per-run cap -> outreach.service.send_message (SES) -> JSONL campaign ledger
    + emailed-suppression file (so the next run never re-emails a contact).

  Email send goes through common.email_backend.send_email — the provider
  selector, which defaults to SendGrid (settings.email_backend). Samus is on
  SendGrid until AWS SES is re-approved; flip EMAIL_BACKEND=ses later to switch
  back. NOT the outreach workcell's send_message (still SES-hardcoded).

  Secrets read from DPAPI (Scope=Samus):

    ApolloApiKey        -> APOLLO_API_KEY      (required; aborts if missing)
    SendgridApiKey      -> SENDGRID_API_KEY    (required for live send)
    SendgridFromEmail   -> SENDGRID_FROM_EMAIL (verified sender)
    AnthropicApiKey     -> ANTHROPIC_API_KEY   (optional; only used with -UseLlm)

  CAN-SPAM compliance is enforced in code: the campaign build REFUSES to send
  unless a postal address AND unsubscribe URL are configured. Provide them via
  -PostalAddress / -UnsubscribeUrl (or the SAMUS_OUTREACH_* env vars). With no
  postal address the run fails closed — by design.

  Logs to ../logs/outreach_daily/ (Start-Transcript + sidecar stderr + rotation
  to last 30), same shape as Run-ProspectingDaily.ps1.

.PARAMETER DryRun
  Resolve + compose everything (and spend Apollo search quota) but send no email
  and write no ledger. Safe to run repeatedly while tuning targeting.

.PARAMETER MaxSend
  Hard cap on emails sent this run. Default 25 — keep low during SES warmup.

.PARAMETER Titles / Industries / Locations
  Apollo targeting. Comma-separated. Locations are person locations, e.g.
  'Yuba City, California, US'.

.PARAMETER UseLlm
  Opt into budget-gated LLM personalisation. Default OFF (templated, $0).

.PARAMETER PostalAddress / UnsubscribeUrl
  CAN-SPAM footer fields. PostalAddress has NO default (fail-closed).

.EXAMPLE
  .\Run-OutreachDaily.ps1 -DryRun -Titles 'owner,founder' -Locations 'Yuba City, California, US'

.EXAMPLE
  .\Run-OutreachDaily.ps1 -Titles 'owner' -Industries 'hvac,roofing' -MaxSend 10 `
      -PostalAddress '123 Main St, Yuba City, CA 95991'
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [int]$MaxSend = 25,
    [int]$Pages = 1,
    [int]$PerPage = 25,
    [string]$Titles = 'owner,founder,president,marketing manager',
    [string]$Industries = '',
    [string]$Locations = 'Yuba City, California, US',
    [string]$Subject = 'Quick question about {company}',
    [switch]$UseLlm,
    [switch]$AllowUnverified,
    [string]$FromName = 'Samus',
    [string]$PostalAddress = '',
    [string]$UnsubscribeUrl = 'https://hustleforge.tech/unsubscribe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Run from Samus repo root with .venv installed."
}

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Required secrets module not found at $sharedModule."
}
Import-Module $sharedModule -Force

# --- transcript capture (same shape as Run-ProspectingDaily.ps1) -------------
$logDir = Join-Path $samusRoot 'logs\outreach_daily'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir ("outreach_daily_$((Get-Date).ToString('yyyy-MM-dd_HHmmss')).log")
$transcriptStarted = $false
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true
    Write-Host "transcript -> $logPath"
} catch {
    Write-Warning "Start-Transcript failed (continuing without log): $_"
}

try {
    Get-ChildItem $logDir -Filter 'outreach_daily_*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 30 |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { Write-Warning "log rotation failed: $_" }

$exit = 1

try {

# --- artifact root (D:-side, Alex-writable; matches Run-ProspectingDaily) -----
$defaultArtifactRoot = 'D:\Hustleforge\Samus\.data\host_artifacts'
$artifactRoot = if ($env:SAMUS_ARTIFACT_ROOT) { $env:SAMUS_ARTIFACT_ROOT } else { $defaultArtifactRoot }
if (-not (Test-Path $artifactRoot)) { New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null }

# --- secret + env preconditions ----------------------------------------------
if (-not (Test-HfSecret -Scope Samus -Name ApolloApiKey)) {
    throw "ApolloApiKey not in Samus DPAPI store. Run: Set-HfSecret -Scope Samus -Name ApolloApiKey"
}

if (-not $DryRun) {
    if ([string]::IsNullOrWhiteSpace($PostalAddress) -and [string]::IsNullOrWhiteSpace($env:SAMUS_OUTREACH_POSTAL_ADDRESS)) {
        throw "CAN-SPAM: -PostalAddress (or SAMUS_OUTREACH_POSTAL_ADDRESS) is required for a live send. Aborting fail-closed."
    }
}

$auditPath = Join-Path $artifactRoot 'outreach\outreach_audit.jsonl'

# Env push -- always restore in finally, even on throw.
$envNames = @(
    'APOLLO_API_KEY','ANTHROPIC_API_KEY','SENDGRID_API_KEY','SENDGRID_FROM_EMAIL',
    'EMAIL_BACKEND','PYTHONIOENCODING','SAMUS_ARTIFACT_ROOT','SAMUS_OUTREACH_AUDIT_PATH',
    'SAMUS_OUTREACH_FROM_NAME','SAMUS_OUTREACH_POSTAL_ADDRESS','SAMUS_OUTREACH_UNSUBSCRIBE_URL'
)
$envBackup = @{}
foreach ($n in $envNames) { $envBackup[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }

Set-Item -Path 'Env:APOLLO_API_KEY' -Value (Get-HfSecret -Scope Samus -Name ApolloApiKey)
Set-Item -Path 'Env:PYTHONIOENCODING' -Value 'utf-8'
Set-Item -Path 'Env:SAMUS_ARTIFACT_ROOT' -Value $artifactRoot
Set-Item -Path 'Env:SAMUS_OUTREACH_AUDIT_PATH' -Value $auditPath
Set-Item -Path 'Env:SAMUS_OUTREACH_FROM_NAME' -Value $FromName
if (-not [string]::IsNullOrWhiteSpace($PostalAddress))  { Set-Item -Path 'Env:SAMUS_OUTREACH_POSTAL_ADDRESS' -Value $PostalAddress }
if (-not [string]::IsNullOrWhiteSpace($UnsubscribeUrl)) { Set-Item -Path 'Env:SAMUS_OUTREACH_UNSUBSCRIBE_URL' -Value $UnsubscribeUrl }

# SendGrid is the active backend (settings.email_backend defaults to sendgrid).
Set-Item -Path 'Env:EMAIL_BACKEND' -Value 'sendgrid'
if (Test-HfSecret -Scope Samus -Name AnthropicApiKey) {
    Set-Item -Path 'Env:ANTHROPIC_API_KEY' -Value (Get-HfSecret -Scope Samus -Name AnthropicApiKey)
}
if (Test-HfSecret -Scope Samus -Name SendgridApiKey) {
    Set-Item -Path 'Env:SENDGRID_API_KEY' -Value (Get-HfSecret -Scope Samus -Name SendgridApiKey)
} elseif (-not $DryRun) {
    throw "SendgridApiKey not in Samus DPAPI store. Run: Set-HfSecret -Scope Samus -Name SendgridApiKey"
}
if (Test-HfSecret -Scope Samus -Name SendgridFromEmail) {
    Set-Item -Path 'Env:SENDGRID_FROM_EMAIL' -Value (Get-HfSecret -Scope Samus -Name SendgridFromEmail)
}

# --- build python argv -------------------------------------------------------
$pyArgs = @(
    '-m','backend.outreach.run_campaign',
    '--titles', $Titles,
    '--locations', $Locations,
    '--per-page', $PerPage,
    '--pages', $Pages,
    '--max-send', $MaxSend,
    '--subject', $Subject
)
# --industries is optional; passing it with an empty value (the -Industries
# default) makes argparse fail 'expected one argument'. Only append when set.
if (-not [string]::IsNullOrWhiteSpace($Industries)) { $pyArgs += @('--industries', $Industries) }
if ($UseLlm)          { $pyArgs += '--use-llm' }
if ($AllowUnverified) { $pyArgs += '--allow-unverified' }
if ($DryRun)          { $pyArgs += '--dry-run' }

Write-Host ""
Write-Host "=== Outreach daily run ==="
Write-Host ("Titles      : {0}" -f $Titles)
Write-Host ("Industries  : {0}" -f $Industries)
Write-Host ("Locations   : {0}" -f $Locations)
Write-Host ("MaxSend     : {0}  Pages: {1}  PerPage: {2}" -f $MaxSend, $Pages, $PerPage)
Write-Host ("LLM         : {0}   Verified-only: {1}   DryRun: {2}" -f $UseLlm, (-not $AllowUnverified), $DryRun)
Write-Host ""

$stderrPath = "$logPath.err"
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

Push-Location $samusRoot
try {
    & $venvPython @pyArgs 2>$stderrPath
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
    foreach ($k in $envBackup.Keys) {
        if ($null -eq $envBackup[$k]) {
            Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$k" -Value $envBackup[$k]
        }
    }
    if (Test-Path $stderrPath) {
        $stderrText = Get-Content $stderrPath -Raw -ErrorAction SilentlyContinue
        if ($stderrText) {
            Write-Host "--- python stderr ---"
            Write-Host $stderrText
            Write-Host "--- end stderr ---"
        }
        Remove-Item $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

} finally {
    Write-Host "EXIT: $exit"
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}

exit $exit
