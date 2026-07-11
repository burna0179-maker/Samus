<#
.SYNOPSIS
  Drain one batch of unread messages from the Samus Gmail company inbox into
  the CRM as artifacts + operator tasks.

.DESCRIPTION
  Wraps ``python -m backend.intake.gmail_poller`` with the secret-handling
  pattern used by Send-Morning.ps1. Pulls credentials from DPAPI
  (Scope=Samus) and sets them only on the child Python process's env;
  every env mutation is reversed in the finally block so this script does
  not leak state into the caller's shell.

  Secrets read (poller is a no-op if any are missing):

    GmailInboxEmail        -> SAMUS_GMAIL_INBOX_EMAIL          (e.g. samushustleforge@gmail.com)
    GmailOauthClientId     -> SAMUS_GMAIL_OAUTH_CLIENT_ID      (Google Cloud OAuth client id)
    GmailOauthClientSecret -> SAMUS_GMAIL_OAUTH_CLIENT_SECRET  (Google Cloud OAuth client secret)
    StripeApiKey           -> STRIPE_API_KEY                   (per-customer billing summary)

  A refresh token must already exist at $env:SAMUS_GMAIL_OAUTH_TOKEN_PATH
  (default /opt/samus/data/intake/gmail_oauth_token.json). Run
  Authorize-Gmail.ps1 once to perform the OAuth consent flow and write it.

  The Stripe key is optional: when missing the billing-summary line on
  each task description reads "billing lookup failed: stripe_api_key_unset"
  but the artifact + task are still created.

  Exit code:
    0 - drain completed (any count of processed/duplicates/failed) OR poller disabled
    1 - Gmail API connection / auth failure (whole pass failed before processing anything)

.EXAMPLE
  .\Poll-Inbox.ps1

.EXAMPLE
  # One-time setup -- store the secrets before the first run
  Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1
  Set-HfSecret -Scope Samus -Name GmailInboxEmail
  Set-HfSecret -Scope Samus -Name GmailOauthClientId
  Set-HfSecret -Scope Samus -Name GmailOauthClientSecret
  # Then complete OAuth consent once:
  .\Authorize-Gmail.ps1
#>

[CmdletBinding()]
param()

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

# --- transcript capture ------------------------------------------------------
# The scheduled task runs powershell hidden, so a failure leaves NO evidence --
# which is exactly how the OAuth-token expiry went unnoticed for ~4 days.
# Capture every run to logs/inbox_poll/ so the next failure is one file away.
$logDir = Join-Path $samusRoot 'logs\inbox_poll'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir ("inbox_poll_$((Get-Date).ToString('yyyy-MM-dd_HHmmss')).log")
$transcriptStarted = $false
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true
} catch {
    Write-Warning "Start-Transcript failed (continuing without log): $_"
}
# Bounded rotation: 10-min cadence => ~144 logs/day; keep the most recent 300.
try {
    Get-ChildItem $logDir -Filter 'inbox_poll_*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 300 |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { Write-Warning "log rotation failed: $_" }

# --- env helpers (push + restore in finally) ---------------------------------

$envBackup = @{}

function Push-EnvVar([string]$Name, [string]$Value) {
    if (-not $envBackup.ContainsKey($Name)) {
        $envBackup[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    Set-Item -Path "Env:$Name" -Value $Value
}

function Restore-EnvVars {
    foreach ($k in $envBackup.Keys) {
        $v = $envBackup[$k]
        if ($null -eq $v) {
            Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$k" -Value $v
        }
    }
}

# --- load secrets ------------------------------------------------------------

# Gmail OAuth credentials -- all three required for the poller to do anything;
# if any is missing, drain_once() reports enabled=False and exits 0.
if (Test-HfSecret -Scope Samus -Name GmailInboxEmail) {
    Push-EnvVar 'SAMUS_GMAIL_INBOX_EMAIL' (Get-HfSecret -Scope Samus -Name GmailInboxEmail)
} else {
    Write-Warning "GmailInboxEmail not in DPAPI -- poller will report disabled. Run: Set-HfSecret -Scope Samus -Name GmailInboxEmail"
}
if (Test-HfSecret -Scope Samus -Name GmailOauthClientId) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_CLIENT_ID' (Get-HfSecret -Scope Samus -Name GmailOauthClientId)
} else {
    Write-Warning "GmailOauthClientId not in DPAPI -- poller will report disabled. Run: Set-HfSecret -Scope Samus -Name GmailOauthClientId"
}
if (Test-HfSecret -Scope Samus -Name GmailOauthClientSecret) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_CLIENT_SECRET' (Get-HfSecret -Scope Samus -Name GmailOauthClientSecret)
} else {
    Write-Warning "GmailOauthClientSecret not in DPAPI -- poller will report disabled. Run: Set-HfSecret -Scope Samus -Name GmailOauthClientSecret"
}

# Stripe key -- optional; when missing the per-task billing summary degrades to
# "billing lookup failed: stripe_api_key_unset" inline. Artifact + task are
# still created.
try {
    if (Test-HfSecret -Scope Samus -Name StripeApiKey) {
        Push-EnvVar 'STRIPE_API_KEY' (Get-HfSecret -Scope Samus -Name StripeApiKey)
    }
} catch {
    Write-Warning "Stripe key load failed -- per-task billing summary will degrade: $_"
}

# --- CRM / DynamoDB creds (REQUIRED for processing) --------------------------
# The poller writes each inbound message as a CRM artifact + operator task to
# DynamoDB. Without AWS creds every message fails 'ddb_put_failed: Unable to
# locate credentials' -- and because the poller marks a message READ even on a
# processing failure (poison-pill guard), those messages are silently dropped
# (marked read + ledgered as failed, never reprocessed), INCLUDING opt-outs that
# then never reach the suppression list. Mirrors Send-Morning.ps1's block.
if ((Test-HfSecret -Scope Samus -Name AwsAccessKeyId) -and
    (Test-HfSecret -Scope Samus -Name AwsSecretAccessKey)) {
    Push-EnvVar 'AWS_ACCESS_KEY_ID'     (Get-HfSecret -Scope Samus -Name AwsAccessKeyId)
    Push-EnvVar 'AWS_SECRET_ACCESS_KEY' (Get-HfSecret -Scope Samus -Name AwsSecretAccessKey)
    if (-not $env:AWS_REGION)         { Push-EnvVar 'AWS_REGION'         'us-west-1' }
    if (-not $env:AWS_DEFAULT_REGION) { Push-EnvVar 'AWS_DEFAULT_REGION' 'us-west-1' }
} else {
    Write-Warning "AWS creds (AwsAccessKeyId/AwsSecretAccessKey) not in DPAPI -- inbound CRM writes will FAIL and messages will be marked read + dropped. Run: Set-HfSecret -Scope Samus -Name AwsAccessKeyId  (and AwsSecretAccessKey)"
}

# Match the running stack's runtime env so fail-closed gates behave the same as
# the containers (they run SAMUS_ENV=production). Operator override respected.
if (-not $env:SAMUS_ENV) {
    Push-EnvVar 'SAMUS_ENV' 'production'
}

# UTF-8 for the child's stdout.
Push-EnvVar 'PYTHONIOENCODING' 'utf-8'

# Pin the OAuth token to a HOST path. The config default
# (/opt/samus/data/intake/gmail_oauth_token.json) is a CONTAINER path that
# does not resolve on a host run, so the poller could never load the token.
# Must match Authorize-Gmail.ps1 so consent writes where the poller reads.
# Honor an existing override if the operator set one.
if (-not $env:SAMUS_GMAIL_OAUTH_TOKEN_PATH) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_TOKEN_PATH' (Join-Path $samusRoot '.data\host_artifacts\intake\gmail_oauth_token.json')
}

# --- invoke python poller ----------------------------------------------------
# Default so a throw before the invocation still returns a code.
$exit = 1

$prevConsoleEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# CRITICAL: the poller logs to stderr (httpx INFO, the SAMUS_ENV notice, etc.).
# Under $ErrorActionPreference='Stop' (set at top) PS 5.1 wraps each native-exe
# stderr line as a terminating NativeCommandError -- so a fully successful drain
# still throws and the script exits nonzero (a FALSE failure Task Scheduler
# records as Last Result != 0). Localize EAP to 'Continue' around the call so
# the real python exit code is what propagates. Mirrors Send-Morning.ps1.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
Push-Location $samusRoot
try {
    & $venvPython -m backend.intake.gmail_poller
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
    Restore-EnvVars
    [Console]::OutputEncoding = $prevConsoleEnc
}

Write-Host "EXIT: $exit"
if ($transcriptStarted) { try { Stop-Transcript | Out-Null } catch {} }
exit $exit
