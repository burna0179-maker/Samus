<#
.SYNOPSIS
  Clean up SendGrid domain authentication so @hustleforge.tech sends are
  DMARC-aligned: set the one VALID authenticated domain as the account default
  and remove the dead (never-validated) duplicate records.

.DESCRIPTION
  Diagnosis 2026-07-02 (backend/outreach/email_batch_analyzer flagged
  block_rate=6.3%, delivery=79%): the SendGrid account has 4 domain-auth records
  all for 'www.hustleforge.tech', only ONE valid (subdomain em5538), and NONE
  set as default. Mail is sent From @hustleforge.tech (the root). With no
  matching auth and no default, SendGrid falls back to signing as sendgrid.net,
  so DMARC alignment fails -> blocks + reduced delivery.

  Fix (no DNS changes needed): set the valid record as the account DEFAULT, so
  root-domain sends use it. 'www.hustleforge.tech' and 'hustleforge.tech' share
  the org domain, so relaxed DMARC alignment then passes. Also delete the dead
  valid=False duplicates.

  IDEAL long-term (needs DNS, NOT done here): authenticate the root
  'hustleforge.tech' directly (SendGrid -> Authenticate a Domain -> add the
  generated CNAMEs at your DNS provider -> validate).

  SAFETY: DRY-RUN by default (prints the plan, changes nothing). Pass -Apply to
  execute. Never deletes a valid record; only sets a valid one as default.

.PARAMETER Apply
  Execute the changes. Omit for a dry-run.
#>
[CmdletBinding()]
param([switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1 -Force
$key = Get-HfSecret -Scope Samus -Name SendgridApiKey
if (-not $key) { Write-Error 'SendgridApiKey not in Samus DPAPI store.'; exit 2 }
$h = @{ Authorization = "Bearer $key" }
$base = 'https://api.sendgrid.com/v3/whitelabel/domains'

$domains = Invoke-RestMethod -Uri $base -Headers $h -TimeoutSec 20
$valid   = @($domains | Where-Object { $_.valid })
$invalid = @($domains | Where-Object { -not $_.valid })

Write-Host ("Domain-auth records: {0} total, {1} valid, {2} invalid" -f @($domains).Count, $valid.Count, $invalid.Count)
foreach ($d in $domains) {
    Write-Host ("  id={0} {1}.{2} valid={3} default={4}" -f $d.id, $d.subdomain, $d.domain, $d.valid, $d.default)
}

if ($valid.Count -eq 0) {
    Write-Warning "No VALID domain-auth record - cannot set a default. Authenticate a domain first (needs DNS)."
    exit 1
}
$target = $valid[0]                       # the record to make default
$needDefault = -not $target.default
Write-Host ""
Write-Host ("Plan: set id={0} ({1}) as DEFAULT ({2}); delete {3} invalid record(s)." -f `
    $target.id, $target.subdomain, $(if ($needDefault) { 'needed' } else { 'already default' }), $invalid.Count)

if (-not $Apply) {
    Write-Host "DRY-RUN: nothing changed. Re-run with -Apply to execute." -ForegroundColor Cyan
    exit 0
}

if ($needDefault) {
    $r = Invoke-RestMethod -Method Patch -Uri "$base/$($target.id)" -Headers $h `
        -Body (ConvertTo-Json @{ default = $true }) -ContentType 'application/json' -TimeoutSec 20
    Write-Host ("[apply] id={0} default={1}" -f $target.id, $r.default) -ForegroundColor Green
}
foreach ($d in $invalid) {
    Invoke-RestMethod -Method Delete -Uri "$base/$($d.id)" -Headers $h -TimeoutSec 20 | Out-Null
    Write-Host ("[apply] deleted invalid id={0} ({1})" -f $d.id, $d.subdomain) -ForegroundColor Green
}

$after = Invoke-RestMethod -Uri $base -Headers $h -TimeoutSec 20
Write-Host ""
Write-Host ("Final: {0} record(s)." -f @($after).Count)
foreach ($d in $after) { Write-Host ("  id={0} {1} valid={2} default={3}" -f $d.id, $d.subdomain, $d.valid, $d.default) }
Write-Host "Next: send a small batch and re-run the email audit; block_rate + delivery should improve." -ForegroundColor Cyan
