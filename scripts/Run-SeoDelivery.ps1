<#
.SYNOPSIS
  Monthly SEO retainer delivery. For each active client in the SEO client
  registry, run a fresh audit inside samus-seo, render a client-ready report,
  and archive both. Closes the subscription->fulfillment gap found 2026-06-30
  (the sole $300/mo customer was billed with zero delivered/tracked work).

.DESCRIPTION
  Registered as \Hustleforge\Samus-SeoDelivery: 1st of each month, principal
  PC / S4U (runs whether logged on or not), RunLevel Highest. Docker Desktop
  must be up (lives in the interactive session).

  CRITICAL: the scheduled-task action switch is -NonInteractive, NOT
  -NoInteractive (see reference_ps51_scheduled_task_gotchas trap 8 — the typo
  exits 0x80070002 without running).

  Registration:
    $a = New-ScheduledTaskAction -Execute powershell.exe -Argument `
      '-NonInteractive -ExecutionPolicy Bypass -File "D:\Hustleforge\Samus\scripts\Run-SeoDelivery.ps1"'
    $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 6am  # placeholder; real trigger = monthly day-1 via schtasks /sc MONTHLY /d 1
    $p = New-ScheduledTaskPrincipal -UserId "PC" -LogonType S4U -RunLevel Highest
#>
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root       = 'D:\Hustleforge\Samus'
$clientsDir = Join-Path $root '.data\host_artifacts\seo_clients'
$clientsJson= Join-Path $clientsDir 'clients.json'
$renderer   = Join-Path $root 'scripts\render_seo_report.py'
$helper     = Join-Path $root 'scripts\seo_audit_helper.py'
$logDir     = Join-Path $root 'logs\seo_delivery'
$today      = (Get-Date).ToString('yyyy-MM-dd')

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir "seo_delivery_$today.json"

# Resolve docker.exe (S4U task PATH omits it).
$docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $docker) {
    foreach ($c in @('C:\Program Files\Docker\Docker\resources\bin\docker.exe',
                     'C:\ProgramData\DockerDesktop\version-bin\docker.exe')) {
        if (Test-Path $c) { $docker = $c; break }
    }
}
if (-not $docker) { throw "docker.exe not found." }

# Resolve host python (renderer is stdlib-only).
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'D:\Hustleforge\Samus\.venv\Scripts\python.exe' }
if (-not (Test-Path $py)) { throw "python not found for renderer." }

if (-not (Test-Path $clientsJson)) { throw "client registry not found: $clientsJson" }
$registry = Get-Content $clientsJson -Raw | ConvertFrom-Json

# Push the audit helper into samus-seo's writable volume (rootfs may be read-only;
# /opt/samus/data/artifacts is a volume and accepts docker cp).
& $docker cp $helper "samus-seo:/opt/samus/data/artifacts/_seo_audit_helper.py" | Out-Null

$results = @()
foreach ($cl in $registry.clients) {
    if ($cl.status -ne 'active') { continue }
    $slug = ($cl.name -replace '[^a-zA-Z0-9]+','-').Trim('-').ToLower()
    $cdir = Join-Path $clientsDir $slug
    if (-not (Test-Path $cdir)) { New-Item -ItemType Directory -Path $cdir -Force | Out-Null }
    $auditOut = Join-Path $cdir "audit_$today.json"
    $reportOut= Join-Path $cdir "SEO_REPORT_$today.md"

    Write-Host "==> $($cl.name): auditing $($cl.url)"
    $kw = ($cl.keywords -join '|')
    try {
        & $docker exec `
            -e "PYTHONPATH=/opt/samus" `
            -e "SEO_URL=$($cl.url)" -e "SEO_KEYWORDS=$kw" `
            -e "SEO_INDUSTRY=$($cl.plan)" -e "SEO_CID=$($cl.customer_id)" `
            samus-seo python3 /opt/samus/data/artifacts/_seo_audit_helper.py 2>$null | Out-Host
        # copy the audit JSON out
        & $docker cp "samus-seo:/opt/samus/data/artifacts/_seo_delivery_audit.json" $auditOut | Out-Null

        # prior audit (for score movement) = newest audit_*.json that isn't today's
        $prev = Get-ChildItem (Join-Path $cdir 'audit_*.json') -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ne "audit_$today.json" } |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1

        # single-client json for the renderer
        $clFile = Join-Path $cdir '_client.json'
        $cl | ConvertTo-Json -Depth 6 | Out-File -FilePath $clFile -Encoding utf8

        $renderArgs = @($renderer, $auditOut, $clFile, $reportOut)
        if ($prev) { $renderArgs += $prev.FullName }
        & $py @renderArgs | Out-Host

        $results += @{ client = $cl.name; url = $cl.url; report = $reportOut; ok = (Test-Path $reportOut) }
    } catch {
        Write-Warning "  $($cl.name) FAILED: $($_.Exception.Message)"
        $results += @{ client = $cl.name; url = $cl.url; ok = $false; error = $_.Exception.Message }
    }
}

@{ ts = $today; results = $results } | ConvertTo-Json -Depth 6 | Out-File -FilePath $logPath -Encoding utf8
Write-Host "SEO delivery sweep complete: $($results.Count) client(s). Log: $logPath"
