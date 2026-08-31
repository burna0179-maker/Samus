<#
.SYNOPSIS
  Log hand-dialed call outcomes from today's morning call list into the CRM.

.DESCRIPTION
  Companion to the morning brief. After working a prospect on the call list
  (morning_call_list_<date>.txt), run this to record the outcome + your notes.
  It writes a Conversation + refreshes the prospect's CallState through the
  canonical CRM service layer, so manual calls land in the same tables the
  automated Vapi calls do, and show up in tomorrow's brief.

  Interactive: pick a prospect by its call-list number, choose an outcome,
  type your notes. Numbers match the morning brief / call-list .txt (sorted
  hot, then warm, then low). Repeats until you quit.

  Every logged call is also appended to an operator journal,
  call_outcomes_<date>.jsonl, next to the call list, so a call is never lost
  even if the CRM write is briefly degraded.

  AWS credentials are pulled from the DPAPI store (Scope = Samus) for the
  child Python process and scrubbed from the environment on exit.

.EXAMPLE
  .\Log-Call.ps1
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Run from the Samus repo root with .venv installed."
}

# --- AWS credentials from DPAPI (CRM writes to DynamoDB) --------------------
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "secrets module not found at $sharedModule"
}
Import-Module $sharedModule -Force
foreach ($pair in @(
        @{ Secret = 'AwsAccessKeyId';     Env = 'AWS_ACCESS_KEY_ID' },
        @{ Secret = 'AwsSecretAccessKey'; Env = 'AWS_SECRET_ACCESS_KEY' })) {
    if (-not (Test-HfSecret -Scope Samus -Name $pair.Secret)) {
        throw "DPAPI secret '$($pair.Secret)' (Scope=Samus) missing - CRM write needs AWS creds."
    }
    Set-Item -Path "Env:$($pair.Env)" -Value (Get-HfSecret -Scope Samus -Name $pair.Secret)
}
$env:AWS_REGION         = 'us-west-1'
$env:AWS_DEFAULT_REGION = 'us-west-1'

# Self-locating under the Samus runtime root ($samusRoot = this script's repo),
# in lockstep with Run-ProspectingDaily.ps1 (the call-list producer) and
# Send-Morning.ps1. Previously hardcoded to a standalone
# D:\Hustleforge\Samus\.data\host_artifacts that the worktree consolidation
# deleted, so this consumer read an absent tree and always reported "No call
# list for today" even though the producer had written one. Override via
# $env:SAMUS_ARTIFACT_ROOT.
$artifactRoot = if ($env:SAMUS_ARTIFACT_ROOT) { $env:SAMUS_ARTIFACT_ROOT } else { Join-Path $samusRoot '.data\host_artifacts' }
$env:SAMUS_ARTIFACT_ROOT = $artifactRoot
$env:PYTHONIOENCODING    = 'utf-8'
$prevEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$today   = (Get-Date).ToString('yyyy-MM-dd')
$csvPath = Join-Path $artifactRoot "daily_calls\call_list_$today.csv"

# Outcome menu - keys map 1:1 to backend.crm.log_call VALID_OUTCOMES.
# 1-6 keep their historical numbers (operator muscle memory); 7-9 were
# appended 2026-05-21 from real call data - see _OUTCOME_TO_STATE in log_call.py.
$outcomes = [ordered]@{
    '1' = @{ code = 'booked';         label = 'booked         - meeting / audit / deal booked' }
    '2' = @{ code = 'follow_up';      label = 'follow_up      - reached the decision-maker, interested, call back' }
    '3' = @{ code = 'disqualified';   label = 'disqualified   - not a fit (structural - wrong vertical / size / area)' }
    '4' = @{ code = 'no_answer';      label = 'no_answer      - no pickup (rang out)' }
    '5' = @{ code = 'voicemail';      label = 'voicemail      - left a voicemail' }
    '6' = @{ code = 'do_not_call';    label = 'do_not_call    - asked not to be called' }
    '7' = @{ code = 'gatekeeper';     label = 'gatekeeper     - reached a gatekeeper, not the decision-maker (retry)' }
    '8' = @{ code = 'not_interested'; label = 'not_interested - heard the pitch, declined (soft no, not a hard DQ)' }
    '9' = @{ code = 'hung_up';        label = 'hung_up        - answered then hung up / brush-off, no engagement' }
}

try {
    if (-not (Test-Path $csvPath)) {
        Write-Host "No call list for today ($today)." -ForegroundColor Yellow
        Write-Host "Expected: $csvPath"
        Write-Host "Run .\Run-ProspectingDaily.ps1 first, or wait for the 07:30 task."
        exit 1
    }

    $rank      = @{ hot = 0; warm = 1; low = 2 }
    # Security-grade tie-breaker — a worse grade ranks a prospect higher.
    # Guarded with a property-exists check so a pre-this-feature call_list CSV
    # (no security_grade column) still sorts cleanly under Set-StrictMode.
    $gradeRank = @{ F = 0; D = 1; C = 2; B = 3; A = 4 }
    $rows   = @(Import-Csv -Path $csvPath | Sort-Object `
        @{ Expression = { $pr = "$($_.call_priority)".ToLower(); if ($rank.ContainsKey($pr)) { $rank[$pr] } else { 9 } } }, `
        @{ Expression = { if ($_.lead_score) { [int]$_.lead_score } else { 0 } }; Descending = $true }, `
        @{ Expression = {
            $g = ''
            if ($_.PSObject.Properties.Name -contains 'security_grade') { $g = "$($_.security_grade)".ToUpper() }
            if ($gradeRank.ContainsKey($g)) { $gradeRank[$g] } else { 9 }
        } }, `
        @{ Expression = { if ($_.seo_score)  { [int]$_.seo_score }  else { 0 } } })

    if ($rows.Count -eq 0) {
        Write-Host "Today's call list is empty (0 prospects)." -ForegroundColor Yellow
        exit 1
    }

    # Strategy-bandit attribution (Unit 3): safely read a column off a CSV row.
    # A pre-this-feature call_list CSV lacks the industry / policy_family /
    # seo_score / owner_email / social_* columns; under Set-StrictMode a bare
    # $row.missing_column throws, so every new column read is guarded with the
    # same PSObject.Properties.Name check the security_grade sort above uses.
    function Get-RowValue {
        param($Row, [string]$Column)
        if ($Row.PSObject.Properties.Name -contains $Column) {
            return "$($Row.$Column)"
        }
        return ''
    }

    function Show-List {
        Write-Host ""
        Write-Host ("  Call list - $today  ($($rows.Count) prospects)") -ForegroundColor Cyan
        for ($i = 0; $i -lt $rows.Count; $i++) {
            $r = $rows[$i]
            $name = "$($r.company_name)"
            if ($name.Length -gt 32) { $name = $name.Substring(0, 32) }
            Write-Host ("  {0,3}. {1,-32} {2,-16} {3} - {4}" -f `
                ($i + 1), $name, $r.phone, $r.city, $r.industry)
        }
        Write-Host ""
    }

    Write-Host "Samus call logger - $today" -ForegroundColor Green
    Write-Host "Numbers match your morning brief / call-list .txt."
    Show-List

    $logged = 0
    while ($true) {
        $sel = (Read-Host "Prospect # to log  (L = list, Q = quit)").Trim()
        if ($sel -match '^[Qq]') { break }
        if ($sel -eq '' -or $sel -match '^[Ll]') { Show-List; continue }
        if ($sel -notmatch '^\d+$') {
            Write-Host "  enter a number, L, or Q" -ForegroundColor Yellow
            continue
        }

        $idx = [int]$sel
        if ($idx -lt 1 -or $idx -gt $rows.Count) {
            Write-Host "  out of range (1-$($rows.Count))" -ForegroundColor Yellow
            continue
        }
        $p = $rows[$idx - 1]
        Write-Host ""
        Write-Host ("  #{0}  {1}" -f $idx, $p.company_name) -ForegroundColor Cyan
        Write-Host ("       {0}   {1} - {2}" -f $p.phone, $p.city, $p.industry)

        Write-Host "  Outcome:"
        foreach ($k in $outcomes.Keys) {
            Write-Host ("    {0}  {1}" -f $k, $outcomes[$k].label)
        }
        $oc = (Read-Host "  outcome #").Trim()
        if (-not $outcomes.Contains($oc)) {
            Write-Host "  not logged - invalid outcome" -ForegroundColor Yellow
            continue
        }
        $outcomeCode = $outcomes[$oc].code

        # Voicemail sub-prompt: many small-business owners' VM greetings invite
        # a text instead of a callback (e.g. "send me a text"). Capture it as a
        # stable "[prefers_text]" tag prepended to the notes - that keeps the
        # SMS-follow-up set greppable later instead of buried in ad-hoc notes.
        $prefersText = $false
        if ($outcomeCode -eq 'voicemail') {
            $pt = (Read-Host "  voicemail invited a text? (y/n)").Trim()
            if ($pt -match '^[Yy]') {
                $prefersText = $true
                Write-Host "  flagged: prefers text" -ForegroundColor Cyan
            }
        }

        # Gatekeeper sub-prompt: a gatekeeper often deflects with "just email
        # <address>" instead of putting the call through - and that address is
        # sometimes a brush-off (a real 2026-05-21 call was handed the
        # malformed "info@juniper-.com"). Any offered contact is checked on the
        # spot by backend.prospecting.contact_validation so a malformed or
        # off-domain address is caught before the operator wastes an email.
        $contactTag = ''
        if ($outcomeCode -eq 'gatekeeper') {
            $offered = (Read-Host "  contact offered on the call (email - blank to skip)").Trim()
            if ($offered) {
                $cvArgs = @('-m', 'backend.prospecting.contact_validation',
                            '--email', $offered, '--company', "$($p.company_name)")
                $siteCol = Get-RowValue $p 'website_url'
                if ($siteCol) { $cvArgs += @('--website', $siteCol) }
                $onFile = Get-RowValue $p 'owner_email'
                if ($onFile -and $onFile.Contains('@')) {
                    $cvArgs += @('--known-domain', $onFile.Split('@')[-1])
                }
                Push-Location $samusRoot
                try { $cvOut = & $venvPython @cvArgs } finally { Pop-Location }
                $cv = $null
                try { $cv = $cvOut | ConvertFrom-Json } catch { $cv = $null }
                if ($cv) {
                    $color = switch ($cv.verdict) {
                        'valid'     { 'Green' }
                        'malformed' { 'Red' }
                        'suspect'   { 'Yellow' }
                        default     { 'Gray' }
                    }
                    Write-Host ("  contact check: {0} -> {1}" -f `
                        $offered, ([string]$cv.verdict).ToUpper()) -ForegroundColor $color
                    foreach ($r in $cv.reasons) {
                        Write-Host "    - $r" -ForegroundColor $color
                    }
                    $contactTag = "[contact_offered: $offered ($($cv.verdict))]"
                } else {
                    Write-Host "  contact check unavailable - logging the address unverified" -ForegroundColor Yellow
                    $contactTag = "[contact_offered: $offered (unchecked)]"
                }
            }
        }

        $notes = (Read-Host "  notes (what was said / next step)").Trim()
        if ($prefersText) {
            if ($notes) { $notes = "[prefers_text] $notes" }
            else        { $notes = '[prefers_text]' }
        }
        if ($contactTag) {
            if ($notes) { $notes = "$contactTag $notes" }
            else        { $notes = $contactTag }
        }

        Write-Host ("  -> log #{0} {1} as '{2}'?" -f $idx, $p.company_name, $outcomeCode)
        if ((Read-Host "  confirm (y/n)").Trim() -notmatch '^[Yy]') {
            Write-Host "  skipped" -ForegroundColor Yellow
            continue
        }

        # Strategy-bandit attribution snapshot (Unit 3): read the arm +
        # reward-signal columns off the call-list row, guarded for a
        # pre-this-feature CSV. industry is part of the long-standing call-list
        # schema; the rest are new and must be column-checked.
        $industry      = Get-RowValue $p 'industry'
        $policyFamily  = Get-RowValue $p 'policy_family'
        $seoScoreCol   = Get-RowValue $p 'seo_score'
        $ownerEmailCol = Get-RowValue $p 'owner_email'
        $facebookCol   = Get-RowValue $p 'social_facebook'
        $instagramCol  = Get-RowValue $p 'social_instagram'
        # Per-prospect LLM cost (Unit 4) — new call-list column, column-checked.
        $tokenCostCol  = Get-RowValue $p 'llm_cost_usd'

        $banditArgs = @()
        if ($industry)     { $banditArgs += @('--industry', $industry) }
        if ($policyFamily) { $banditArgs += @('--policy-family', $policyFamily) }
        if ($seoScoreCol)  {
            $seoInt = 0
            if ([int]::TryParse($seoScoreCol, [ref]$seoInt)) {
                $banditArgs += @('--seo-score', "$seoInt")
            }
        }
        # The CSV stores the actual email / URL strings; a non-empty value
        # means enrichment found one, so it maps to the boolean --*-found flag.
        if ($ownerEmailCol) { $banditArgs += '--owner-email-found' }
        if ($facebookCol)   { $banditArgs += '--social-facebook-found' }
        if ($instagramCol)  { $banditArgs += '--social-instagram-found' }
        # llm_cost_usd is a float column; parse it culture-invariantly and only
        # forward a real positive cost (a missing column / 0 stays at default).
        if ($tokenCostCol)  {
            $tokenCost = 0.0
            if ([double]::TryParse($tokenCostCol,
                    [System.Globalization.NumberStyles]::Float,
                    [System.Globalization.CultureInfo]::InvariantCulture,
                    [ref]$tokenCost) -and $tokenCost -gt 0.0) {
                $banditArgs += @('--token-cost-usd', "$tokenCost")
            }
        }

        Push-Location $samusRoot
        try {
            & $venvPython -m backend.crm.log_call `
                --prospect-id $p.prospect_id `
                --company     $p.company_name `
                --phone       $p.phone `
                --outcome     $outcomeCode `
                --notes       $notes `
                @banditArgs
            $code = $LASTEXITCODE
        } finally {
            Pop-Location
        }
        if ($code -eq 0) {
            $logged++
            Write-Host ("  v logged  ({0} this session)" -f $logged) -ForegroundColor Green
        } else {
            Write-Host "  ! CRM write degraded - saved to the call_outcomes journal; re-ingest later" -ForegroundColor Yellow
        }
        Write-Host ""
    }
    Write-Host ("Done - $logged call(s) logged this session.") -ForegroundColor Green
}
finally {
    Remove-Item -Path Env:AWS_ACCESS_KEY_ID     -ErrorAction SilentlyContinue
    Remove-Item -Path Env:AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
    Remove-Item -Path Env:AWS_REGION            -ErrorAction SilentlyContinue
    Remove-Item -Path Env:AWS_DEFAULT_REGION    -ErrorAction SilentlyContinue
    Remove-Item -Path Env:PYTHONIOENCODING      -ErrorAction SilentlyContinue
    [Console]::OutputEncoding = $prevEnc
}
