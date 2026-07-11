<#
.SYNOPSIS
  Apply the two operator-directed WordPress dead-link fixes that the
  deterministic remediation refused to guess (no unique Stripe successor).

.DESCRIPTION
  DPAPI launcher for scripts\_oneshot_wp_link_surgery.py. Injects the WordPress
  application-password credential (and, if present, the Stripe read key for the
  post-apply funnel-snapshot refresh) onto this process's env, runs from the
  Samus repo root, and scrubs the secrets in `finally`. Same posture as
  Remediate-WpLinks.ps1 / Publish-OnboardingPage.ps1.

  The two edits (pure URL swaps, surrounding markup preserved):
    * home (id 248):            buy.stripe.com/test        -> /onboarding
    * seo-optimization (id 320): archived ...so0j checkout -> /onboarding?interest=seo_optimization

  Default is INSPECT (read-only, dumps the markup around each dead link).
  -Diff previews the exact change. -Apply writes to the LIVE pages, and only
  when the capability is armed (SAMUS_WP_REMEDIATION_ENABLED, resolved from the
  process env or docker\compose\.env). The one-shot is idempotent, so a
  rate-limited partial apply is safely resumable by re-running.

.EXAMPLE
  .\Fix-WpDeadLinks.ps1              # inspect (read-only)

.EXAMPLE
  .\Fix-WpDeadLinks.ps1 -Diff        # preview the exact change

.EXAMPLE
  .\Fix-WpDeadLinks.ps1 -Apply       # write to the live pages (if armed)
#>
[CmdletBinding()]
param(
    [Parameter()][switch]$Diff,
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

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Shared secrets module not found at $sharedModule"
}
Import-Module $sharedModule -Force

# WordPress creds are required (read + write). Stripe key is optional -- only the
# post-apply funnel-snapshot refresh uses it; without it that refresh degrades
# to 'unknown' but the edits still apply.
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

function Resolve-Armed {
    $truthy = @('1', 'true', 'yes', 'on')
    if ($env:SAMUS_WP_REMEDIATION_ENABLED -and
        $truthy -contains $env:SAMUS_WP_REMEDIATION_ENABLED.ToLower()) {
        return $true
    }
    $envFile = Join-Path $samusRoot 'docker\compose\.env'
    if (Test-Path $envFile) {
        $line = Select-String -Path $envFile -Pattern '^\s*SAMUS_WP_REMEDIATION_ENABLED\s*=\s*(.+?)\s*$' |
                Select-Object -First 1
        if ($line) {
            $val = $line.Matches[0].Groups[1].Value.Trim().Trim("'`"").ToLower()
            if ($truthy -contains $val) { return $true }
        }
    }
    return $false
}

$pyArgs = @('scripts\_oneshot_wp_link_surgery.py')
$armForChild = $false
if ($Apply) {
    $pyArgs += '--apply'
    $armForChild = Resolve-Armed
    if ($armForChild) {
        Write-Host "==> Applying WP dead-link fixes (armed) - writes to the LIVE site" -ForegroundColor Yellow
    } else {
        Write-Warning "-Apply given but SAMUS_WP_REMEDIATION_ENABLED is not armed (process env or docker\compose\.env)."
        Write-Warning "The one-shot will refuse the write (wired-dormant). To enact, arm it:"
        Write-Warning '  $env:SAMUS_WP_REMEDIATION_ENABLED = ''1''   # this shell only'
        Write-Warning "  or set SAMUS_WP_REMEDIATION_ENABLED=1 in docker\compose\.env (standing arm)"
    }
} elseif ($Diff) {
    $pyArgs += '--diff'
    Write-Host "==> Previewing WP dead-link fixes (read-only)" -ForegroundColor Cyan
} else {
    Write-Host "==> Inspecting WP dead-link markup (read-only)" -ForegroundColor Cyan
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

    # Optional Stripe key for the post-apply snapshot refresh.
    try {
        if (Test-HfSecret -Scope Samus -Name StripeApiKey) {
            Set-Item -Path 'Env:STRIPE_API_KEY' -Value (Get-HfSecret -Scope Samus -Name StripeApiKey)
            $exported += 'STRIPE_API_KEY'
        }
    } catch {
        Write-Warning "Stripe key load failed -- post-apply snapshot refresh will report 'unknown': $_"
    }

    if (-not $env:SAMUS_ENV) {
        $env:SAMUS_ENV = 'production'
        $exported += 'SAMUS_ENV'
    }
    $env:PYTHONIOENCODING = 'utf-8'
    $exported += 'PYTHONIOENCODING'

    if ($armForChild) {
        $env:SAMUS_WP_REMEDIATION_ENABLED = '1'
        $exported += 'SAMUS_WP_REMEDIATION_ENABLED'
    }

    # PS 5.1 wraps native-exe stderr as a terminating error under EAP=Stop; the
    # python one-shot logs to stderr, so localize EAP so the real exit code wins.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $prevConsoleEnc = $null
    try {
        $prevConsoleEnc = [Console]::OutputEncoding
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    } catch {
        Write-Verbose "Console encoding not settable (ConstrainedLanguage?): $_"
    }
    Push-Location $samusRoot
    try {
        & $venvPython @pyArgs
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
        if ($null -ne $prevConsoleEnc) {
            try { [Console]::OutputEncoding = $prevConsoleEnc } catch {}
        }
        $ErrorActionPreference = $prevEAP
    }
} finally {
    foreach ($var in $exported) {
        Remove-Item -Path "Env:$var" -ErrorAction SilentlyContinue
    }
}

if ($exitCode -ne 0) {
    Write-Warning "wp link surgery exited with code $exitCode"
}
exit $exitCode
