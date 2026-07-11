<#
.SYNOPSIS
  Open a tracked CRM Opportunity for a deal that materialised outside the
  first-dial booked-call flow.

.DESCRIPTION
  The booked->Opportunity wiring in Log-Call.ps1 only fires when a call is
  logged 'booked' at first-dial time. A deal that lands later -- a follow-up
  call that converts, or a hand-dialed prospect who agreed to a specific
  product -- needs this to enter the pipeline.

  The minted opportunity_id ("op_...") is the exact-attribution key. Tag a
  Stripe buy link with ?client_reference_id=<opportunity_id> and the finance
  webhook advances THIS opportunity to closed_won when the customer pays.

  Non-interactive: one Opportunity per invocation. Prints the JSON result
  (including the new opportunity_id). Exit 0 on a clean create; exit 1 when
  the prospect already has an Opportunity (use -Force) or the write degraded.

  AWS credentials are pulled from the DPAPI store (Scope = Samus) for the
  child Python process and scrubbed from the environment on exit.

.EXAMPLE
  .\Create-Opportunity.ps1 -ProspectId pr_xxx -Name "Acme - SEO Audit" `
      -IntentScore 85 -ServiceInterest seo_audit -NextStep "sent buy link" `
      -AssignedTo operator@example.com
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]   $ProspectId,
    [string]   $Name          = '',
    [int]      $IntentScore    = 0,
    [string[]] $ServiceInterest = @(),
    [string]   $NextStep       = '',
    [string]   $AssignedTo     = '',
    [string]   $MonthlyBudget  = '',
    [switch]   $Force,
    # --- strategy bandit attribution snapshot (Unit 3) ---------------------
    # The operator copies these from the prospect's call-list row so the
    # eventual deal close credits the bandit arm that picked the prospect.
    # Industry + PolicyFamily are the composite arm; the three social/owner
    # switches are booleans ("enrichment found one").
    [string]   $Industry       = '',
    [string]   $PolicyFamily   = '',
    [int]      $SeoScore       = 0,
    [switch]   $OwnerEmailFound,
    [switch]   $SocialFacebookFound,
    [switch]   $SocialInstagramFound,
    # --- per-prospect LLM cost (strategy-integration build, Unit 4) --------
    # The operator copies the prospect's llm_cost_usd call-list column here so
    # the eventual deal's reward signal can weigh a cheap win against an
    # expensive one. Defaults to 0 (no captured discovery cost).
    [double]   $TokenCostUsd   = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Run from the Samus repo root with .venv installed."
}

# --- AWS credentials from DPAPI (CRM writes to DynamoDB) --------------------
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "secrets module not found at $sharedModule"
}
Import-Module $sharedModule -Force
foreach ($pair in @(
        @{ Secret = 'AwsAccessKeyId';     Env = 'AWS_ACCESS_KEY_ID' },
        @{ Secret = 'AwsSecretAccessKey'; Env = 'AWS_SECRET_ACCESS_KEY' })) {
    if (-not (Test-HfSecret -Scope Samus -Name $pair.Secret)) {
        throw "DPAPI secret '$($pair.Secret)' (Scope=Samus) missing - CRM write needs AWS creds."
    }
    Set-Item -Path "Env:$($pair.Env)" -Value (Get-HfSecret -Scope Samus -Name $pair.Secret)
}
$env:AWS_REGION         = 'us-west-1'
$env:AWS_DEFAULT_REGION = 'us-west-1'
$env:PYTHONIOENCODING   = 'utf-8'
$prevEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    $cliArgs = @('-m', 'backend.crm.create_opportunity', '--prospect-id', $ProspectId)
    if ($Name)          { $cliArgs += @('--name', $Name) }
    if ($IntentScore)   { $cliArgs += @('--intent-score', "$IntentScore") }
    foreach ($svc in $ServiceInterest) {
        if ($svc) { $cliArgs += @('--service-interest', $svc) }
    }
    if ($NextStep)      { $cliArgs += @('--next-step', $NextStep) }
    if ($AssignedTo)    { $cliArgs += @('--assigned-to', $AssignedTo) }
    if ($MonthlyBudget) { $cliArgs += @('--monthly-budget', $MonthlyBudget) }
    if ($Force)         { $cliArgs += '--force' }
    # Strategy bandit attribution snapshot (Unit 3).
    if ($Industry)              { $cliArgs += @('--industry', $Industry) }
    if ($PolicyFamily)          { $cliArgs += @('--policy-family', $PolicyFamily) }
    if ($SeoScore)              { $cliArgs += @('--seo-score', "$SeoScore") }
    if ($OwnerEmailFound)       { $cliArgs += '--owner-email-found' }
    if ($SocialFacebookFound)   { $cliArgs += '--social-facebook-found' }
    if ($SocialInstagramFound)  { $cliArgs += '--social-instagram-found' }
    # Per-prospect LLM cost (Unit 4) — formatted culture-invariantly so a
    # comma-decimal operator locale can't hand Python an unparseable float.
    if ($TokenCostUsd -gt 0) {
        $cliArgs += @('--token-cost-usd',
            $TokenCostUsd.ToString([System.Globalization.CultureInfo]::InvariantCulture))
    }

    Push-Location $samusRoot
    try {
        & $venvPython @cliArgs
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($code -eq 0) {
        Write-Host "Opportunity created - see the opportunity_id in the JSON above." -ForegroundColor Green
    } else {
        Write-Host "Not created - the prospect already has an Opportunity (use -Force) or the write degraded." -ForegroundColor Yellow
    }
    exit $code
}
finally {
    Remove-Item -Path Env:AWS_ACCESS_KEY_ID     -ErrorAction SilentlyContinue
    Remove-Item -Path Env:AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
    Remove-Item -Path Env:AWS_REGION            -ErrorAction SilentlyContinue
    Remove-Item -Path Env:AWS_DEFAULT_REGION    -ErrorAction SilentlyContinue
    Remove-Item -Path Env:PYTHONIOENCODING      -ErrorAction SilentlyContinue
    [Console]::OutputEncoding = $prevEnc
}
