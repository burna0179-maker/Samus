<#
.SYNOPSIS
  Run every retainer monthly cycle whose due-date has passed.

.DESCRIPTION
  Wraps ``python -m backend.retainer.run_due_cycles`` with the same secret-
  handling pattern as Send-Morning.ps1: DPAPI-scoped secrets pushed onto the
  child Python process's env, restored in the finally block.

  Secrets read (optional — missing one disables the dependent function):

    HivemindPassword           -> NEO4J_PASSWORD                (CustomerStore reads)
    SendgridApiKey             -> SENDGRID_API_KEY              (cycle's report send)
    SendgridFromEmail          -> SENDGRID_FROM_EMAIL           (verified sender)
    StripeApiKey               -> STRIPE_API_KEY                (CRM/finance dispatches)
    SamusHmacKey               -> SAMUS_SHARED_HMAC_KEY         (CRM dispatch signing)

  Pass -DryRun to list due cycles without executing them.

  Exit code:
    0  no cycles due, OR every due cycle ran successfully
    1  at least one cycle failed
    2  hard crash before/during iteration

.PARAMETER DryRun
  Print the list of due cycles without running them.

.EXAMPLE
  .\Run-Retainer-Cycles.ps1

.EXAMPLE
  .\Run-Retainer-Cycles.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Run from Samus repo root with .venv installed."
}

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Required secrets module not found at $sharedModule."
}
Import-Module $sharedModule -Force

# --- env helpers (push + restore in finally) ---------------------------------

$envBackup = @{}

function Push-EnvVar([string]$Name, [string]$Value) {
    if (-not $envBackup.ContainsKey($Name)) {
        $envBackup[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    Set-Item -Path "Env:$Name" -Value $Value
}

function Restore-EnvVars {
    foreach ($k in $envBackup.Keys) {
        $v = $envBackup[$k]
        if ($null -eq $v) {
            Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$k" -Value $v
        }
    }
}

function Push-OptionalSecret([string]$SecretName, [string]$EnvName) {
    try {
        if (Test-HfSecret -Scope Samus -Name $SecretName) {
            Push-EnvVar $EnvName (Get-HfSecret -Scope Samus -Name $SecretName)
        }
    } catch {
        Write-Warning "Optional secret $SecretName not loaded: $_"
    }
}

# --- load secrets ------------------------------------------------------------

Push-OptionalSecret 'HivemindPassword'  'NEO4J_PASSWORD'
Push-OptionalSecret 'SendgridApiKey'    'SENDGRID_API_KEY'
Push-OptionalSecret 'SendgridFromEmail' 'SENDGRID_FROM_EMAIL'
Push-OptionalSecret 'StripeApiKey'      'STRIPE_API_KEY'
Push-OptionalSecret 'SamusHmacKey'      'SAMUS_SHARED_HMAC_KEY'

Push-EnvVar 'PYTHONIOENCODING' 'utf-8'

# --- invoke python ----------------------------------------------------------

$prevConsoleEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Push-Location $samusRoot
try {
    $args = @('-m', 'backend.retainer.run_due_cycles')
    if ($DryRun) { $args += '--dry-run' }
    & $venvPython @args
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    Restore-EnvVars
    [Console]::OutputEncoding = $prevConsoleEnc
}

exit $exit
