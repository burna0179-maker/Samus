<#
.SYNOPSIS
  DPAPI-backed credential store for Samus local-dev secrets.

.DESCRIPTION
  Stores secrets as DPAPI-encrypted text files under
  ``$env:LOCALAPPDATA\Samus\credentials\<name>.cred``. DPAPI binds the
  ciphertext to the current Windows user on the current machine — no other
  user, and no copy of the file on another machine, can decrypt it.

  Same idea as ``C:\hardening_baseline\agent_credentials\svc_Samus.cred`` but
  in the per-user profile (no admin needed). Anything that needs the secret
  reads it via Get-SamusSecret; nothing reads it from $env:NEO4J_PASSWORD in
  plaintext at rest.

.EXAMPLE
  # one-time setup
  Import-Module .\Samus.Secrets.psm1
  Set-SamusSecret -Name HivemindPassword

  # later, exporting into the current shell for docker compose
  $env:NEO4J_PASSWORD = Get-SamusSecret -Name HivemindPassword
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:SamusSecretRoot = Join-Path $env:LOCALAPPDATA 'Samus\credentials'


function Get-SamusSecretPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name
    )
    if ($Name -notmatch '^[A-Za-z0-9._-]{1,64}$') {
        throw "Secret name must be 1-64 chars of [A-Za-z0-9._-]; got '$Name'."
    }
    if (-not (Test-Path $script:SamusSecretRoot)) {
        New-Item -ItemType Directory -Path $script:SamusSecretRoot -Force | Out-Null
    }
    Join-Path $script:SamusSecretRoot ($Name + '.cred')
}


function Set-SamusSecret {
    <#
    .SYNOPSIS Encrypt and store a secret under the given name.
    .DESCRIPTION
      Prompts for the secret interactively unless -Value is provided. The
      secret is DPAPI-encrypted with the current user's profile key and
      written to disk. Prefer the interactive prompt — passing -Value as
      plaintext leaks it into shell history.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name,
        [Parameter()][SecureString]$Value
    )
    if (-not $Value) {
        $Value = Read-Host -AsSecureString -Prompt "Enter value for '$Name'"
    }
    if ($Value.Length -eq 0) {
        throw "Empty secret refused."
    }
    $cipher = ConvertFrom-SecureString -SecureString $Value
    $path = Get-SamusSecretPath -Name $Name
    Set-Content -Path $path -Value $cipher -Encoding ASCII -NoNewline
    # Tight ACL: SYSTEM + current user only.
    $acl = Get-Acl $path
    $acl.SetAccessRuleProtection($true, $false)
    $rule1 = New-Object System.Security.AccessControl.FileSystemAccessRule(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User,
        'FullControl', 'Allow')
    $rule2 = New-Object System.Security.AccessControl.FileSystemAccessRule(
        'NT AUTHORITY\SYSTEM', 'FullControl', 'Allow')
    $acl.AddAccessRule($rule1)
    $acl.AddAccessRule($rule2)
    Set-Acl -Path $path -AclObject $acl
    Write-Host "Stored '$Name' at $path" -ForegroundColor Green
}


function Get-SamusSecret {
    <#
    .SYNOPSIS Decrypt the named secret and return as a plain string.
    .DESCRIPTION
      Reads the DPAPI-encrypted ciphertext, decrypts with the current user's
      profile key, and returns the plaintext. Throws if the file is missing
      or the current user/machine can't decrypt it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name
    )
    $path = Get-SamusSecretPath -Name $Name
    if (-not (Test-Path $path)) {
        throw "Secret '$Name' not found at $path. Run Set-SamusSecret -Name $Name first."
    }
    $cipher = Get-Content -Path $path -Raw -Encoding ASCII
    $secure = ConvertTo-SecureString -String $cipher
    try {
        $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        } finally {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        }
    } finally {
        $secure.Dispose()
    }
}


function Test-SamusSecret {
    <#
    .SYNOPSIS Return $true if the named secret exists and can be decrypted.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name
    )
    $path = Get-SamusSecretPath -Name $Name
    if (-not (Test-Path $path)) { return $false }
    try {
        $null = Get-SamusSecret -Name $Name
        return $true
    } catch {
        return $false
    }
}


function Remove-SamusSecret {
    <#
    .SYNOPSIS Delete the named secret from the local store.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name
    )
    $path = Get-SamusSecretPath -Name $Name
    if (Test-Path $path) {
        Remove-Item -Path $path -Force
        Write-Host "Removed '$Name'." -ForegroundColor Yellow
    } else {
        Write-Host "Secret '$Name' was not present." -ForegroundColor Yellow
    }
}


function Get-SamusSecretList {
    <#
    .SYNOPSIS List the names of all stored Samus secrets.
    #>
    [CmdletBinding()]
    param()
    if (-not (Test-Path $script:SamusSecretRoot)) {
        return @()
    }
    Get-ChildItem -Path $script:SamusSecretRoot -Filter '*.cred' -File |
        ForEach-Object { $_.BaseName }
}


Export-ModuleMember -Function `
    Set-SamusSecret, Get-SamusSecret, Test-SamusSecret, `
    Remove-SamusSecret, Get-SamusSecretList
