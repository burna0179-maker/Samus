<#
.SYNOPSIS
  Run one post-call reconciliation sweep — backfill end_of_call events for
  today's dialed calls whose Vapi end-of-call-report webhook never arrived.

.DESCRIPTION
  Gap-8 recovery mechanism. The first two live calls (2026-06-30) BOTH had
  their end-of-call-report webhook dropped (Vapi computed the analysis but
  never delivered it — likely the ngrok free-tier tunnel or a Vapi delivery
  gap). Without backfill, those outcomes are invisible to the session
  monitor, pattern aggregator, briefing, and CRM. This sweep reads today's
  initiated dials, finds the ones missing an end_of_call event, fetches each
  call from the Vapi REST API (source of truth — it holds the analysis), and
  writes the missing event. Idempotent + fail-open.

  Runs INSIDE samus-voice (shares the volume + the Vapi key baked into the
  image). No host-side DPAPI hydration needed.

  Registered 2026-06-30 as \Hustleforge\Samus-CallReconcile: every 20 min,
  principal PC / S4U (runs whether logged on or not), RunLevel Highest. Example:
    $action = New-ScheduledTaskAction -Execute powershell.exe -Argument `
      '-NonInteractive -ExecutionPolicy Bypass -File "D:\Hustleforge\Samus\scripts\Run-CallReconcile.ps1"'
    $trigger = New-ScheduledTaskTrigger -Once -At 7am `
      -RepetitionInterval (New-TimeSpan -Minutes 20) -RepetitionDuration ([TimeSpan]::FromDays(1))
    $principal = New-ScheduledTaskPrincipal -UserId "PC" -LogonType S4U -RunLevel Highest
    Register-ScheduledTask -TaskName "\Hustleforge\Samus-CallReconcile" -Action $action `
      -Trigger $trigger -Principal $principal -Force

  CRITICAL: the switch is -NonInteractive, NOT -NoInteractive. powershell.exe
  has no -NoInteractive; the typo makes it exit 0x80070002 (file-not-found)
  without running the script, so the task "completes" while doing nothing. This
  bit us on 2026-06-30 (the task silently never ran for its whole first life).

  Note: reconcile shells into samus-voice via `docker exec`, so Docker Desktop
  must be up (it runs in the interactive console session). When Docker is down,
  a sweep fails-soft and the next 20-min run catches up (reconcile is idempotent).
#>
param()

# NOTE: EAP stays at 'Continue', NOT 'Stop'. Under 'Stop', a single line of
# docker/python STDERR is promoted to a terminating NativeCommandError (PS5.1
# trap) that aborts the script BEFORE the log write — which is exactly how this
# sweep went dark from 2026-07-01 to 2026-07-03: every run threw early, wrote no
# log, and Task Scheduler reported a bare failure with nothing to diagnose. We
# now (a) never let an early throw skip the log, and (b) capture STDERR into the
# log so the NEXT failure is visible. All real success/failure is decided by
# explicit $LASTEXITCODE checks, not by EAP.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$logDir = 'D:\Hustleforge\Samus\logs\call_reconcile'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$ts = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$logPath = Join-Path $logDir "call_reconcile_${ts}.json"

Write-Host "reconcile sweep @ $ts"

# Everything runs inside try/finally so the log is ALWAYS written — even on an
# early failure (docker missing, daemon unreachable, container down). The log is
# the whole point of a scheduled sweep you never watch live.
$exit = 1
$stdout = ""
$dockerErr = ""
$drainOut = ""
$fatal = ""
# Keep the stderr temp in $logDir (known to exist) rather than $env:TEMP — the
# S4U scheduled-task environment can have TEMP unset, which would send this to
# the filesystem root and fail.
$stderrTmp = Join-Path $logDir ("_stderr_{0}.txt" -f (Get-Random))
try {
    # Resolve docker.exe by full path. Under the S4U scheduled-task principal the
    # minimal environment does NOT include Docker on PATH, so a bare `docker`
    # fails to resolve. Prefer PATH, fall back to the Docker Desktop install dir.
    $dockerExe = (Get-Command docker -ErrorAction SilentlyContinue).Source
    if (-not $dockerExe) {
        foreach ($c in @(
            'C:\Program Files\Docker\Docker\resources\bin\docker.exe',
            'C:\ProgramData\DockerDesktop\version-bin\docker.exe'
        )) { if (Test-Path $c) { $dockerExe = $c; break } }
    }
    if (-not $dockerExe) {
        # Record + fall through to the log write; do NOT throw (that skips the log).
        $fatal = "docker.exe not found on PATH or in the Docker Desktop install dir."
        Write-Warning $fatal
    } else {
        # STDERR -> temp file so a diagnostic line (e.g. 'Cannot connect to the
        # Docker daemon' when the engine pipe isn't visible to this logon) lands
        # in the log instead of vanishing or aborting the run.
        $stdout = (& $dockerExe exec samus-voice python3 -m backend.voice.reconcile_cli 2>$stderrTmp | Out-String)
        $exit = $LASTEXITCODE
        if (Test-Path $stderrTmp) { $dockerErr = (Get-Content -Raw -Path $stderrTmp -ErrorAction SilentlyContinue) }
        if ($exit -ne 0) {
            Write-Warning "reconcile_cli exited $exit. stderr: $dockerErr"
        }

        # Gap-18: drain the FULL set of today's outcomes (webhook-delivered AND
        # reconcile-backfilled), NOT just reconcile's newly-backfilled `details`.
        # reconcile only lists a call in `details` the ONE run that backfills it,
        # so a call whose end-of-call-report webhook was actually DELIVERED
        # (handler wrote the event, reconcile skipped it) would never drain to the
        # operator list. We emit all of today's end_of_call events from
        # samus-voice and drain THOSE; the drain's own idempotency (skip
        # already-journaled prospect_ids) dedups. Fall back to reconcile details
        # if the emit step fails.
        $emitOut = ""
        & $dockerExe cp (Join-Path $PSScriptRoot 'emit_today_outcomes.py') `
            'samus-voice:/opt/samus/data/artifacts/_emit_today_outcomes.py' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $emitOut = (& $dockerExe exec samus-voice python3 /opt/samus/data/artifacts/_emit_today_outcomes.py 2>$null | Out-String)
        }
        $drainInput = if ($emitOut.Trim()) { $emitOut } else { $stdout }

        # Drain into forge-ui's call_outcomes journal (host-side; the container
        # can't write HF_DATA_DIR). Best-effort: a drain failure never fails the
        # sweep and never blocks the log write.
        $py = (Get-Command python -ErrorAction SilentlyContinue).Source
        if (-not $py) { $py = "D:\Python311\python.exe" }
        $drainScript = Join-Path $PSScriptRoot "Drain-CallOutcomes.py"
        if (Test-Path $py) {
            $drainOut = ($drainInput | & $py $drainScript 2>&1 | Out-String)
            Write-Host "drain -> forge-ui: $drainOut"
        } else {
            $drainOut = "drain skipped: python not found at PATH or D:\Python311\python.exe"
            Write-Warning $drainOut
        }
    }
} catch {
    $fatal = "unhandled: $($_.Exception.Message)"
    Write-Warning $fatal
} finally {
    Remove-Item -Path $stderrTmp -ErrorAction SilentlyContinue
    @{ ts = $ts; exit_code = $exit; fatal = $fatal; docker_stderr = $dockerErr;
       stdout = $stdout; drain = $drainOut } |
        ConvertTo-Json -Depth 6 | Out-File -FilePath $logPath -Encoding utf8
}

Write-Host $stdout
# Fail the task (non-zero) when docker couldn't run at all, so Task Scheduler's
# LastTaskResult reflects a real problem instead of a false success.
if ($fatal) { exit 1 }
exit $exit
