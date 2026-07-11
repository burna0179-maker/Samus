<#
.SYNOPSIS
  Run the production-failure monitor and email an alert to the brief recipient
  ONLY when the health state changes (new failure, or recovery).

.DESCRIPTION
  Wraps `python -m backend.observability.production_health_notify` with the
  DPAPI secret-handling used by the other Samus scripts. Intended to fire every
  ~15 minutes so a silent production failure (e.g. an expired Gmail OAuth token
  stalling the inbox poll while outbound keeps sending) surfaces within minutes
  instead of days.

  The notify module is state-change throttled: a persistent failure is emailed
  ONCE, not every cycle. Recovery sends one "resolved" note.

  IMPORTANT (host script-loading policy): launch this wrapper via
  `powershell.exe -Command "& <script>"`, NOT `powershell -File <script>`. The
  -File path fails to run it on this host (no transcript, nonzero exit, Python
  never runs) -- apparently the host's ConstrainedLanguage / WDAC script-loading
  policy; the call operator (&) runs it correctly. See
  Register-ProductionHealthSchedule.ps1. As belt-and-suspenders for
  ConstrainedLanguage this wrapper is also straight-line -- no top-level function
  definitions, no non-allowlisted .NET calls. A scheduled-task process is
  disposable, so env vars are set directly (no push/restore dance needed).

  Secrets read from DPAPI (Scope=Samus):
    SendgridApiKey     -> SENDGRID_API_KEY        (alert email channel)
    SendgridFromEmail  -> SENDGRID_FROM_EMAIL     (verified sender)
    GmailInboxEmail    -> SAMUS_GMAIL_INBOX_EMAIL (so the OAuth-token check runs)

  Exit code:
    0 - dispatch completed (alerted / recovered / unchanged / no_recipient)
    1 - the alert email send failed (next cycle retries)

.PARAMETER EmailTo
  Override the alert recipient. Default: env SAMUS_PRODUCTION_HEALTH_EMAIL_TO.
#>

[CmdletBinding()]
param(
    [Parameter()][string]$EmailTo$EmailTo = $env:SAMUS_PRODUCTION_HEALTH_EMAIL_TO
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = (Resolve-Path (Join-Path $here '..')).Path
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Run from Samus repo root with .venv installed."
}

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Required secrets module not found at $sharedModule."
}
Import-Module $sharedModule -Force

# --- transcript capture (best-effort; hidden runs leave no console otherwise) -
$logDir = Join-Path $samusRoot 'logs\production_health'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir ("production_health_$((Get-Date).ToString('yyyy-MM-dd_HHmmss')).log")
$transcriptStarted = $false
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true
} catch {
    Write-Warning "Start-Transcript failed (continuing without log): $_"
}

# Bounded rotation: a 15-min cadence makes ~96 logs/day; keep ~200 (two days).
try {
    Get-ChildItem $logDir -Filter 'production_health_*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 200 |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { Write-Warning "log rotation failed: $_" }

$exit = 1
try {
    # --- email channel secrets (both required to actually send an alert) -----
    $emailReady = $true
    if (Test-HfSecret -Scope Samus -Name SendgridApiKey) {
        $env:SENDGRID_API_KEY = Get-HfSecret -Scope Samus -Name SendgridApiKey
    } else {
        Write-Warning "SendgridApiKey not in DPAPI -- alert email will fail. Run: Set-HfSecret -Scope Samus -Name SendgridApiKey"
        $emailReady = $false
    }
    if (Test-HfSecret -Scope Samus -Name SendgridFromEmail) {
        $env:SENDGRID_FROM_EMAIL = Get-HfSecret -Scope Samus -Name SendgridFromEmail
    } elseif (-not $env:SENDGRID_FROM_EMAIL) {
        Write-Warning "SendgridFromEmail not in DPAPI or env -- alert email will fail. Run: Set-HfSecret -Scope Samus -Name SendgridFromEmail"
        $emailReady = $false
    }
    if ($emailReady) {
        $env:SAMUS_BRIEF_EMAIL_TO = $EmailTo
    }

    # --- Gmail identity so the OAuth-token check actually runs ---------------
    # Without SAMUS_GMAIL_INBOX_EMAIL the token check reports "not configured"
    # and never sees an expired token. Mirror Poll-Inbox.ps1.
    if (Test-HfSecret -Scope Samus -Name GmailInboxEmail) {
        $env:SAMUS_GMAIL_INBOX_EMAIL = Get-HfSecret -Scope Samus -Name GmailInboxEmail
    } else {
        Write-Warning "GmailInboxEmail not in DPAPI -- OAuth-token check reports 'not configured'. Run: Set-HfSecret -Scope Samus -Name GmailInboxEmail"
    }
    if (-not $env:SAMUS_GMAIL_OAUTH_TOKEN_PATH) {
        $env:SAMUS_GMAIL_OAUTH_TOKEN_PATH = Join-Path $samusRoot '.data\host_artifacts\intake\gmail_oauth_token.json'
    }

    # --- runtime posture: match the production stack + host artifact layout --
    if (-not $env:SAMUS_ENV) { $env:SAMUS_ENV = 'production' }
    if (-not $env:SAMUS_ARTIFACT_ROOT) {
        $env:SAMUS_ARTIFACT_ROOT = Join-Path $samusRoot '.data\host_artifacts'
    }
    $env:PYTHONIOENCODING = 'utf-8'

    # --- invoke python notifier ---------------------------------------------
    # PS 5.1 wraps native-exe stderr as a NativeCommandError under EAP=Stop,
    # which throws even on exit 0; localize EAP to Continue and redirect stderr
    # to a sidecar folded into the transcript afterward.
    $stderrPath = "$logPath.err"
    $ErrorActionPreference = 'Continue'
    Push-Location $samusRoot
    try {
        & $venvPython -m backend.observability.production_health_notify 2>$stderrPath
        $exit = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = 'Stop'
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
    if ($transcriptStarted) { try { Stop-Transcript | Out-Null } catch {} }
}

exit $exit
