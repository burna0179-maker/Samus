<#
.SYNOPSIS
  Operator-prompted demo-site pre-build for today's no-website cold-call
  prospects: "I already built you a site - here's the link."

.DESCRIPTION
  Wraps `python -m backend.website.demo_sites` with the Run-ProspectingDaily
  operational shape: transcript + rotation, DPAPI secrets pushed into the
  child env (restored in finally), stderr sidecar, exit-code propagation.

  Reads the day's call_list_YYYY-MM-DD.csv under <artifact root>/daily_calls,
  builds one static demo site per website_status == "no_website" prospect,
  and (creds permitting) deploys each to its own free Cloudflare Pages
  project demo-<company-slug>. Hard total cost ceiling per run (default
  $5.00) - the only paid step is optional haiku copy polish; once the
  conservative cumulative estimate would cross the ceiling, copy falls back
  to the $0 deterministic path and the run still completes.

  Secrets read from DPAPI (Scope=Samus) - ALL optional:

    AnthropicApiKey     -> ANTHROPIC_API_KEY      (absent = deterministic copy, $0)
    CloudflareApiToken  -> CLOUDFLARE_API_TOKEN   (absent = build-only, no deploy)
    CloudflareAccountId -> CLOUDFLARE_ACCOUNT_ID  (absent = build-only, no deploy)

  Arms SAMUS_WEBSITE_DEMO_SITES_ENABLED=1 for the child process only - the
  flag stays OFF everywhere else, so nothing but this operator command can
  fire a demo run.

.PARAMETER Date
  Call-list date (YYYY-MM-DD). Default: today.

.PARAMETER Ceiling
  Hard total run cost ceiling in USD. Default 5.0.

.PARAMETER NoDeploy
  Build the sites locally but skip the Cloudflare deploy.

.PARAMETER NoLlm
  Skip LLM copy polish entirely (deterministic copy, $0).

.PARAMETER Limit
  Process at most N prospects (0 = all).

.EXAMPLE
  .\Build-DemoSites.ps1                       # today's list, deploy, <= $5
  .\Build-DemoSites.ps1 -NoDeploy -NoLlm      # free local dry-build
  .\Build-DemoSites.ps1 -Date 2026-06-10 -Limit 5
#>

[CmdletBinding()]
param(
    [string]$Date = '',
    [double]$Ceiling = 5.0,
    [switch]$NoDeploy,
    [switch]$NoLlm,
    [int]$Limit = 0,
    [string]$Company = ''
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

# --- transcript capture (same shape as Run-ProspectingDaily.ps1) -------------
$logDir = Join-Path $samusRoot 'logs\demo_sites'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir ("demo_sites_$((Get-Date).ToString('yyyy-MM-dd_HHmmss')).log")
$transcriptStarted = $false
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true
    Write-Host "transcript -> $logPath"
} catch {
    Write-Warning "Start-Transcript failed (continuing without log): $_"
}

try {
    Get-ChildItem $logDir -Filter 'demo_sites_*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 30 |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { Write-Warning "log rotation failed: $_" }

$exit = 1

try {

# --- artifact root ------------------------------------------------------------
# Same anchoring rationale as Run-ProspectingDaily.ps1: producer (prospecting)
# and consumer (this script) must read ONE host_artifacts tree that follows
# the checkout, or the call list silently "disappears".
$defaultArtifactRoot = Join-Path $samusRoot '.data\host_artifacts'
$artifactRoot = if ($env:SAMUS_ARTIFACT_ROOT) { $env:SAMUS_ARTIFACT_ROOT } else { $defaultArtifactRoot }
if (-not (Test-Path $artifactRoot)) { New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null }

Write-Host ""
Write-Host "=== Demo-site pre-build (cold-call display sites) ==="
Write-Host ("Artifact root : {0}" -f $artifactRoot)
Write-Host ("Ceiling       : `${0:N2} (hard, per run)" -f $Ceiling)
if ($Date)     { Write-Host ("Call list date: {0}" -f $Date) }
if ($NoDeploy) { Write-Host "Deploy        : SKIPPED (-NoDeploy)" }
if ($NoLlm)    { Write-Host "LLM copy      : SKIPPED (-NoLlm, deterministic only)" }
Write-Host ""

# --- secret availability report (all optional) --------------------------------
$haveAnthropic = Test-HfSecret -Scope Samus -Name AnthropicApiKey
$haveCfToken   = Test-HfSecret -Scope Samus -Name CloudflareApiToken
$haveCfAccount = Test-HfSecret -Scope Samus -Name CloudflareAccountId
# Web-presence cross-referencing creds (backend.website.presence_check). Without
# at least one of these the presence gate runs blind (fail-soft -> always
# buildable), so a business that HAS a site could get a demo built/pitched. With
# them, the gate re-verifies and skips (run.skipped_has_site). Places = the
# authoritative-when-linked check; Gemini grounded search = the not-linked-in-GBP
# gap-closer (Google's replacement for the Custom Search JSON API, which is closed
# to new customers and unavailable on this project). All optional.
$havePlaces    = Test-HfSecret -Scope Samus -Name GooglePlacesApiKey
$haveGemini    = Test-HfSecret -Scope Samus -Name GeminiApiKey
if (-not $haveAnthropic) { Write-Host "AnthropicApiKey not in DPAPI store - deterministic copy ($0)." }
if (-not ($haveCfToken -and $haveCfAccount)) {
    Write-Host "Cloudflare token/account incomplete in DPAPI store - build-only, no deploy."
}
if (-not ($havePlaces -or $haveGemini)) {
    Write-Host "No presence-check creds (Places/Gemini) - build gate runs blind (fail-soft)."
}

# --- env push: always restored in finally, even on throw ----------------------
$envBackup = @{
    'SAMUS_WEBSITE_DEMO_SITES_ENABLED' = [Environment]::GetEnvironmentVariable('SAMUS_WEBSITE_DEMO_SITES_ENABLED', 'Process')
    'SAMUS_ARTIFACT_ROOT'              = [Environment]::GetEnvironmentVariable('SAMUS_ARTIFACT_ROOT', 'Process')
    'PYTHONIOENCODING'                 = [Environment]::GetEnvironmentVariable('PYTHONIOENCODING', 'Process')
    'ANTHROPIC_API_KEY'                = [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'Process')
    'CLOUDFLARE_API_TOKEN'             = [Environment]::GetEnvironmentVariable('CLOUDFLARE_API_TOKEN', 'Process')
    'CLOUDFLARE_ACCOUNT_ID'            = [Environment]::GetEnvironmentVariable('CLOUDFLARE_ACCOUNT_ID', 'Process')
    'GOOGLE_PLACES_API_KEY'            = [Environment]::GetEnvironmentVariable('GOOGLE_PLACES_API_KEY', 'Process')
    'GEMINI_API_KEY'                   = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'Process')
}
# Arm the flag for THIS child process only (catalog default stays OFF).
Set-Item -Path 'Env:SAMUS_WEBSITE_DEMO_SITES_ENABLED' -Value '1'
Set-Item -Path 'Env:SAMUS_ARTIFACT_ROOT' -Value $artifactRoot
Set-Item -Path 'Env:PYTHONIOENCODING' -Value 'utf-8'
if ($haveAnthropic) {
    Set-Item -Path 'Env:ANTHROPIC_API_KEY' -Value (Get-HfSecret -Scope Samus -Name AnthropicApiKey)
}
if ($haveCfToken -and $haveCfAccount) {
    Set-Item -Path 'Env:CLOUDFLARE_API_TOKEN' -Value (Get-HfSecret -Scope Samus -Name CloudflareApiToken)
    Set-Item -Path 'Env:CLOUDFLARE_ACCOUNT_ID' -Value (Get-HfSecret -Scope Samus -Name CloudflareAccountId)
}
# Presence-check backends (Places authoritative-when-linked; Gemini grounded
# search closes the not-linked-in-GBP gap). Each seeded independently so partial
# coverage still helps. presence_check reads these via get_settings() at build.
if ($havePlaces) {
    Set-Item -Path 'Env:GOOGLE_PLACES_API_KEY' -Value (Get-HfSecret -Scope Samus -Name GooglePlacesApiKey)
}
if ($haveGemini) {
    Set-Item -Path 'Env:GEMINI_API_KEY' -Value (Get-HfSecret -Scope Samus -Name GeminiApiKey)
}

# --- compose python args -------------------------------------------------------
$pyArgs = @('-m', 'backend.website.demo_sites', '--ceiling', $Ceiling.ToString([System.Globalization.CultureInfo]::InvariantCulture))
if ($Date)      { $pyArgs += @('--date', $Date) }
if ($NoDeploy)  { $pyArgs += '--no-deploy' }
if ($NoLlm)     { $pyArgs += '--no-llm' }
if ($Limit -gt 0) { $pyArgs += @('--limit', $Limit) }
if ($Company)   { $pyArgs += @('--company', $Company) }

$stderrPath = "$logPath.err"
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

Push-Location $samusRoot
try {
    & $venvPython @pyArgs 2>$stderrPath
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

if ($exit -eq 0) {
    Write-Host ""
    Write-Host "Call sheet: $artifactRoot\demo_sites\demo_sites_<date>.txt" -ForegroundColor Green
}

} finally {
    Write-Host "EXIT: $exit"
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}

exit $exit
