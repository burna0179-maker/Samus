<#
.SYNOPSIS
  Sync the reviewed Morgan snapshot (system prompt + voicemail) to the LIVE
  Vapi assistant.

.DESCRIPTION
  The canonical, operator-reviewed copy of Morgan's system prompt + voicemail
  settings lives in backend/voice/assistant_configs/morgan_sdr.json. The LIVE
  Vapi assistant can drift from it (dashboard hand-edits, or the mid-session
  prompt patcher in backend.voice.call_session_monitor). This script pushes the
  snapshot's system prompt (and, with -Voicemail, the voicemail settings) back
  onto the live assistant so the reviewed wording is what actually runs.

  It pulls VapiApiKey + VapiAssistantId out of the Samus DPAPI store, sets them
  as env vars for the child python process, and invokes
  `python -m backend.voice.sync_assistant`, which builds a MINIMAL PATCH body
  (system prompt + optional voicemail only - never server/voice/transcriber).

  SAFETY: DRY-RUN by default. Without -Live (alias -Apply) it only prints the
  diff of live-vs-snapshot and sends NOTHING to Vapi. You must pass -Live to
  actually PATCH the assistant.

.PARAMETER Live
  Actually PATCH the live Vapi assistant. Alias: -Apply. Omit for a dry-run.

.PARAMETER Voicemail
  Also sync voicemailMessage + voicemailDetection from the snapshot (default:
  system prompt only).

.PARAMETER Python
  Path to the host venv python. Defaults to the Samus repo .venv.

.EXAMPLE
  # dry-run - show the diff, send nothing:
  .\Sync-MorganAssistant.ps1

.EXAMPLE
  # actually push the reviewed prompt to the live assistant:
  .\Sync-MorganAssistant.ps1 -Live

.EXAMPLE
  # push prompt + voicemail settings:
  .\Sync-MorganAssistant.ps1 -Voicemail -Live
#>

[CmdletBinding()]
param(
    [Parameter()][Alias('Apply')][switch]$Live,
    [Parameter()][switch]$Voicemail,
    [Parameter()][string]$Python = 'D:\Hustleforge\Samus\.venv\Scripts\python.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot   # D:\Hustleforge\Samus
if (-not (Test-Path $Python)) {
    throw "python not found at $Python - pass -Python (venv python)."
}

# --- Pull Vapi credentials from the Samus DPAPI store ----------------------
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) { throw "secrets module not found at $sharedModule" }
Import-Module $sharedModule -Force

foreach ($name in 'VapiApiKey', 'VapiAssistantId') {
    if (-not (Test-HfSecret -Scope Samus -Name $name)) {
        throw "DPAPI secret '$name' (Scope=Samus) is missing - cannot sync the assistant."
    }
}
$key  = Get-HfSecret -Scope Samus -Name VapiApiKey
$asst = Get-HfSecret -Scope Samus -Name VapiAssistantId

# --- Invoke the python sync module -----------------------------------------
# The module reads VAPI_API_KEY / VAPI_ASSISTANT_ID from settings (env). It is
# a DRY-RUN unless we pass --apply; we only pass --apply when -Live is set.
$env:VAPI_API_KEY = $key
$env:VAPI_ASSISTANT_ID = $asst

$pyArgs = @('-m', 'backend.voice.sync_assistant')
if ($Voicemail) { $pyArgs += '--voicemail' }
if ($Live) {
    Write-Host 'Mode: LIVE - will PATCH the assistant.' -ForegroundColor Yellow
    $pyArgs += '--apply'
} else {
    Write-Host 'Mode: DRY-RUN - no changes will be sent. Pass -Live to apply.' -ForegroundColor Cyan
}

try {
    Push-Location $repoRoot
    & $Python @pyArgs
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item Env:\VAPI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\VAPI_ASSISTANT_ID -ErrorAction SilentlyContinue
}

exit $code
