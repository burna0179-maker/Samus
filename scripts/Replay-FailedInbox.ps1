<#
.SYNOPSIS
  Replay inbound emails that failed due to missing AWS credentials.

.DESCRIPTION
  Wraps ``python scripts/replay_failed_inbox.py`` with the same DPAPI
  secret-handling pattern as Poll-Inbox.ps1. Re-fetches each failed
  message from Gmail by its gmail_id and processes it into CRM
  artifacts + operator tasks.

.PARAMETER DryRun
  Count failures without re-processing.

.PARAMETER Batch
  Process at most N messages (0 = all, default).

.EXAMPLE
  .\Replay-FailedInbox.ps1 --DryRun
  .\Replay-FailedInbox.ps1
  .\Replay-FailedInbox.ps1 -Batch 50
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [int]$Batch = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython."
}

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Required secrets module not found at $sharedModule."
}
Import-Module $sharedModule -Force

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

# Gmail OAuth
if (Test-HfSecret -Scope Samus -Name GmailInboxEmail) {
    Push-EnvVar 'SAMUS_GMAIL_INBOX_EMAIL' (Get-HfSecret -Scope Samus -Name GmailInboxEmail)
}
if (Test-HfSecret -Scope Samus -Name GmailOauthClientId) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_CLIENT_ID' (Get-HfSecret -Scope Samus -Name GmailOauthClientId)
}
if (Test-HfSecret -Scope Samus -Name GmailOauthClientSecret) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_CLIENT_SECRET' (Get-HfSecret -Scope Samus -Name GmailOauthClientSecret)
}

# AWS (required for CRM writes)
if ((Test-HfSecret -Scope Samus -Name AwsAccessKeyId) -and
    (Test-HfSecret -Scope Samus -Name AwsSecretAccessKey)) {
    Push-EnvVar 'AWS_ACCESS_KEY_ID'     (Get-HfSecret -Scope Samus -Name AwsAccessKeyId)
    Push-EnvVar 'AWS_SECRET_ACCESS_KEY' (Get-HfSecret -Scope Samus -Name AwsSecretAccessKey)
    if (-not $env:AWS_REGION)         { Push-EnvVar 'AWS_REGION'         'us-west-1' }
    if (-not $env:AWS_DEFAULT_REGION) { Push-EnvVar 'AWS_DEFAULT_REGION' 'us-west-1' }
} else {
    throw "AWS creds not in DPAPI -- replay would fail the same way. Seed them first."
}

# Stripe (optional, for billing summary)
try {
    if (Test-HfSecret -Scope Samus -Name StripeApiKey) {
        Push-EnvVar 'STRIPE_API_KEY' (Get-HfSecret -Scope Samus -Name StripeApiKey)
    }
} catch { Write-Warning "Stripe key load failed: $_" }

if (-not $env:SAMUS_ENV) { Push-EnvVar 'SAMUS_ENV' 'production' }
Push-EnvVar 'PYTHONIOENCODING' 'utf-8'

if (-not $env:SAMUS_GMAIL_OAUTH_TOKEN_PATH) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_TOKEN_PATH' (Join-Path $samusRoot '.data\host_artifacts\intake\gmail_oauth_token.json')
}

$pyArgs = @()
if ($DryRun) { $pyArgs += '--dry-run' }
if ($Batch -gt 0) { $pyArgs += '--batch'; $pyArgs += "$Batch" }

$prevConsoleEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
Push-Location $samusRoot
try {
    & $venvPython scripts/replay_failed_inbox.py @pyArgs
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
    Restore-EnvVars
    [Console]::OutputEncoding = $prevConsoleEnc
}

Write-Host "EXIT: $exit"
exit $exit
