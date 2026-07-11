<#
.SYNOPSIS
  Run the Samus headless site builder + Cloudflare Pages deployer with
  credentials loaded from DPAPI (Scope=Samus) into the subprocess env.

.DESCRIPTION
  Mirrors Run-WebsiteBuild.ps1. Loads three Cloudflare secrets from the Samus
  DPAPI store, sets the headless+deploy flags ON, then forwards all remaining
  arguments to ``python -m backend.website.cli``.

  ONE-TIME SEED — run in a REAL PC console as the operator (never via the
  in-session ``!`` prefix):

    Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1
    Set-HfSecret -Scope Samus -Name CloudflareApiToken  -Force  # paste token at masked prompt
    Set-HfSecret -Scope Samus -Name CloudflareAccountId -Force  # paste account-id
    Set-HfSecret -Scope Samus -Name CloudflarePagesProject -Force  # paste project name, e.g. samus-sites

  Cloudflare account: 27db709bd31174c1892dd03cbc2f1d39
  Pages project:      samus-sites  (samus-sites.pages.dev)

  Token scope required: Account -> Cloudflare Pages -> Edit
  Token creation URL:   https://dash.cloudflare.com/profile/api-tokens

.EXAMPLE
  # Smoke test: deploy the <sample-client> placeholder
  .\Run-CloudflareDeploy.ps1 start --order scripts\website_orders\harmony.json

.EXAMPLE
  # Or run the smoke test one-shot from Python directly (after seeding DPAPI):
  # $env set then:
  #   python -c "<smoke test code from handoff>"
#>
[CmdletBinding()]
param(
    [switch]$Autonomous,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { throw "venv python not found at $venvPython." }

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) { throw "Secrets module not found at $sharedModule." }
Import-Module $sharedModule -Force

# ---- env backup (always restored) -------------------------------------------
$envKeys = @(
    'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_PAGES_PROJECT',
    'SAMUS_WEBSITE_HEADLESS_ENABLED', 'SAMUS_WEBSITE_DEPLOY_ENABLED',
    'SAMUS_WEBSITE_DEPLOY_HOST', 'SAMUS_WEBSITE_AUTONOMOUS_ENABLED',
    'SAMUS_STATE_ROOT', 'PYTHONIOENCODING'
)
$envBackup = @{}
foreach ($k in $envKeys) {
    $envBackup[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
}

$exit = 1
try {
    Set-Item -Path 'Env:PYTHONIOENCODING'               -Value 'utf-8'
    Set-Item -Path 'Env:SAMUS_WEBSITE_HEADLESS_ENABLED' -Value '1'
    Set-Item -Path 'Env:SAMUS_WEBSITE_DEPLOY_ENABLED'   -Value '1'
    Set-Item -Path 'Env:SAMUS_WEBSITE_DEPLOY_HOST'      -Value 'cloudflare'

    $artifactRoot = Join-Path $samusRoot '.data\host_artifacts'
    $stateRoot    = Join-Path $artifactRoot 'state'
    if (-not (Test-Path $stateRoot)) { New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null }
    Set-Item -Path 'Env:SAMUS_STATE_ROOT' -Value $stateRoot

    # --- Cloudflare secrets ---------------------------------------------------
    if (Test-HfSecret -Scope Samus -Name CloudflareApiToken) {
        Set-Item -Path 'Env:CLOUDFLARE_API_TOKEN' -Value (Get-HfSecret -Scope Samus -Name CloudflareApiToken)
        Write-Host "CLOUDFLARE_API_TOKEN loaded from DPAPI." -ForegroundColor Green
    } else {
        Write-Warning "CloudflareApiToken not in Samus DPAPI. Deploy will fail. Seed with: Set-HfSecret -Scope Samus -Name CloudflareApiToken -Force"
    }

    if (Test-HfSecret -Scope Samus -Name CloudflareAccountId) {
        Set-Item -Path 'Env:CLOUDFLARE_ACCOUNT_ID' -Value (Get-HfSecret -Scope Samus -Name CloudflareAccountId)
        Write-Host "CLOUDFLARE_ACCOUNT_ID loaded from DPAPI." -ForegroundColor Green
    } else {
        Write-Warning "CloudflareAccountId not in Samus DPAPI."
    }

    if (Test-HfSecret -Scope Samus -Name CloudflarePagesProject) {
        Set-Item -Path 'Env:CLOUDFLARE_PAGES_PROJECT' -Value (Get-HfSecret -Scope Samus -Name CloudflarePagesProject)
        Write-Host "CLOUDFLARE_PAGES_PROJECT loaded from DPAPI." -ForegroundColor Green
    } else {
        Write-Warning "CloudflarePagesProject not in Samus DPAPI."
    }

    if ($Autonomous) {
        Set-Item -Path 'Env:SAMUS_WEBSITE_AUTONOMOUS_ENABLED' -Value 'true'
        Write-Host "AUTONOMOUS ENABLED — per-stage operator approval is bypassed." -ForegroundColor Yellow
    }

    if ($CliArgs -and $CliArgs.Count -gt 0) {
        Push-Location $samusRoot
        try {
            & $venvPython -m backend.website.cli @CliArgs
            $exit = $LASTEXITCODE
        } finally {
            Pop-Location
        }
    } else {
        # No CLI args: just set env and print summary (useful for interactive use)
        Write-Host ""
        Write-Host "Cloudflare env loaded. No CLI args passed — env is set for this shell session." -ForegroundColor Cyan
        Write-Host "  CLOUDFLARE_ACCOUNT_ID   = $($env:CLOUDFLARE_ACCOUNT_ID)"
        Write-Host "  CLOUDFLARE_PAGES_PROJECT = $($env:CLOUDFLARE_PAGES_PROJECT)"
        Write-Host ""
        Write-Host "Smoke test (from samus worktree):" -ForegroundColor Cyan
        Write-Host @'
  python -c "
from backend.common.settings import reload_settings
from backend.website.site_builder import GeneratedSite
from backend.website import deploy_cloudflare as dc
import json
site = GeneratedSite(files={'index.html': open(r'.data/deliverables/sample-cleaning/index.html',encoding='utf-8').read()}, pages=['index.html'])
print(json.dumps(dc.deploy_site(site, settings=reload_settings()).to_dict(), indent=2))
"
'@
        $exit = 0
    }
} finally {
    foreach ($k in $envBackup.Keys) {
        if ($null -eq $envBackup[$k]) {
            Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$k" -Value $envBackup[$k]
        }
    }
}

exit $exit
