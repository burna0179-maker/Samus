<#
.SYNOPSIS
  Print the Samus morning briefing — debt actions + cash + sales + gaps.

.DESCRIPTION
  Pulls STRIPE_API_KEY and the Samus AWS credentials from the DPAPI store
  (Scope = Samus) and exports them to the child Python process's environment.
  Stripe powers the live CASH roundtrip; the AWS creds let the SALES section
  read CRM CallState for the outreach follow-ups lane. Plaintext secrets only
  live in this PowerShell process for the lifetime of the child invocation;
  the env vars are scrubbed in the ``finally`` block before this script returns.

  Both are OPTIONAL — if Stripe is absent the CASH section reports
  "stripe_api_key_unset" inline; if the AWS creds are absent the SALES
  follow-ups lane is simply omitted. Every other section (debt actions, info
  gaps, debt portfolio, call list, hardship context) is computed from local
  yaml + needs neither.

.EXAMPLE
  .\Show-Morning.ps1

.EXAMPLE
  # Pipe to a file (auto-strips colors via --no-color when stdout isn't a TTY)
  .\Show-Morning.ps1 -NoColor | Out-File morning.txt
#>

[CmdletBinding()]
param(
    [Parameter()][switch]$NoColor
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Run from Samus repo root with .venv installed."
}

# Pull Stripe key + AWS creds from DPAPI if available. Both are non-fatal if
# missing: the CASH section shows stripe_api_key_unset, and the SALES
# follow-ups lane is omitted — the briefing still prints either way.
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
$awsLoaded = $false
if (Test-Path $sharedModule) {
    Import-Module $sharedModule -Force
    try {
        if (Test-HfSecret -Scope Samus -Name StripeApiKey) {
            $env:STRIPE_API_KEY = Get-HfSecret -Scope Samus -Name StripeApiKey
        }
    } catch {
        Write-Warning "Stripe key load failed -- briefing will skip live Stripe pull: $_"
    }
    # AWS creds for the SALES follow-ups lane (reads CRM CallState in
    # DynamoDB). Optional — same degrade-gracefully posture as the Stripe key.
    try {
        if ((Test-HfSecret -Scope Samus -Name AwsAccessKeyId) -and
            (Test-HfSecret -Scope Samus -Name AwsSecretAccessKey)) {
            $env:AWS_ACCESS_KEY_ID     = Get-HfSecret -Scope Samus -Name AwsAccessKeyId
            $env:AWS_SECRET_ACCESS_KEY = Get-HfSecret -Scope Samus -Name AwsSecretAccessKey
            $env:AWS_REGION            = 'us-west-1'
            $env:AWS_DEFAULT_REGION    = 'us-west-1'
            $awsLoaded = $true
        }
    } catch {
        Write-Warning "AWS creds load failed -- briefing will skip the follow-ups lane: $_"
    }
}

# Pin SAMUS_ARTIFACT_ROOT to the D:-side host-artifacts tree -- the same path
# the scheduled Send-Morning brief and Run-ProspectingDaily use. Without it
# storage.root() falls back to the E:\ default and the call-list pickup misses
# today's daily_calls/ file. Only set when the caller hasn't already.
$artifactRootSet = $false
if (-not $env:SAMUS_ARTIFACT_ROOT) {
    $env:SAMUS_ARTIFACT_ROOT = 'D:\Hustleforge\Samus\.data\host_artifacts'
    $artifactRootSet = $true
}

# Force UTF-8 end-to-end so em-dashes / unicode in the yaml data files
# render correctly through the PowerShell console (default cp1252 mangles
# them to '?'). This is restored to whatever it was before in the finally
# block so we don't leak the change into the caller's shell.
$prevConsoleEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

# Run the python module. -NoColor maps to --no-color on the python side.
$pyArgs = @('-m', 'backend.morning')
if ($NoColor) { $pyArgs += '--no-color' }

Push-Location $samusRoot
try {
    & $venvPython @pyArgs
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    if ($env:STRIPE_API_KEY) {
        Remove-Item -Path Env:STRIPE_API_KEY -ErrorAction SilentlyContinue
    }
    if ($awsLoaded) {
        Remove-Item -Path Env:AWS_ACCESS_KEY_ID     -ErrorAction SilentlyContinue
        Remove-Item -Path Env:AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
        Remove-Item -Path Env:AWS_REGION            -ErrorAction SilentlyContinue
        Remove-Item -Path Env:AWS_DEFAULT_REGION    -ErrorAction SilentlyContinue
    }
    Remove-Item -Path Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
    if ($artifactRootSet) {
        Remove-Item -Path Env:SAMUS_ARTIFACT_ROOT -ErrorAction SilentlyContinue
    }
    [Console]::OutputEncoding = $prevConsoleEnc
}

exit $exit
