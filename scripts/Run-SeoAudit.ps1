<#
.SYNOPSIS
  Fire the full Samus SEO audit + report pipeline against a target URL.

.DESCRIPTION
  End-to-end: audit_site -> optimize_page -> generate_content -> write_seo_report.
  Writes a customer-facing markdown report under
  ``$artifactRoot/customers/<slug>/seo_report.md`` plus an audit-ledger entry.

  Pins SAMUS_ARTIFACT_ROOT + SAMUS_SEO_AUDIT_PATH to the same D:-side
  host_artifacts tree the rest of the host-run Samus tooling uses (see
  [[project-samus-prospecting-geo-rings]] for the ACL background).

  Secrets read from DPAPI (Scope=Samus):
    AnthropicApiKey         -> ANTHROPIC_API_KEY        (optional, enables LLM content drafts)
    GooglePagespeedApiKey   -> GOOGLE_PAGESPEED_API_KEY (optional, enables PageSpeed metrics)

  Both are optional -- audit + report still produce useful output without them.

.PARAMETER Url
  Target site URL. Default: https://www.hustleforge.tech .

.PARAMETER CustomerLabel
  Label used in the report cover + the artifact directory slug. Defaults to
  the URL host.

.PARAMETER Keywords
  Comma-separated target keywords for optimization. Optional.

.EXAMPLE
  .\Run-SeoAudit.ps1                                         # audit hustleforge.tech
  .\Run-SeoAudit.ps1 -Url https://www.somebiz.com            # audit another site
  .\Run-SeoAudit.ps1 -Url https://www.hustleforge.tech `
                     -Keywords "workflow automation,small business operations"
#>

[CmdletBinding()]
param(
    [Parameter()][string]$Url = 'https://www.hustleforge.tech',
    [Parameter()][string]$CustomerLabel = '',
    [Parameter()][string]$Keywords = ''
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
if (-not (Test-Path $sharedModule)) { throw "Required secrets module not found at $sharedModule." }
Import-Module $sharedModule -Force

# Artifact root + audit ledger path (D:-side, Alex-writable -- same as prospecting wrapper)
$artifactRoot = 'D:\Hustleforge\Samus\.data\host_artifacts'
if (-not (Test-Path $artifactRoot)) { New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null }
$seoLedgerPath = Join-Path $artifactRoot 'evidence\seo\seo_audit.jsonl'

# Env push: backup current values + always restore in finally
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
        if ($null -eq $v) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }
        else { Set-Item -Path "Env:$k" -Value $v }
    }
}

Push-EnvVar 'SAMUS_ARTIFACT_ROOT'       $artifactRoot
Push-EnvVar 'SAMUS_SEO_AUDIT_PATH'      $seoLedgerPath
Push-EnvVar 'PYTHONIOENCODING'          'utf-8'

if (Test-HfSecret -Scope Samus -Name AnthropicApiKey) {
    Push-EnvVar 'ANTHROPIC_API_KEY' (Get-HfSecret -Scope Samus -Name AnthropicApiKey)
    Write-Host "ANTHROPIC_API_KEY    loaded (content drafts may use LLM)"
} else {
    Write-Host "ANTHROPIC_API_KEY    not in DPAPI (content drafts will use templated path)"
}
if (Test-HfSecret -Scope Samus -Name GooglePagespeedApiKey) {
    Push-EnvVar 'GOOGLE_PAGESPEED_API_KEY' (Get-HfSecret -Scope Samus -Name GooglePagespeedApiKey)
    Write-Host "PAGESPEED_API_KEY    loaded (Core Web Vitals will be included)"
} else {
    Write-Host "PAGESPEED_API_KEY    not in DPAPI (audit will skip PageSpeed metrics)"
}

# Build the Python invocation inline -- no separate file needed.
$kwArg = if ($Keywords) { "$Keywords" } else { '' }
$labelArg = if ($CustomerLabel) { "$CustomerLabel" } else { '' }

Push-EnvVar 'SAMUS_AUDIT_URL'     $Url
Push-EnvVar 'SAMUS_AUDIT_LABEL'   $labelArg
Push-EnvVar 'SAMUS_AUDIT_KW'      $kwArg

$pyScript = Join-Path $here '_run_seo_audit.py'
if (-not (Test-Path $pyScript)) {
    throw "Helper script not found at $pyScript -- redeploy Run-SeoAudit.ps1 sibling file _run_seo_audit.py."
}

Push-Location $samusRoot
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $venvPython $pyScript
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
    Restore-EnvVars
}

exit $exit
