<#
.SYNOPSIS
  Fire the daily prospecting pipeline against the currently-active geographic
  ring. Expands outward only when the operator explicitly says so.

.DESCRIPTION
  Strategy: local-first. The operator works one geographic ring at a time. When
  the active ring is exhausted (hand-dial pool dry, no fresh prospects worth
  calling), invoke this script with -AdvanceRing once to add the next ring's
  zipcodes to the active set. Rings are cumulative -- once added, a ring stays
  in the search set so the operator can revisit + the dedupe pipeline can
  recognise prior contacts.

  Each fire writes a call_list_YYYY-MM-DD.csv + morning_call_list_YYYY-MM-DD.txt
  under storage.root()/daily_calls/ . The 08:00 Samus Morning Brief picks up the
  .txt and attaches it to the daily push, closing the loop.

  Secrets read from DPAPI (Scope=Samus):

    GooglePlacesApiKey  ->  GOOGLE_PLACES_API_KEY  (required; aborts if missing)

  Logs to ../logs/prospecting_daily/ following the Send-Morning.ps1 pattern
  (Start-Transcript + sidecar stderr capture + rotation to last 30).

  State file: <SAMUS_ARTIFACT_ROOT>/prospecting_geo_state.json
  Default:    E:\Hustleforge\Samus\data\artifacts\prospecting_geo_state.json

.PARAMETER AdvanceRing
  Bump the active ring forward by one before running. Idempotent at the top
  ring (warns + stays). Operator-controlled; nothing auto-advances.

.PARAMETER DryRun
  Print the ring/zipcode/industry plan and the python command line that would
  fire, but do NOT call Google Places and do NOT update the state file. Safe
  to run repeatedly while tuning the ring catalog.

.PARAMETER Industries
  Override the industry keyword list. Default mirrors the Tier A+B verticals
  derived from Samus/backend/services/workflow_rescue/example_workflows.md and
  Samus/backend/retainer/ai_ops_partner/example_engagements.md.

.PARAMETER MaxPerZip
  Forward to run_daily.py --max-per-zip. Default 25 (Places API page size).

.PARAMETER ExcludeNoWebsite
  Drop prospects with no website at discovery (legacy behaviour). By default
  the daily run now keeps them -- a business with no site is the strongest
  web-design lead and is surfaced at the top of the morning call list.

.EXAMPLE
  .\Run-ProspectingDaily.ps1                # fire today's run against active ring
  .\Run-ProspectingDaily.ps1 -DryRun        # show plan, don't spend quota
  .\Run-ProspectingDaily.ps1 -AdvanceRing   # promote one ring then fire
#>

[CmdletBinding()]
param(
    [switch]$AdvanceRing,
    [switch]$DryRun,
    [switch]$ExcludeNoWebsite,
    [int]$MaxPerZip = 25,
    [string]$Industries = 'real estate agency,dentist,hvac contractor,plumber,roofing contractor,accounting firm,car dealer'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    $mainVenvPython = 'D:\Hustleforge\Samus\.venv\Scripts\python.exe'
    if (Test-Path $mainVenvPython) {
        $venvPython = $mainVenvPython
    } else {
        throw "venv python not found at $venvPython or $mainVenvPython. Run from Samus repo root with .venv installed."
    }
}

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Required secrets module not found at $sharedModule."
}
Import-Module $sharedModule -Force

# --- transcript capture (same shape as Send-Morning.ps1) ---------------------
$logDir = Join-Path $samusRoot 'logs\prospecting_daily'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir ("prospecting_daily_$((Get-Date).ToString('yyyy-MM-dd_HHmmss')).log")
$transcriptStarted = $false
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true
    Write-Host "transcript -> $logPath"
} catch {
    Write-Warning "Start-Transcript failed (continuing without log): $_"
}

try {
    Get-ChildItem $logDir -Filter 'prospecting_daily_*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 30 |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { Write-Warning "log rotation failed: $_" }

$exit = 1

try {

# --- ring catalog ------------------------------------------------------------
# Operator-editable. Yuba City anchor expanding southward into Sacramento metro.
# Rings are cumulative once added: each ring index N includes rings 0..N's
# zipcodes in the active search. Edit zipcodes here when the field changes.
$ringCatalog = @(
    @{ name = 'yuba_city_core';        zipcodes = @('95991','95993','95901','95961','95692') }
    @{ name = 'yuba_sutter_outer';     zipcodes = @('95953','95919','95941','95918','95659','95957','95668') }
    @{ name = 'placer_lincoln_auburn'; zipcodes = @('95648','95603','95746','95765','95650') }
    @{ name = 'roseville_corridor';    zipcodes = @('95661','95678','95747') }
    @{ name = 'folsom_elk_grove';      zipcodes = @('95630','95758','95624','95757') }
    @{ name = 'sacramento_metro';      zipcodes = @('95814','95816','95818','95820','95822','95823','95825') }
)

# --- artifact root -----------------------------------------------------------
# Self-locating under the Samus runtime root ($samusRoot = this script's repo).
# Previously hardcoded to a standalone D:\Hustleforge\Samus\.data\host_artifacts;
# that second copy diverged from the worktree runtime and silently reset the
# prospecting ring (2026-06-01). Anchoring to $samusRoot keeps producer (this
# script) and consumer (Send-Morning.ps1) on ONE path that follows the checkout.
# The original storage.root() default (E:\Hustleforge\Samus\data\artifacts) has
# Phase-1 ACL Alex:(N); `.data/` is gitignored (.gitignore line 48) and
# Alex-writable; the `host_` prefix keeps host artifacts visibly separate from
# anything docker writes under `.data/`.
$defaultArtifactRoot = Join-Path $samusRoot '.data\host_artifacts'
$artifactRoot = if ($env:SAMUS_ARTIFACT_ROOT) { $env:SAMUS_ARTIFACT_ROOT } else { $defaultArtifactRoot }
if (-not (Test-Path $artifactRoot)) { New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null }
$statePath = Join-Path $artifactRoot 'prospecting_geo_state.json'

# Initialize $state under StrictMode -- otherwise the `if (-not $state)` test
# below throws VariableIsUndefined when the state file doesn't exist yet.
$state = $null
if (Test-Path $statePath) {
    try {
        $state = Get-Content $statePath -Raw | ConvertFrom-Json
    } catch {
        $bak = "$statePath.corrupt-$((Get-Date).ToString('yyyyMMdd_HHmmss'))"
        Move-Item $statePath $bak -Force
        Write-Warning "State file unreadable -- moved to $bak and reinitializing"
        $state = $null
    }
}
if (-not $state) {
    $state = [pscustomobject]@{
        current_ring   = 0
        days_at_ring   = 0
        last_run_date  = $null
        history        = @()
    }
    Write-Host "Initializing prospecting geo state at $statePath (start at ring 0 = yuba_city_core)"
}

# --- AdvanceRing handling ----------------------------------------------------
if ($AdvanceRing) {
    if ($state.current_ring -lt ($ringCatalog.Count - 1)) {
        $oldName = $ringCatalog[$state.current_ring].name
        $state.current_ring = ([int]$state.current_ring) + 1
        $state.days_at_ring = 0
        $newName = $ringCatalog[$state.current_ring].name
        Write-Host "Advanced from ring '$oldName' to ring '$newName' (ring index now $($state.current_ring))" -ForegroundColor Green
    } else {
        Write-Warning "Already at top ring '$($ringCatalog[$state.current_ring].name)' -- no further rings configured. Edit `$ringCatalog in this script to extend."
    }
}

# --- compose cumulative zipcode set ------------------------------------------
$activeZipcodes = New-Object System.Collections.Generic.List[string]
for ($i = 0; $i -le [int]$state.current_ring; $i++) {
    foreach ($z in $ringCatalog[$i].zipcodes) { [void]$activeZipcodes.Add($z) }
}
$activeZipcodes = $activeZipcodes | Select-Object -Unique
$zipString = $activeZipcodes -join ','

$currentRingName = $ringCatalog[[int]$state.current_ring].name
$dayAtRing       = ([int]$state.days_at_ring) + 1

Write-Host ""
Write-Host "=== Prospecting daily run ==="
Write-Host ("Active ring : {0} (index {1} of {2})" -f $currentRingName, $state.current_ring, ($ringCatalog.Count - 1))
Write-Host ("Cumulative  : {0} zipcodes -> {1}" -f $activeZipcodes.Count, $zipString)
Write-Host ("Industries  : {0}" -f $Industries)
Write-Host ("Day at ring : {0}" -f $dayAtRing)
Write-Host ""

# --- invocation --------------------------------------------------------------
$noWebsiteArgs = @()
if (-not $ExcludeNoWebsite) { $noWebsiteArgs = @('--include-no-website') }

if ($DryRun) {
    Write-Host "DRY RUN -- would invoke:" -ForegroundColor Cyan
    Write-Host "  $venvPython -m backend.prospecting.run_daily --zipcodes $zipString --industries `"$Industries`" --max-per-zip $MaxPerZip $($noWebsiteArgs -join ' ')"
    $exit = 0
} else {
    if (-not (Test-HfSecret -Scope Samus -Name GooglePlacesApiKey)) {
        throw "GooglePlacesApiKey not in Samus DPAPI store. Run: Set-HfSecret -Scope Samus -Name GooglePlacesApiKey"
    }

    # Env push -- always restore in finally, even on throw.
    # Pin SAMUS_ARTIFACT_ROOT so the python subprocess writes the call list to
    # the same path the state file lives in, and so the morning brief consumer
    # (also pinned to this path) finds it.
    # SAMUS_PROSPECTING_AUDIT_PATH default in service.py points at E:\...
    # which has Alex:(N) — service catches the OSError but skips the audit
    # event. Redirect to the same host_artifacts tree so audit lands too.
    # Also pin SAMUS_SEO_AUDIT_PATH for the warm/hot full-audit step which
    # writes its own ledger via backend.seo.service.
    $auditPath = Join-Path $artifactRoot 'evidence\prospecting\prospecting_audit.jsonl'
    $seoAuditPath = Join-Path $artifactRoot 'evidence\seo\seo_audit.jsonl'
    $envBackup = @{
        'GOOGLE_PLACES_API_KEY'          = [Environment]::GetEnvironmentVariable('GOOGLE_PLACES_API_KEY', 'Process')
        'PYTHONIOENCODING'               = [Environment]::GetEnvironmentVariable('PYTHONIOENCODING', 'Process')
        'SAMUS_ARTIFACT_ROOT'            = [Environment]::GetEnvironmentVariable('SAMUS_ARTIFACT_ROOT', 'Process')
        'SAMUS_PROSPECTING_AUDIT_PATH'   = [Environment]::GetEnvironmentVariable('SAMUS_PROSPECTING_AUDIT_PATH', 'Process')
        'SAMUS_SEO_AUDIT_PATH'           = [Environment]::GetEnvironmentVariable('SAMUS_SEO_AUDIT_PATH', 'Process')
        'ANTHROPIC_API_KEY'              = [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'Process')
        'GOOGLE_PAGESPEED_API_KEY'       = [Environment]::GetEnvironmentVariable('GOOGLE_PAGESPEED_API_KEY', 'Process')
        'GEMINI_API_KEY'                 = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'Process')
        'SAMUS_ENV'                      = [Environment]::GetEnvironmentVariable('SAMUS_ENV', 'Process')
    }
    Set-Item -Path 'Env:GOOGLE_PLACES_API_KEY' -Value (Get-HfSecret -Scope Samus -Name GooglePlacesApiKey)
    Set-Item -Path 'Env:PYTHONIOENCODING' -Value 'utf-8'
    Set-Item -Path 'Env:SAMUS_ARTIFACT_ROOT' -Value $artifactRoot
    Set-Item -Path 'Env:SAMUS_PROSPECTING_AUDIT_PATH' -Value $auditPath
    Set-Item -Path 'Env:SAMUS_SEO_AUDIT_PATH' -Value $seoAuditPath
    # Pin SAMUS_ENV=production so the host run honours the same fail-closed
    # gates as the in-container stack. The stack runs SAMUS_ENV=production
    # since 2026-06-22; an unset value defaulted host runs to 'development',
    # leaving the security gates bypassed. Operator can override via
    # $env:SAMUS_ENV before invoking this script.
    if (-not $env:SAMUS_ENV) {
        Set-Item -Path 'Env:SAMUS_ENV' -Value 'production'
    }
    # Anthropic key is optional — when present, the warm/hot full-audit step
    # uses LLM-generated content drafts. Without it, content drafts fall
    # back to the templated path and the audit + recommendations still ship.
    if (Test-HfSecret -Scope Samus -Name AnthropicApiKey) {
        Set-Item -Path 'Env:ANTHROPIC_API_KEY' -Value (Get-HfSecret -Scope Samus -Name AnthropicApiKey)
    }
    if (Test-HfSecret -Scope Samus -Name GooglePagespeedApiKey) {
        Set-Item -Path 'Env:GOOGLE_PAGESPEED_API_KEY' -Value (Get-HfSecret -Scope Samus -Name GooglePagespeedApiKey)
    }
    # Web-presence cross-referencing (backend.website.presence_check): the Gemini
    # grounded Google search closes the gap where a real site is NOT linked in the
    # Google Business Profile, so verify_web_presence never mislabels a business
    # that HAS a site as "no website". NOTE: the Google Custom Search JSON API (the
    # other backend) is CLOSED TO NEW CUSTOMERS, so it is permanently unavailable
    # on this project (403) — Gemini grounded search is Google's own recommended
    # replacement and is what we use. Optional — absent, degrades to Places-only.
    if (Test-HfSecret -Scope Samus -Name GeminiApiKey) {
        Set-Item -Path 'Env:GEMINI_API_KEY' -Value (Get-HfSecret -Scope Samus -Name GeminiApiKey)
    }

    $stderrPath = "$logPath.err"
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'

    Push-Location $samusRoot
    try {
        & $venvPython -m backend.prospecting.run_daily `
            --zipcodes $zipString `
            --industries $Industries `
            --max-per-zip $MaxPerZip @noWebsiteArgs 2>$stderrPath
        $exit = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prevEAP
        foreach ($k in $envBackup.Keys) {
            if ($null -eq $envBackup[$k]) {
                Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
            } else {
                Set-Item -Path "Env:$k" -Value $envBackup[$k]
            }
        }
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
}

# --- state update ------------------------------------------------------------
# DryRun does not advance days_at_ring or append history (it's a no-op probe).
if (-not $DryRun) {
    $state.days_at_ring = $dayAtRing
    $state.last_run_date = (Get-Date).ToString('yyyy-MM-dd')

    $historyEntry = [pscustomobject]@{
        date           = $state.last_run_date
        ring_index     = $state.current_ring
        ring_name      = $currentRingName
        zipcodes_count = $activeZipcodes.Count
        day_at_ring    = $dayAtRing
        exit_code      = $exit
    }
    $state.history = @($state.history) + @($historyEntry)
    if ($state.history.Count -gt 90) {
        $state.history = $state.history | Select-Object -Last 90
    }

    $state | ConvertTo-Json -Depth 5 | Set-Content -Path $statePath -Encoding UTF8
    Write-Host ""
    Write-Host "State updated: $statePath  (ring=$($state.current_ring) day=$dayAtRing exit=$exit)"
}

# --- operator nudge ----------------------------------------------------------
Write-Host ""
$nextIdx = ([int]$state.current_ring) + 1
if ($nextIdx -lt $ringCatalog.Count) {
    $nextName = $ringCatalog[$nextIdx].name
    $nextCount = $ringCatalog[$nextIdx].zipcodes.Count
    Write-Host "When local pool is exhausted, expand with:"
    Write-Host "  .\Run-ProspectingDaily.ps1 -AdvanceRing   # adds ring '$nextName' (+$nextCount zipcodes)"
} else {
    Write-Host "(at top ring -- no further rings configured)"
}

} finally {
    Write-Host "EXIT: $exit"
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}

exit $exit
