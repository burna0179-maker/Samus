<#
.SYNOPSIS
  Reconcile founder Capital Contributions from bank_activity into Executive Docs.

.DESCRIPTION
  Reads Samus's bank_activity.jsonl ledger and updates the two Executive Docs
  documents that track founder funding:
    - Executive Docs/03_Ownership/Capital_Contributions_Ledger.md
    - Executive Docs/07_Funding/Founder_Funding_Tracker.md

  Idempotent — external_id state is tracked in
  Executive Docs/03_Ownership/.state/capital_reconcile.json.

  Run after every Ingest-BankActivity.ps1 that ingested new founder-side rows.

.PARAMETER DryRun
  Preview without writing the docs.

.EXAMPLE
  .\Reconcile-CapitalContributions.ps1
  .\Reconcile-CapitalContributions.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot = Resolve-Path (Join-Path $here '..')
$venvPy    = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPy)) { throw "venv python not found: $venvPy" }

$pyArgs = @('scripts/reconcile_capital_contributions.py')
if ($DryRun) { $pyArgs += '--dry-run' }

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
Push-Location $samusRoot
try {
    & $venvPy @pyArgs
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
}

exit $exit
