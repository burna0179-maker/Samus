<#
.SYNOPSIS
  Run one open-no-click nudge watcher pass (hourly scheduled-task entrypoint).

.DESCRIPTION
  Hydrates SendGrid + artifact-root env from the Samus DPAPI store, then
  invokes `python -m backend.outreach.open_no_click_cli tick` inside the
  samus-voice container (the one with the shared volume + faster_whisper-
  unaffected python). The watcher reads the SendGrid event journal already
  written by heat/service.py and fires a nudge when a tracked send crosses
  the dwell window with an open and no click.

  Wired-DORMANT: the watcher updates state (closes records on click,
  records open timestamps) every tick. Firing the actual nudge email
  requires OUTREACH_OPEN_NO_CLICK_NUDGE_ENABLED=true in the samus-voice
  container env. With the flag off the watcher annotates "would_nudge_at"
  on the record and skips the send.

.PARAMETER DryRun
  Pass --dry-run to the tick — never sends, even if the flag is on.

.PARAMETER ForceFire
  Pass --force-fire — operator override, send regardless of the flag.

.EXAMPLE
  Run-NudgeWatch.ps1                  # normal tick (flag-gated send)
  Run-NudgeWatch.ps1 -DryRun          # plan only, no send
  Run-NudgeWatch.ps1 -ForceFire       # send regardless of flag
#>
param(
    [Parameter()][switch]$DryRun,
    [Parameter()][switch]$ForceFire
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($DryRun -and $ForceFire) {
    throw "Pick one: -DryRun OR -ForceFire (not both)."
}

$logDir = 'D:\Hustleforge\Samus\logs\nudge_watch'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$ts = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$logPath = Join-Path $logDir "nudge_watch_${ts}.json"

$pyArgs = @('-m', 'backend.outreach.open_no_click_cli', 'tick')
if ($DryRun)    { $pyArgs += '--dry-run' }
if ($ForceFire) { $pyArgs += '--force-fire' }

# The watcher runs INSIDE the samus-voice container — it already has the
# correct PYTHONPATH, the named samus-data volume mounted at /opt/samus/data,
# and (when armed) the SendGrid env vars baked into the image. No host-side
# DPAPI hydration needed because the container env was set at compose-up time.
Write-Host "tick @ $ts (dry_run=$DryRun, force_fire=$ForceFire)"
$stdout = & docker exec samus-voice python @pyArgs 2>&1 | Out-String
$exit = $LASTEXITCODE

$record = @{
    ts        = $ts
    dry_run   = [bool]$DryRun
    force_fire= [bool]$ForceFire
    exit_code = $exit
    stdout    = $stdout
}
$record | ConvertTo-Json -Depth 6 | Out-File -FilePath $logPath -Encoding utf8

Write-Host $stdout
exit $exit
