<#
.SYNOPSIS
  Import a Twilio-owned phone number into Vapi and bind it to the inbound
  receptionist assistant. One-time provisioning step.

.DESCRIPTION
  Reads VapiApiKey, TwilioAccountSid, and TwilioAuthToken from DPAPI,
  then calls the Vapi REST API to:
    1. List existing assistants to find the inbound receptionist
    2. Import the Twilio number bound to that assistant
    3. Output the resulting VapiInboundPhoneNumberId + VapiInboundAssistantId
       for DPAPI sealing

  After running, seal the two output values:
    Set-HfSecret -Scope Samus -Name VapiInboundAssistantId
    Set-HfSecret -Scope Samus -Name VapiInboundPhoneNumberId
  Then restart the stack: .\Start-SamusStack.ps1

.PARAMETER Number
  E.164 phone number to import (e.g. +15005550006).

.PARAMETER AssistantId
  Vapi assistant UUID for inbound call handling. If omitted, lists all
  assistants so you can pick one.

.PARAMETER DryRun
  Show what would happen without calling the Vapi import endpoint.

.EXAMPLE
  .\Import-TwilioNumber.ps1 -Number +15005550006
  .\Import-TwilioNumber.ps1 -Number +15005550006 -AssistantId <uuid>
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^\+1\d{10}$')]
    [string]$Number,

    [string]$AssistantId,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. Load DPAPI secrets
# ---------------------------------------------------------------------------
Import-Module (Join-Path $PSScriptRoot '..\..\_shared\scripts\Hustleforge.Secrets.psm1') -Force

$vapiKey    = Get-HfSecret -Scope Samus -Name VapiApiKey
$twilioSid  = Get-HfSecret -Scope Samus -Name TwilioAccountSid
$twilioAuth = Get-HfSecret -Scope Samus -Name TwilioAuthToken

if (-not $vapiKey)    { throw 'VapiApiKey not sealed. Run Set-HfSecret -Scope Samus -Name VapiApiKey' }
if (-not $twilioSid)  { throw 'TwilioAccountSid not sealed.' }
if (-not $twilioAuth) { throw 'TwilioAuthToken not sealed.' }

if (-not $twilioSid.StartsWith('AC')) {
    throw "TwilioAccountSid must start with 'AC' (got '$($twilioSid.Substring(0,2))'). An SK value is an API Key SID."
}

$headers = @{
    'Authorization' = "Bearer $vapiKey"
    'Content-Type'  = 'application/json'
}
$vapiBase = 'https://api.vapi.ai'

# ---------------------------------------------------------------------------
# 2. Resolve inbound assistant
# ---------------------------------------------------------------------------
if (-not $AssistantId) {
    Write-Host "`n=== Listing Vapi assistants ===" -ForegroundColor Cyan
    try {
        $resp = Invoke-RestMethod -Uri "$vapiBase/assistant?limit=100" -Headers $headers -Method Get
        $assistants = if ($resp -is [array]) { $resp } else { @($resp) }
    } catch {
        throw "Failed to list assistants: $_"
    }

    if ($assistants.Count -eq 0) {
        Write-Host 'No assistants found in Vapi. Create an inbound receptionist assistant first.' -ForegroundColor Red
        exit 1
    }

    Write-Host "`nFound $($assistants.Count) assistant(s):`n"
    $i = 1
    foreach ($a in $assistants) {
        $name = if ($a.name) { $a.name } else { '(unnamed)' }
        $model = if ($a.model -and $a.model.model) { $a.model.model } else { 'n/a' }
        Write-Host "  [$i] $($a.id)  $name  (model: $model)"
        $i++
    }

    Write-Host ''
    $choice = Read-Host 'Enter the number of the INBOUND receptionist assistant (or paste UUID)'
    if ($choice -match '^\d+$') {
        $idx = [int]$choice - 1
        if ($idx -lt 0 -or $idx -ge $assistants.Count) { throw "Invalid choice: $choice" }
        $AssistantId = $assistants[$idx].id
    } else {
        $AssistantId = $choice.Trim()
    }
}

Write-Host "`nUsing assistant: $AssistantId" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 3. Check for existing phone numbers (avoid double-import)
# ---------------------------------------------------------------------------
Write-Host "`n=== Checking existing phone numbers ===" -ForegroundColor Cyan
try {
    $existing = Invoke-RestMethod -Uri "$vapiBase/phone-number?limit=100" -Headers $headers -Method Get
    $numbers = if ($existing -is [array]) { $existing } else { @($existing) }
} catch {
    Write-Warning "Could not list phone numbers: $_"
    $numbers = @()
}

$already = $numbers | Where-Object { $_.number -eq $Number }
if ($already) {
    Write-Host "`nNumber $Number is already imported in Vapi:" -ForegroundColor Yellow
    Write-Host "  ID:        $($already.id)"
    Write-Host "  Provider:  $($already.provider)"
    Write-Host "  Assistant: $($already.assistantId)"
    Write-Host "`nTo rebind to a different assistant, use PATCH /phone-number/$($already.id)"
    Write-Host "`nSeal these values:"
    Write-Host "  VapiInboundAssistantId   = $($already.assistantId)"
    Write-Host "  VapiInboundPhoneNumberId = $($already.id)"
    exit 0
}

foreach ($n in $numbers) {
    $nName = if ($n.name) { $n.name } else { '(none)' }
    Write-Host "  Existing: $($n.number)  id=$($n.id)  name=$nName  provider=$($n.provider)"
}

# ---------------------------------------------------------------------------
# 4. Import the number
# ---------------------------------------------------------------------------
Write-Host "`n=== Importing $Number from Twilio ===" -ForegroundColor Cyan

$body = @{
    provider          = 'twilio'
    number            = $Number
    assistantId       = $AssistantId
    twilioAccountSid  = $twilioSid
    twilioAuthToken   = $twilioAuth
    name              = 'HustleForge Inbound'
} | ConvertTo-Json -Depth 4

if ($DryRun) {
    Write-Host "`n[DRY RUN] Would POST to $vapiBase/phone-number:" -ForegroundColor Yellow
    Write-Host $body
    exit 0
}

Write-Host "POSTing to Vapi /phone-number ..."
try {
    $result = Invoke-RestMethod -Uri "$vapiBase/phone-number" `
        -Headers $headers -Method Post -Body $body
} catch {
    $err = $_.Exception.Response
    if ($err) {
        $reader = [System.IO.StreamReader]::new($err.GetResponseStream())
        $errBody = $reader.ReadToEnd()
        Write-Host "Vapi error response: $errBody" -ForegroundColor Red
    }
    throw "Failed to import number: $_"
}

$phoneId = $result.id
Write-Host "`n=== SUCCESS ===" -ForegroundColor Green
Write-Host "Number:    $Number"
Write-Host "Provider:  $($result.provider)"
Write-Host "Phone ID:  $phoneId"
Write-Host "Assistant: $AssistantId"

# ---------------------------------------------------------------------------
# 5. Output sealing instructions
# ---------------------------------------------------------------------------
Write-Host "`n=== Next steps ===" -ForegroundColor Cyan
Write-Host "Seal these two values into DPAPI:`n"
Write-Host "  Set-HfSecret -Scope Samus -Name VapiInboundAssistantId"
Write-Host "    -> paste: $AssistantId"
Write-Host ""
Write-Host "  Set-HfSecret -Scope Samus -Name VapiInboundPhoneNumberId"
Write-Host "    -> paste: $phoneId"
Write-Host ""
Write-Host "Then restart the stack:"
Write-Host "  .\Stop-SamusStack.ps1; .\Start-SamusStack.ps1"
Write-Host ""
Write-Host "The samus-voice container will pick up the new env vars and"
Write-Host "route inbound calls on $Number to the receptionist assistant."
