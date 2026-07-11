<#
.SYNOPSIS
  Ingest a bank/Cash App activity CSV into Samus's bank_activity ledger.

.DESCRIPTION
  Thin PowerShell wrapper around ``python -m backend.finance.bank_activity``.
  The ingester is idempotent: re-running against the same CSV or a CSV
  with overlapping rows is safe — external_id dedup skips duplicates.

  Once Mercury API access is available, a companion Ingest-MercuryActivity.ps1
  will call the same ledger append surface with source="mercury_api".

.PARAMETER Path
  Path to the Cash App / bank activity CSV export.

.PARAMETER DryRun
  Parse and print summary without appending to the ledger.

.EXAMPLE
  .\Ingest-BankActivity.ps1 -Path "C:\Users\Alex\Downloads\hustleforge_llc_activity_report_1783384525.csv"
  .\Ingest-BankActivity.ps1 -Path .\activity.csv -DryRun
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Path,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot = Resolve-Path (Join-Path $here '..')
$venvPy    = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPy)) { throw "venv python not found: $venvPy" }
if (-not (Test-Path $Path))   { throw "CSV not found: $Path" }

$args = @('-m', 'backend.finance.bank_activity', $Path)
if ($DryRun) { $args += '--dry-run' }

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
Push-Location $samusRoot
try {
    & $venvPy @args
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
}

exit $exit
