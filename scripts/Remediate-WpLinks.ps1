<#
.SYNOPSIS
  Plan (or apply) checkout-link remediation on hustleforge.tech WordPress pages.

.DESCRIPTION
  Thin launcher around `python -m backend.catalog.wp_link_remediation`. It loads
  the Stripe read key + WordPress application-password credential from the DPAPI
  store (Scope=Samus) onto this process's env, runs from the Samus repo root, and
  scrubs the secrets in `finally` -- the same posture as Publish-OnboardingPage.ps1.

  The tool finds archived buy.stripe.com links on published WP pages and, for
  each one Stripe itself names a unique live successor for (same product +
  price), replaces the URL in the page's raw content. Test placeholders,
  ambiguous, or retired links are reported MANUAL and never touched.

  STRIPE_API_KEY is REQUIRED even to plan: without it the tool cannot fetch the
  live/archived link set and reports "nothing to fix". WordPress creds are only
  needed for -Apply, but are loaded either way (harmless, and the plan's WP
  content read is public).

  Wire-not-arm: -Apply writes to the LIVE site only when the capability is armed
  (SAMUS_WP_REMEDIATION_ENABLED). This launcher honors the arm decision recorded
  in docker\compose\.env -- the same ledger Start-SamusStack uses -- so a host
  run respects the operator's standing arm without a per-shell export. If it is
  not armed there (or in the process env), the python module refuses the write.

.EXAMPLE
  .\Remediate-WpLinks.ps1                # plan only (read-only)

.EXAMPLE
  .\Remediate-WpLinks.ps1 -Apply         # enact (if SAMUS_WP_REMEDIATION_ENABLED=1)
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

# DPAPI secret pull -- same shared module every Samus launcher uses.
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Shared secrets module not found at $sharedModule"
}
Import-Module $sharedModule -Force

# StripeApiKey is required to do anything; WordPress creds are required for the
# actual write (-Apply). Load all three so an -Apply run is fully wired.
$required = @(
    @{ DpapiName='StripeApiKey';         EnvVar='STRIPE_API_KEY' },
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
    throw "Aborting -- required credentials are not in the DPAPI store."
}

# Resolve the arm state for -Apply. Honor the process env first, then the
# operator's standing arm recorded in docker\compose\.env (the same ledger
# Start-SamusStack reads). This makes "armed in .env" govern host runs too.
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

$pyArgs = @('-m', 'backend.catalog.wp_link_remediation')
$armForChild = $false
if ($Apply) {
    $pyArgs += '--apply'
    $armForChild = Resolve-Armed
    if ($armForChild) {
        Write-Host "==> Applying WP link remediation (armed) - writes to the LIVE site" -ForegroundColor Yellow
    } else {
        Write-Warning "-Apply given but SAMUS_WP_REMEDIATION_ENABLED is not armed (process env or docker\compose\.env)."
        Write-Warning "The tool will plan + refuse the write (wired-dormant). To enact, arm it:"
        Write-Warning '  $env:SAMUS_WP_REMEDIATION_ENABLED = ''1''   # this shell only'
        Write-Warning "  or set SAMUS_WP_REMEDIATION_ENABLED=1 in docker\compose\.env (standing arm)"
    }
} else {
    Write-Host "==> Planning WP link remediation (read-only; pass -Apply to enact)" -ForegroundColor Cyan
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

    # Match the running stack's runtime env so fail-closed gates behave the same
    # as the containers (SAMUS_ENV=production). Operator override respected.
    if (-not $env:SAMUS_ENV) {
        $env:SAMUS_ENV = 'production'
        $exported += 'SAMUS_ENV'
    }
    $env:PYTHONIOENCODING = 'utf-8'
    $exported += 'PYTHONIOENCODING'

    # Only set the arm flag for the child when -Apply AND the arm decision
    # resolved true -- plan runs never need it, and an unarmed -Apply stays
    # wired-dormant (python refuses the write).
    if ($armForChild) {
        $env:SAMUS_WP_REMEDIATION_ENABLED = '1'
        $exported += 'SAMUS_WP_REMEDIATION_ENABLED'
    }

    # The module logs to stderr (httpx INFO, the SAMUS_ENV notice, etc.). Under
    # $ErrorActionPreference='Stop' PS 5.1 wraps each native-exe stderr line as a
    # terminating NativeCommandError, so a fully successful run would still throw
    # + exit nonzero. Localize EAP to 'Continue' around the call so the real
    # python exit code propagates. Mirrors Poll-Inbox.ps1.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # UTF-8 console is a nicety, not essential. Under WDAC ConstrainedLanguage
    # the [Console]/[Encoding] type access can be blocked, so degrade gracefully
    # rather than abort -- PYTHONIOENCODING above already governs the child's IO.
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

# Exit codes from the module: 0 = clean or all-auto-applied; 3 = manual fixes
# remain (surfaced for the operator, not a hard failure). Pass them through.
if ($exitCode -eq 3) {
    Write-Host "NOTE: manual fixes remain (see [manual] lines above)." -ForegroundColor Yellow
} elseif ($exitCode -ne 0) {
    Write-Warning "wp_link_remediation exited with code $exitCode"
}
exit $exitCode
