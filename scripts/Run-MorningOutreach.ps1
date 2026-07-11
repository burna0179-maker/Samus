<#
.SYNOPSIS
  Run the controlled morning outreach batch against today's call-list CSV.

.DESCRIPTION
  Wraps ``python -m backend.outreach.morning_batch`` with the secret-handling +
  approval-gate pattern the other Samus operator scripts use (mirrors
  Run-OutreachDaily.ps1's DPAPI pull and Start-MorningDial.ps1's -Live gate):

    1. Pull SendGridApiKey, SendgridFromEmail, OpenAiApiKey (+ AnthropicApiKey
       when present) from DPAPI (Scope=Samus). Set them ONLY on this process's
       env, then scrub every one in the finally block.
    2. Pin EMAIL_BACKEND=sendgrid, SAMUS_ARTIFACT_ROOT, SAMUS_ENV=production.
    3. Default to DRY-RUN so the operator approves before any real send.
       Pass -Live to actually send.
    4. Forward --csv / --max-send / --min-score / --subject to the Python CLI.

  This batch sources prospects from the Google-Places daily call list (NOT
  Apollo, which is 403 on the free plan). It reuses Samus's compliant compose +
  send path: every body carries the CAN-SPAM footer, suppression is enforced,
  and the run is hard-capped (default 10).

  Prereqs (one-time):

    Set-HfSecret -Scope Samus -Name SendGridApiKey
    Set-HfSecret -Scope Samus -Name SendgridFromEmail
    Set-HfSecret -Scope Samus -Name OpenAiApiKey

.PARAMETER Live
  Actually send. Default = dry-run (composes + prints every email, sends nothing).

.PARAMETER MaxSend
  Hard cap on emails sent this run. Default 10.

.PARAMETER MinScore
  Minimum lead_score for a row to be included. Default 70.

.PARAMETER Csv
  Override the CSV path. Default is today's call_list_YYYY-MM-DD.csv under
  <artifact_root>/daily_calls/.

.PARAMETER Subject
  Subject template; {company} is filled per recipient.

.EXAMPLE
  .\Run-MorningOutreach.ps1                       # dry-run, top 10 hot rows
  .\Run-MorningOutreach.ps1 -Live                 # live send, cap 10
  .\Run-MorningOutreach.ps1 -Live -MaxSend 5      # live send, cap 5
#>

[CmdletBinding()]
param(
    [Parameter()][switch]$Live,
    [Parameter()][int]$MaxSend = 10,
    [Parameter()][int]$MinScore = 70,
    [Parameter()][string]$Csv,
    [Parameter()][string]$Subject = 'Quick question about {company}'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    # Worktrees share the main checkout's venv.
    $mainVenvPython = 'D:\Hustleforge\Samus\.venv\Scripts\python.exe'
    if (Test-Path $mainVenvPython) {
        $venvPython = $mainVenvPython
    } else {
        throw "venv python not found at $venvPython or $mainVenvPython. Run scripts\Bootstrap-Samus.ps1 first."
    }
}

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Shared secrets module not found at $sharedModule"
}
Import-Module $sharedModule -Force

# --- artifact root + CSV resolution (validate BEFORE decrypting secrets) ------
$artifactRoot = if ($env:SAMUS_ARTIFACT_ROOT) { $env:SAMUS_ARTIFACT_ROOT } else { 'D:\Hustleforge\Samus\.data\host_artifacts' }
$resolvedCsv = if ($Csv) { $Csv } else {
    $today = (Get-Date).ToString('yyyy-MM-dd')
    Join-Path $artifactRoot "daily_calls\call_list_$today.csv"
}
if (-not (Test-Path $resolvedCsv)) {
    throw "Call list CSV not found: $resolvedCsv -- prospecting workcell must run first."
}
$lineCount = (Get-Content $resolvedCsv | Measure-Object -Line).Lines
if ($lineCount -le 1) {
    throw "Call list CSV is empty (only a header row): $resolvedCsv -- aborting to avoid a zero-send run."
}
Write-Host "==> CSV: $resolvedCsv ($($lineCount - 1) data rows)"

# --- required secrets ---------------------------------------------------------
# For a LIVE send, SendGridApiKey is mandatory. In dry-run everything is
# optional (nothing is sent), so missing secrets only warn.
if ($Live -and -not (Test-HfSecret -Scope Samus -Name SendGridApiKey)) {
    throw "SendGridApiKey not in Samus DPAPI store. Run: Set-HfSecret -Scope Samus -Name SendGridApiKey"
}

# --- build python argv --------------------------------------------------------
$pyArgs = @(
    '-m', 'backend.outreach.morning_batch',
    '--csv', $resolvedCsv,
    '--max-send', $MaxSend,
    '--min-score', $MinScore,
    '--subject', $Subject
)
if (-not $Live) { $pyArgs += '--dry-run' }

Write-Host ""
if ($Live) {
    Write-Host "==> Running LIVE morning outreach (emails WILL be sent, cap $MaxSend)" -ForegroundColor Red
} else {
    Write-Host "==> Running DRY-RUN morning outreach (no email sent; pass -Live to send)" -ForegroundColor Cyan
}
Write-Host ""

# --- env push (always restored in finally, even on throw) ---------------------
$exported = @()
try {
    Set-Item -Path 'Env:PYTHONIOENCODING' -Value 'utf-8'; $exported += 'PYTHONIOENCODING'
    Set-Item -Path 'Env:EMAIL_BACKEND'    -Value 'sendgrid'; $exported += 'EMAIL_BACKEND'
    Set-Item -Path 'Env:SAMUS_ENV'        -Value 'production'; $exported += 'SAMUS_ENV'
    Set-Item -Path 'Env:SAMUS_ARTIFACT_ROOT' -Value $artifactRoot; $exported += 'SAMUS_ARTIFACT_ROOT'

    if (Test-HfSecret -Scope Samus -Name SendGridApiKey) {
        Set-Item -Path 'Env:SENDGRID_API_KEY' -Value (Get-HfSecret -Scope Samus -Name SendGridApiKey)
        $exported += 'SENDGRID_API_KEY'
    } elseif (-not $Live) {
        Write-Warning "SendGridApiKey missing (dry-run continues; live send would abort)."
    }
    if (Test-HfSecret -Scope Samus -Name SendgridFromEmail) {
        Set-Item -Path 'Env:SENDGRID_FROM_EMAIL' -Value (Get-HfSecret -Scope Samus -Name SendgridFromEmail)
        $exported += 'SENDGRID_FROM_EMAIL'
    }
    if (Test-HfSecret -Scope Samus -Name OpenAiApiKey) {
        Set-Item -Path 'Env:OPENAI_API_KEY' -Value (Get-HfSecret -Scope Samus -Name OpenAiApiKey)
        $exported += 'OPENAI_API_KEY'
    }
    if (Test-HfSecret -Scope Samus -Name AnthropicApiKey) {
        Set-Item -Path 'Env:ANTHROPIC_API_KEY' -Value (Get-HfSecret -Scope Samus -Name AnthropicApiKey)
        $exported += 'ANTHROPIC_API_KEY'
    }
    # Ledger integrity: the hash-chained audit ledger fails closed without this
    # in production, so a live send records only to the local JSONL, not the
    # tamper-evident ledger. Seed it from DPAPI (same mapping Start-SamusStack
    # uses) so sends are captured in the canonical ledger.
    if (Test-HfSecret -Scope Samus -Name SharedHmacKey) {
        Set-Item -Path 'Env:SAMUS_SHARED_HMAC_KEY' -Value (Get-HfSecret -Scope Samus -Name SharedHmacKey)
        $exported += 'SAMUS_SHARED_HMAC_KEY'
    } elseif ($Live) {
        Write-Warning "SharedHmacKey missing -- audit ledger will stay unavailable on this live send."
    }

    Push-Location $samusRoot
    try {
        & $venvPython @pyArgs
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    foreach ($var in $exported) {
        Remove-Item -Path "Env:$var" -ErrorAction SilentlyContinue
    }
}

if ($exitCode -ne 0) {
    Write-Warning "Morning outreach exited with code $exitCode"
    exit $exitCode
}
