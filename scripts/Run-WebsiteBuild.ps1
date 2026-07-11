<#
.SYNOPSIS
  Drive the website-build walk-through (backend/website/cli.py) with the Wix
  credentials loaded from the DPAPI secret store into the subprocess env — the
  API key is decrypted straight into the Python process and is never printed.

.DESCRIPTION
  Thin, secure launcher. It:
    * loads WIX_API_KEY / WIX_ACCOUNT_ID (and optional collection id) from the
      Samus DPAPI store (Scope=Samus) into the process env,
    * pins SAMUS_STATE_ROOT to the host artifact tree so the build state
      persists across invocations (every verb resumes the same order),
    * forwards all remaining arguments to ``python -m backend.website.cli``.

  Secrets read from DPAPI (Scope=Samus), seed in a REAL PC console as Alex
  (never via the in-session ``!`` prefix, which leaks masked prompts). ``-Name``
  is the fixed label; the value is pasted at the masked prompt. ``-Force`` is
  required: -Scope Samus is an agent scope and this host run is Alex, so it
  needs an Alex-DPAPI copy the Alex-run wrapper can decrypt.
    Set-HfSecret -Scope Samus -Name WixApiKey -Force       # paste API token at prompt
    Set-HfSecret -Scope Samus -Name WixAccountId -Force    # paste account-id GUID

  Creds are OPTIONAL for rehearsal — without them the early stages (brief) run
  and the Wix-touching stages PARK honestly. The outward stages (publish,
  deliver) additionally need -LivePublish; autonomy needs -Autonomous. Both are
  OFF unless you pass the switch, so a normal run is supervised + non-outward.

.EXAMPLE
  # one-time, in a real console:
  Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1
  Set-HfSecret -Scope Samus -Name WixApiKey
  Set-HfSecret -Scope Samus -Name WixAccountId

.EXAMPLE
  .\Run-WebsiteBuild.ps1 start   --order scripts\website_orders\harmony.json
  .\Run-WebsiteBuild.ps1 approve --order-id wb-abc123 --stage brief
  .\Run-WebsiteBuild.ps1 advance --order-id wb-abc123
  .\Run-WebsiteBuild.ps1 status  --order-id wb-abc123
#>
[CmdletBinding()]
param(
    [switch]$LivePublish,
    [switch]$Autonomous,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython."
}

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) { throw "Secrets module not found at $sharedModule." }
Import-Module $sharedModule -Force

if (-not $CliArgs -or $CliArgs.Count -eq 0) {
    throw "No CLI args. e.g.  .\Run-WebsiteBuild.ps1 status --order-id wb-xxxx"
}

# --- host state root (persists the build across invocations) -----------------
$artifactRoot = Join-Path $samusRoot '.data\host_artifacts'
$stateRoot    = Join-Path $artifactRoot 'state'
if (-not (Test-Path $stateRoot)) { New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null }

# --- env push (always restored in finally) -----------------------------------
$envBackup = @{
    'WIX_API_KEY'                 = [Environment]::GetEnvironmentVariable('WIX_API_KEY', 'Process')
    'WIX_ACCOUNT_ID'              = [Environment]::GetEnvironmentVariable('WIX_ACCOUNT_ID', 'Process')
    'WIX_CONTENT_COLLECTION_ID'   = [Environment]::GetEnvironmentVariable('WIX_CONTENT_COLLECTION_ID', 'Process')
    'SAMUS_STATE_ROOT'            = [Environment]::GetEnvironmentVariable('SAMUS_STATE_ROOT', 'Process')
    'SAMUS_WEBSITE_LIVE_PUBLISH'  = [Environment]::GetEnvironmentVariable('SAMUS_WEBSITE_LIVE_PUBLISH', 'Process')
    'SAMUS_WEBSITE_AUTONOMOUS_ENABLED' = [Environment]::GetEnvironmentVariable('SAMUS_WEBSITE_AUTONOMOUS_ENABLED', 'Process')
    'PYTHONIOENCODING'            = [Environment]::GetEnvironmentVariable('PYTHONIOENCODING', 'Process')
}

$exit = 1
try {
    Set-Item -Path 'Env:SAMUS_STATE_ROOT' -Value $stateRoot
    Set-Item -Path 'Env:PYTHONIOENCODING' -Value 'utf-8'

    # Credentials: load if present; warn (don't fail) if absent so rehearsal
    # of the early stages still works. Never echo the values.
    if (Test-HfSecret -Scope Samus -Name WixApiKey) {
        Set-Item -Path 'Env:WIX_API_KEY' -Value (Get-HfSecret -Scope Samus -Name WixApiKey)
        Write-Host "WIX_API_KEY loaded from DPAPI." -ForegroundColor Green
    } else {
        Write-Warning "WixApiKey not in Samus DPAPI store. Wix stages will PARK. Seed: Set-HfSecret -Scope Samus -Name WixApiKey"
    }
    if (Test-HfSecret -Scope Samus -Name WixAccountId) {
        Set-Item -Path 'Env:WIX_ACCOUNT_ID' -Value (Get-HfSecret -Scope Samus -Name WixAccountId)
    }
    if (Test-HfSecret -Scope Samus -Name WixContentCollectionId) {
        Set-Item -Path 'Env:WIX_CONTENT_COLLECTION_ID' -Value (Get-HfSecret -Scope Samus -Name WixContentCollectionId)
    }

    if ($LivePublish) {
        Set-Item -Path 'Env:SAMUS_WEBSITE_LIVE_PUBLISH' -Value 'true'
        Write-Host "LIVE PUBLISH ENABLED — publish/deliver stages may take outward action." -ForegroundColor Yellow
    }
    if ($Autonomous) {
        Set-Item -Path 'Env:SAMUS_WEBSITE_AUTONOMOUS_ENABLED' -Value 'true'
        Write-Host "AUTONOMOUS ENABLED — per-stage operator approval is bypassed." -ForegroundColor Yellow
    }

    Push-Location $samusRoot
    try {
        & $venvPython -m backend.website.cli @CliArgs
        $exit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    foreach ($k in $envBackup.Keys) {
        if ($null -eq $envBackup[$k]) {
            Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$k" -Value $envBackup[$k]
        }
    }
}

exit $exit
