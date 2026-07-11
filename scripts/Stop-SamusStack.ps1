<#
.SYNOPSIS Tear down the Samus Docker Compose stack.

.DESCRIPTION
  Counterpart to Start-SamusStack.ps1. No secrets are needed for `compose down`
  but the script honors the same compose-file + env-file paths so the call site
  doesn't have to know them.

.PARAMETER RemoveVolumes
  Pass -v to docker compose down (drops samus-data volume + Neo4j-related volumes
  if any). Default false — preserves data across restarts.
#>

[CmdletBinding()]
param(
    [Parameter()][switch]$RemoveVolumes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent (Resolve-Path $MyInvocation.MyCommand.Path)
$samusRoot = Resolve-Path (Join-Path $here '..')
$repoRoot  = Resolve-Path (Join-Path $samusRoot '..')
$composeFile = Join-Path $samusRoot 'docker\compose\docker-compose.samus.yml'
$envFile     = Join-Path $samusRoot 'docker\compose\.env'

Push-Location $repoRoot
try {
    $composeArgs = @('compose', '-f', $composeFile, '--env-file', $envFile, 'down')
    if ($RemoveVolumes) { $composeArgs += '-v' }
    Write-Host "docker $($composeArgs -join ' ')" -ForegroundColor Cyan
    & docker @composeArgs
} finally {
    Pop-Location
}
