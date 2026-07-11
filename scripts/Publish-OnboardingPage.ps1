<#
.SYNOPSIS
  Publish (or plan) the hustleforge.tech/onboarding WordPress fallback page.

.DESCRIPTION
  Thin launcher around `python -m backend.intake.wp_onboarding_page`. It loads
  the WordPress application-password credential from the DPAPI store
  (Scope=Samus) onto this process's env, runs the one-shot from the Samus repo
  root, and scrubs the secrets in `finally` — the same posture as
  Start-MorningDial.ps1.

  Loading the creds matters: the one-shot's existence check fails OPEN, so
  running it WITHOUT WordPress creds makes it plan `create` even if the page
  already exists — and `-Apply` would then spawn a duplicate draft. This
  launcher guarantees the creds are present so the lookup is authoritative.

  Wire-not-arm: -Apply still requires SAMUS_WP_ONBOARDING_PUBLISH_ENABLED=1 in
  the environment (the python module enforces it). Without -Apply this is a
  read-only plan.

.EXAMPLE
  .\Publish-OnboardingPage.ps1                 # plan only (read-only)

.EXAMPLE
  $env:SAMUS_WP_ONBOARDING_PUBLISH_ENABLED = '1'
  .\Publish-OnboardingPage.ps1 -Apply          # create draft / update in place
#>
[CmdletBinding()]
param(
    [Parameter()][switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    $mainVenvPython = 'D:\Hustleforge\Samus\.venv\Scripts\python.exe'
    if (Test-Path $mainVenvPython) {
        $venvPython = $mainVenvPython
    } else {
        throw "venv python not found at $venvPython or $mainVenvPython. Run scripts\Bootstrap-Samus.ps1 first."
    }
}

# DPAPI secret pull — same shared module every Samus launcher uses.
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Shared secrets module not found at $sharedModule"
}
Import-Module $sharedModule -Force

$required = @(
    @{ DpapiName='WordPressUsername';    EnvVar='WORDPRESS_USERNAME' },
    @{ DpapiName='WordPressAppPassword'; EnvVar='WORDPRESS_APP_PASSWORD' }
)
$missing = @()
foreach ($s in $required) {
    if (-not (Test-HfSecret -Scope Samus -Name $s.DpapiName)) {
        $missing += $s.DpapiName
    }
}
if ($missing.Count -gt 0) {
    Write-Warning "Missing required DPAPI secrets: $($missing -join ', ')"
    Write-Warning "Populate with: Set-HfSecret -Scope Samus -Name <Name>"
    throw "Aborting -- WordPress credentials are not in the DPAPI store."
}

$pyArgs = @('-m', 'backend.intake.wp_onboarding_page')
if ($Apply) {
    $pyArgs += '--apply'
    if ($env:SAMUS_WP_ONBOARDING_PUBLISH_ENABLED -notin @('1','true','yes','on')) {
        Write-Warning "-Apply given but SAMUS_WP_ONBOARDING_PUBLISH_ENABLED is not armed."
        Write-Warning "The one-shot will refuse the write (wired-dormant). Set the flag to enact:"
        Write-Warning '  $env:SAMUS_WP_ONBOARDING_PUBLISH_ENABLED = ''1'''
    }
    Write-Host "==> Publishing onboarding page (create draft / update in place)" -ForegroundColor Yellow
} else {
    Write-Host "==> Planning onboarding page (read-only; pass -Apply to enact)" -ForegroundColor Cyan
}

$exitCode = 0
$exported = @()
try {
    foreach ($s in $required) {
        Set-Item -Path "Env:$($s.EnvVar)" -Value (Get-HfSecret -Scope Samus -Name $s.DpapiName)
        $exported += $s.EnvVar
    }
    $env:WORDPRESS_SITE = if ($env:WORDPRESS_SITE) { $env:WORDPRESS_SITE } else { 'hustleforge.tech' }
    $exported += 'WORDPRESS_SITE'

    Push-Location $samusRoot
    try {
        & $venvPython @pyArgs
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    foreach ($var in $exported) {
        Remove-Item -Path "Env:$var" -ErrorAction SilentlyContinue
    }
}

if ($exitCode -ne 0) {
    Write-Warning "Onboarding publish exited with code $exitCode"
    exit $exitCode
}
