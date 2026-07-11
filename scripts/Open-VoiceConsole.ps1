<#
.SYNOPSIS
  Open the Samus operator voice console in the default browser, optionally
  bringing the stack up first if the voice workcell is down.

.DESCRIPTION
  Probes the voice workcell's /health endpoint:

    - Healthy            -> open /console in the default browser.
    - Down, no switch    -> print a clear instruction, exit 0 (no dead tab).
    - Down, -StartStackIfDown -> run Start-SamusStack.ps1 to bring the Samus
                           Docker stack up, poll /health until the voice
                           workcell answers, then open /console.

  Exits 0 even when the service can't be reached/started: a stopped service
  (or stopped Docker) is an operator-state issue, not a failure of this
  script, and a scheduled caller must not treat it as an error.

  The console (backend.voice.console) is gated by SAMUS_VOICE_CONSOLE_TOKEN.
  This script does NOT pass the token (a token in a URL leaks into browser
  history) -- the operator pastes it on the console's sign-in screen.

  Run standalone any time, or via Send-Morning.ps1 -OpenConsole (which the
  registered morning-brief task passes; see Register-MorningBriefSchedule.ps1).

.PARAMETER ConsoleUrl
  Full URL of the console page. Default http://localhost:8107/console -- the
  host port the compose stack publishes samus-voice on (127.0.0.1:8107). For a
  bare local `uvicorn backend.voice.app:app` use http://localhost:8080/console.

.PARAMETER StartStackIfDown
  If the health probe fails, run Start-SamusStack.ps1 to bring up the Samus
  Docker stack, then wait for the voice workcell before opening the console.
  Requires Docker Desktop to be running.

.PARAMETER Quiet
  Suppress the informational output. Warnings and errors still print.

.EXAMPLE
  .\Open-VoiceConsole.ps1

.EXAMPLE
  # Morning-routine behaviour: start the stack if it isn't already up.
  .\Open-VoiceConsole.ps1 -StartStackIfDown
#>

[CmdletBinding()]
param(
    [Parameter()][string]$ConsoleUrl = 'http://localhost:8107/console',
    [Parameter()][switch]$StartStackIfDown,
    [Parameter()][switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Info([string]$msg) {
    if (-not $Quiet) { Write-Host $msg }
}

# Derive the health URL from the console URL: same scheme/host/port, /health.
try {
    $uri = [Uri]$ConsoleUrl
    $healthUrl = '{0}://{1}:{2}/health' -f $uri.Scheme, $uri.Host, $uri.Port
} catch {
    Write-Warning "Invalid -ConsoleUrl '$ConsoleUrl': $_"
    exit 1
}

# Probe the voice workcell. Short timeout so a scheduled morning fire is never
# blocked on a dead port. Any non-200 / network error => treat as not up.
function Test-VoiceHealth {
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 4 `
            -UseBasicParsing -ErrorAction Stop
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (-not (Test-VoiceHealth)) {
    if (-not $StartStackIfDown) {
        Write-Warning "Voice workcell not reachable at $healthUrl."
        Write-Warning "Start the voice service, then browse to $ConsoleUrl"
        Write-Warning "(or re-run with -StartStackIfDown to bring the stack up first.)"
        exit 0
    }

    # -StartStackIfDown: bring the whole Samus stack up via the canonical
    # launcher (it wires every DPAPI secret into the compose env). The voice
    # console's call flow needs CRM + memory up too, so a full-stack start is
    # correct, not just `compose up samus-voice`.
    $startStack = Join-Path $PSScriptRoot 'Start-SamusStack.ps1'
    if (-not (Test-Path $startStack)) {
        Write-Warning "Start-SamusStack.ps1 not found at $startStack -- cannot auto-start."
        exit 0
    }
    Write-Info "Voice workcell down -- bringing up the Samus stack (Start-SamusStack.ps1)..."
    try {
        & $startStack
    } catch {
        Write-Warning "Start-SamusStack.ps1 failed: $_"
        Write-Warning "Is Docker Desktop running? Start it, then re-run."
        exit 0
    }

    # `compose up -d` returns once containers are CREATED, not healthy. Poll
    # /health until the voice app answers (it boots, then opens its ngrok
    # tunnel). Cap the wait so a stuck boot never hangs the morning routine.
    Write-Info "Stack started -- waiting for the voice workcell to answer /health..."
    $deadline = (Get-Date).AddSeconds(150)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        if (Test-VoiceHealth) { $up = $true; break }
    }
    if (-not $up) {
        Write-Warning "Voice workcell still not healthy after the wait."
        Write-Warning "Check:  docker compose ps  /  docker logs samus-voice"
        exit 0
    }
    Write-Info "Voice workcell is up."
}

Write-Info "Opening the operator console:"
Write-Info "  $ConsoleUrl"
Start-Process $ConsoleUrl
exit 0
