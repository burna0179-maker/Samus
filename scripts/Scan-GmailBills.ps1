<#
.SYNOPSIS
  Scan the Samus Gmail company inbox for vendor billing emails (invoices,
  receipts, statements, payment-declined notices) and compile a bills
  snapshot cross-referenced against the CODB registry.

.DESCRIPTION
  Wraps ``python -m backend.finance.gmail_bill_scan`` with the same
  DPAPI secret-handling pattern as Poll-Inbox.ps1. Pulls Gmail OAuth
  credentials from DPAPI (Scope=Samus) and sets them only on the child
  Python process's env; every env mutation is reversed in the finally
  block so this script never leaks state into the caller's shell.

  READ-ONLY BY NATURE: the scanner only lists + fetches Gmail messages
  (never marks read, modifies, or sends), so unlike Poll-Inbox.ps1 there
  is no -Live flag or idempotency ledger to worry about — running it
  repeatedly is always safe. It also NEVER writes codb_registry.yaml;
  it writes a separate JSON snapshot for operator review.

  Secrets read (scan is a no-op / clear-message exit if any are missing):

    GmailInboxEmail        -> SAMUS_GMAIL_INBOX_EMAIL
    GmailOauthClientId     -> SAMUS_GMAIL_OAUTH_CLIENT_ID
    GmailOauthClientSecret -> SAMUS_GMAIL_OAUTH_CLIENT_SECRET

  A refresh token must already exist at $env:SAMUS_GMAIL_OAUTH_TOKEN_PATH
  (default .data\host_artifacts\intake\gmail_oauth_token.json under the
  repo root, matching Poll-Inbox.ps1). Run Authorize-Gmail.ps1 once to
  perform the OAuth consent flow and write it. If the token is missing,
  the underlying Python module prints one clear line and exits 2 — this
  wrapper passes that exit code straight through.

.PARAMETER LookbackDays
  How many days back to search. Default 90.

.PARAMETER OutPath
  Where to write the JSON snapshot. Defaults to the module's own
  data/finance/gmail_bills_snapshot.json convention when omitted.

.EXAMPLE
  .\Scan-GmailBills.ps1

.EXAMPLE
  .\Scan-GmailBills.ps1 -LookbackDays 30 -OutPath D:\tmp\bills.json
#>

[CmdletBinding()]
param(
    [int]$LookbackDays = 90,
    # Default to the host artifacts tree — the module's own default derives
    # from the container-convention /opt/samus/... path, which on a Windows
    # host resolves drive-relative (D:\opt\...): wrong tree, easy to miss.
    [string]$OutPath = 'D:\Hustleforge\Samus\.data\host_artifacts\finance\gmail_bills_snapshot.json'
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

if (Test-HfSecret -Scope Samus -Name GmailInboxEmail) {
    Push-EnvVar 'SAMUS_GMAIL_INBOX_EMAIL' (Get-HfSecret -Scope Samus -Name GmailInboxEmail)
} else {
    Write-Warning "GmailInboxEmail not in DPAPI -- scan will fail to connect. Run: Set-HfSecret -Scope Samus -Name GmailInboxEmail"
}
if (Test-HfSecret -Scope Samus -Name GmailOauthClientId) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_CLIENT_ID' (Get-HfSecret -Scope Samus -Name GmailOauthClientId)
} else {
    Write-Warning "GmailOauthClientId not in DPAPI -- scan will fail to connect. Run: Set-HfSecret -Scope Samus -Name GmailOauthClientId"
}
if (Test-HfSecret -Scope Samus -Name GmailOauthClientSecret) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_CLIENT_SECRET' (Get-HfSecret -Scope Samus -Name GmailOauthClientSecret)
} else {
    Write-Warning "GmailOauthClientSecret not in DPAPI -- scan will fail to connect. Run: Set-HfSecret -Scope Samus -Name GmailOauthClientSecret"
}

# UTF-8 for the child's stdout.
Push-EnvVar 'PYTHONIOENCODING' 'utf-8'

# Pin the OAuth token to a HOST path -- same convention + same file as
# Poll-Inbox.ps1 / Authorize-Gmail.ps1 (the config default is a CONTAINER
# path that does not resolve on a host run).
if (-not $env:SAMUS_GMAIL_OAUTH_TOKEN_PATH) {
    Push-EnvVar 'SAMUS_GMAIL_OAUTH_TOKEN_PATH' (Join-Path $samusRoot '.data\host_artifacts\intake\gmail_oauth_token.json')
}

# --- invoke python scanner ----------------------------------------------------

$prevConsoleEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$pyArgs = @('-m', 'backend.finance.gmail_bill_scan', '--lookback-days', $LookbackDays)
if ($OutPath) {
    $outDir = Split-Path -Parent $OutPath
    if ($outDir -and -not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }
    $pyArgs += @('--out', $OutPath)
}

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
