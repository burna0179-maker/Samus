<#
.SYNOPSIS
  One-time Gmail OAuth consent — opens the browser, captures the
  authorization code on a loopback URL, exchanges it for a refresh token,
  and persists the token JSON for the inbox poller to use.

.DESCRIPTION
  Wraps ``python -m backend.intake.gmail_oauth`` with DPAPI-loaded
  credentials. Run this once per Gmail account. Re-run only if:

    - the refresh token has been revoked (consent revoked at
      https://myaccount.google.com/permissions or OAuth client deleted
      in Google Cloud Console)
    - you want to switch to a different inbox account
    - the OAuth scope needs to change

  After this script succeeds, scripts/Poll-Inbox.ps1 will start working.

  Required secrets (set first with Set-HfSecret -Scope Samus -Name ...):

    GmailInboxEmail        e.g. samushustleforge@gmail.com
    GmailOauthClientId     Google Cloud Console -> APIs & Services
                           -> Credentials -> OAuth client (Desktop app)
    GmailOauthClientSecret (same)

  One-time Google Cloud setup checklist (~5 min, no admin needed):

    1. https://console.cloud.google.com -> create or select a project
    2. APIs & Services -> Library -> search "Gmail API" -> Enable
    3. OAuth consent screen -> External, fill name/email, ADD your inbox
       email to "Test users" (avoids the "app not verified" 7-day cap)
    4. Credentials -> Create credentials -> OAuth client ID
       -> Application type: Desktop app
       -> Note client_id + client_secret
    5. Set-HfSecret -Scope Samus -Name GmailOauthClientId
       Set-HfSecret -Scope Samus -Name GmailOauthClientSecret
    6. Run this script.

  Token storage:
    Default: %SAMUS_GMAIL_OAUTH_TOKEN_PATH% or
             /opt/samus/data/intake/gmail_oauth_token.json (will be created
             with parent directories as needed). Plain JSON on disk; treat
             the directory like any other secret store.

  Exit code:
    0 - consent completed, refresh token saved
    1 - any step failed (cancelled consent, network, code-exchange refusal)

.EXAMPLE
  .\Authorize-Gmail.ps1

.EXAMPLE
  # No browser available (SSH session) — print the URL and don't auto-open
  .\Authorize-Gmail.ps1 -NoOpenBrowser
#>

[CmdletBinding()]
param(
    [switch]$NoOpenBrowser,
    [Parameter()][int]$TimeoutSeconds = 300
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

$missing = @()
if (Test-HfSecret -Scope Samus -Name GmailInboxEmail) {
    Push-EnvVar 'SAMUS_GMAIL_INBOX_EMAIL' (Get-HfSecret -Scope Samus -Name GmailInboxEmail)
} else { $missing += 'GmailInboxEmail' }

if (Test-HfSecret -Scope Samus -Name GmailOauthClientId) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_CLIENT_ID' (Get-HfSecret -Scope Samus -Name GmailOauthClientId)
} else { $missing += 'GmailOauthClientId' }

if (Test-HfSecret -Scope Samus -Name GmailOauthClientSecret) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_CLIENT_SECRET' (Get-HfSecret -Scope Samus -Name GmailOauthClientSecret)
} else { $missing += 'GmailOauthClientSecret' }

if ($missing.Count -gt 0) {
    Restore-EnvVars
    Write-Error "Missing required DPAPI secrets: $($missing -join ', '). Set each with: Set-HfSecret -Scope Samus -Name <name>"
    exit 1
}

# UTF-8 for the child's stdout (the consent URL contains URL-encoded characters
# that render cleanly only in UTF-8).
Push-EnvVar 'PYTHONIOENCODING' 'utf-8'

# Pin the OAuth token to a HOST path. The config default
# (/opt/samus/data/intake/gmail_oauth_token.json) is a CONTAINER path that
# does not resolve on a host run; consent must write where Poll-Inbox.ps1
# later reads. Keep these two in lockstep. Honor an existing override.
if (-not $env:SAMUS_GMAIL_OAUTH_TOKEN_PATH) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_TOKEN_PATH' (Join-Path $samusRoot '.data\host_artifacts\intake\gmail_oauth_token.json')
}

# --- invoke python consent flow ---------------------------------------------

$prevConsoleEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$pyArgs = @('-m', 'backend.intake.gmail_oauth', '--timeout', $TimeoutSeconds)
if ($NoOpenBrowser) { $pyArgs += '--no-open-browser' }

Push-Location $samusRoot
try {
    & $venvPython @pyArgs
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    Restore-EnvVars
    [Console]::OutputEncoding = $prevConsoleEnc
}

exit $exit
