<#
.SYNOPSIS
  Client check-in SEND sweep. Sends any queued check-in whose 24h hold has
  elapsed and that the operator hasn't stopped. Runs the send helper inside
  samus-outreach (SendGrid key + send_email + host_artifacts bind).

.DESCRIPTION
  Registered as \Hustleforge\Samus-ClientCheckinSend: every 6 hours, PC/S4U,
  RunLevel Highest. Policy (operator 2026-06-30): draft -> 24h grace ->
  auto-send unless stopped. STOP = delete the draft .md, or set the queue
  entry's status to "stopped". Sends as transactional (relationship) mail.

  CRITICAL: scheduled-task switch is -NonInteractive (not -NoInteractive;
  the typo exits 0x80070002 — see reference_ps51_scheduled_task_gotchas trap 8).
#>
param([switch]$DryRun)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root   = 'D:\Hustleforge\Samus'
$helper = Join-Path $root 'scripts\checkin_send_helper.py'
$logDir = Join-Path $root 'logs\client_checkin'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir ("checkin_send_" + (Get-Date -Format 'yyyy-MM-dd_HHmmss') + ".json")

$docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $docker) {
    foreach ($c in @('C:\Program Files\Docker\Docker\resources\bin\docker.exe',
                     'C:\ProgramData\DockerDesktop\version-bin\docker.exe')) {
        if (Test-Path $c) { $docker = $c; break }
    }
}
if (-not $docker) { throw "docker.exe not found." }

# Push the send helper into samus-outreach (writable volume).
& $docker cp $helper 'samus-outreach:/opt/samus/data/artifacts/_checkin_send_helper.py' | Out-Null

$envArgs = @('-e', 'PYTHONPATH=/opt/samus')
if ($DryRun) { $envArgs += @('-e', 'CHECKIN_DRY_RUN=1') }

$out = & $docker exec @envArgs samus-outreach python3 /opt/samus/data/artifacts/_checkin_send_helper.py 2>$null | Out-String
Write-Host $out.Trim()
$out | Out-File -FilePath $logPath -Encoding utf8
exit 0
