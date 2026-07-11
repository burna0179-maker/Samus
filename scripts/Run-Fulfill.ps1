<#
.SYNOPSIS
  Run the Samus customer-fulfillment orchestrator for one customer.

.DESCRIPTION
  Pulls every secret the orchestrator needs from the DPAPI store
  (Scope = Samus) and exports them to the child Python process's environment
  for the lifetime of the run, then invokes:

      python -m backend.fulfill --email <Email> --url <Url> ...

  Required secrets:
    HivemindPassword     -> NEO4J_PASSWORD        (customer state in memory)
    AwsAccessKeyId       -> AWS_ACCESS_KEY_ID     (SES email send)
    AwsSecretAccessKey   -> AWS_SECRET_ACCESS_KEY (SES email send)

  Optional secrets:
    AnthropicApiKey      -> ANTHROPIC_API_KEY     (SEO content drafts — falls back to templated)

  The orchestrator runs 5 steps:
    1. find or create the customer in Neo4j (idempotent on email)
    2. advance state to in_delivery
    3. run SEO audit + write markdown report
    4. email the report via SES (unless -NoSend)
    5. advance state to delivered

.PARAMETER Email
  Customer email address. Required.

.PARAMETER Url
  Customer's website URL to audit. Required.

.PARAMETER Name
  Customer's name (optional). Used in the email greeting.

.PARAMETER Company
  Customer's company name (optional).

.PARAMETER Keywords
  Comma-separated target keywords for the audit (optional).

.PARAMETER Tone
  Content draft tone: professional | friendly | technical (default: professional).

.PARAMETER NoSend
  Skip the email step. Still writes the report + advances state to delivered.

.EXAMPLE
  .\Run-Fulfill.ps1 -Email customer@example.com -Url https://acme-plumbing.example.com

.EXAMPLE
  .\Run-Fulfill.ps1 -Email c@x.com -Url https://x.com -Keywords "plumbing,yuba city" -Name "Alex"

.EXAMPLE
  # Dry-run: write the report but don't email the customer
  .\Run-Fulfill.ps1 -Email c@x.com -Url https://x.com -NoSend
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Email,
    [Parameter(Mandatory)][string]$Url,
    [Parameter()][string]$Name = "",
    [Parameter()][string]$Company = "",
    [Parameter()][string]$Keywords = "",
    [Parameter()][string]$Tone = "professional",
    [Parameter()][switch]$NoSend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Run from Samus repo root with .venv installed."
}

# Pull secrets from DPAPI. Same pattern as Start-SamusStack.ps1.
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Shared secrets module not found at $sharedModule. Cannot run without DPAPI access."
}
Import-Module $sharedModule -Force

$required = @(
    @{ DpapiName='HivemindPassword';   EnvVar='NEO4J_PASSWORD' },
    @{ DpapiName='AwsAccessKeyId';     EnvVar='AWS_ACCESS_KEY_ID' },
    @{ DpapiName='AwsSecretAccessKey'; EnvVar='AWS_SECRET_ACCESS_KEY' }
)
$optional = @(
    @{ DpapiName='AnthropicApiKey';    EnvVar='ANTHROPIC_API_KEY' }
)

$missing = @()
foreach ($s in $required) {
    if (-not (Test-HfSecret -Scope Samus -Name $s.DpapiName)) {
        $missing += $s.DpapiName
    }
}
if ($missing.Count -gt 0) {
    Write-Warning "Missing required secrets in DPAPI: $($missing -join ', ')"
    Write-Warning "Populate with:  Set-HfSecret -Scope Samus -Name <SecretName>"
    throw "Aborting -- required secrets are not in the DPAPI store."
}

# Force UTF-8 console output so unicode in the markdown report and audit
# fields renders correctly (default Windows console is cp1252).
$prevConsoleEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

# AWS region — orchestrator uses send_email_via_ses which falls back to
# settings.aws_region (default us-west-1). Set both env-var spellings so
# boto3 + sibling SDKs resolve the same region.
if (-not $env:AWS_REGION)         { $env:AWS_REGION         = 'us-west-1' }
if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = 'us-west-1' }

# Export DPAPI secrets to the child process's env. Tracked for the finally scrub.
$exportedVars = @()
try {
    foreach ($s in $required) {
        Set-Item -Path "Env:$($s.EnvVar)" -Value (Get-HfSecret -Scope Samus -Name $s.DpapiName)
        $exportedVars += $s.EnvVar
    }
    foreach ($s in $optional) {
        if (Test-HfSecret -Scope Samus -Name $s.DpapiName) {
            Set-Item -Path "Env:$($s.EnvVar)" -Value (Get-HfSecret -Scope Samus -Name $s.DpapiName)
            $exportedVars += $s.EnvVar
        }
    }

    $pyArgs = @(
        '-m', 'backend.fulfill',
        '--email', $Email,
        '--url',   $Url,
        '--tone',  $Tone
    )
    if ($Name)     { $pyArgs += @('--name', $Name) }
    if ($Company)  { $pyArgs += @('--company', $Company) }
    if ($Keywords) { $pyArgs += @('--keywords', $Keywords) }
    if ($NoSend)   { $pyArgs += '--no-send' }

    Push-Location $samusRoot
    try {
        & $venvPython @pyArgs
        $exit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    foreach ($var in $exportedVars) {
        Remove-Item -Path "Env:$var" -ErrorAction SilentlyContinue
    }
    Remove-Item -Path Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
    [Console]::OutputEncoding = $prevConsoleEnc
}

exit $exit
