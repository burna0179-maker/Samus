<#
.SYNOPSIS
  Real-time dial-run monitor + auditor (Framework Agent call audit feed).

.DESCRIPTION
  Polls the Vapi REST API (the ground truth — the host dialer does NOT reliably
  write voice_audit.jsonl, so file-watching misses runs) for today's calls and
  streams status transitions, duration, ended-reason, per-call cost, and which
  caller-ID number placed each call. Writes a durable timestamped audit log so
  the record survives a session/host restart. Emits a final per-call summary
  with Vapi's own call analysis when the batch goes quiet.

  READ-ONLY: only GET /call. Never places, modifies, or ends a call.

  Cost note: Vapi does not bill for API reads; only the calls themselves cost.
  While idle-waiting it is a cheap loop; polling only tightens once calls start.

.PARAMETER PollSeconds     Seconds between Vapi polls while calls are active. Default 20.
.PARAMETER MaxMinutes      Hard stop after this many minutes. Default 60.
.PARAMETER QuietRounds     Consecutive quiet polls (no new events, none active) to declare done. Default 9 (~3 min).
#>
[CmdletBinding()]
param(
    [int]$PollSeconds = 20,
    [int]$MaxMinutes  = 60,
    [int]$QuietRounds = 9
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1 -Force
$vapiKey = Get-HfSecret -Scope Samus -Name VapiApiKey
if (-not $vapiKey) { throw 'VapiApiKey not in Samus DPAPI store.' }

$auditDir = 'D:\Hustleforge\Samus\.data\host_artifacts\voice\realtime_audit'
if (-not (Test-Path $auditDir)) { New-Item -ItemType Directory -Path $auditDir -Force | Out-Null }
$log = Join-Path $auditDir ("dial_audit_{0}.log" -f (Get-Date).ToString('yyyy-MM-dd_HHmmss'))
function W([string]$m) { $l = "[{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $m; $l | Tee-Object -FilePath $log -Append }

function Get-TodayCalls {
    try {
        $r = Invoke-RestMethod -Uri 'https://api.vapi.ai/call?limit=30' -Headers @{ Authorization = "Bearer $vapiKey" } -TimeoutSec 20
        $today = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd')
        return @($r | Where-Object { $_.createdAt -and $_.createdAt.ToString().Substring(0,10) -eq $today })
    } catch { W "vapi poll error: $($_.Exception.Message)"; return @() }
}
function Dur($c) { if ($c.startedAt -and $c.endedAt) { try { return [math]::Round(([datetime]$c.endedAt - [datetime]$c.startedAt).TotalSeconds,0) } catch {} } return '' }

W "monitor armed (Vapi API). poll=${PollSeconds}s hardstop=${MaxMinutes}m log=$log"
$seen = @{}   # id8 -> status
$hardStop = (Get-Date).AddMinutes($MaxMinutes)
$idle = 0
while ((Get-Date) -lt $hardStop) {
    $calls = Get-TodayCalls
    $newEvents = 0
    foreach ($c in ($calls | Sort-Object createdAt)) {
        $id = $c.id.ToString().Substring(0,8)
        $st = [string]$c.status
        if ($seen[$id] -ne $st) {
            $seen[$id] = $st; $newEvents++
            $d = Dur $c; $reason = if ($c.PSObject.Properties['endedReason']) { [string]$c.endedReason } else { '' }
            $cost = if ($c.PSObject.Properties['cost'] -and $c.cost) { '$' + [string]$c.cost } else { '' }
            $num = if ($c.PSObject.Properties['phoneNumberId'] -and $c.phoneNumberId) { $c.phoneNumberId.ToString().Substring(0,8) } else { '?' }
            W ("CALL {0} {1}{2}{3}{4} num={5}" -f $id, $st.ToUpper(), $(if($reason){" $reason"}), $(if($d -ne ''){" ${d}s"}), $(if($cost){" $cost"}), $num)
        }
    }
    $active = @($seen.Values | Where-Object { $_ -ne 'ended' }).Count
    if ($newEvents -eq 0 -and $seen.Count -gt 0 -and $active -eq 0) { $idle++ } else { $idle = 0 }
    if ($idle -ge $QuietRounds) { W "batch complete: $($seen.Count) calls, all ended, quiet"; break }
    Start-Sleep -Seconds $PollSeconds
}

# --- final per-call summary (with Vapi analysis) ---
W "=== FINAL AUDIT: $($seen.Count) calls today ==="
$calls = Get-TodayCalls
$agg = @{ voicemail=0; live_hangup=0; engaged=0; no_answer=0; other=0; cost=0.0 }
foreach ($c in ($calls | Sort-Object createdAt)) {
    $d = Dur $c; $reason = [string]$c.endedReason
    if ($c.cost) { $agg.cost += [double]$c.cost }
    switch -Regex ($reason) {
        'voicemail'            { $agg.voicemail++ }
        'customer-ended'       { $agg.live_hangup++ }
        'assistant-ended|silence' { $agg.engaged++ }
        'did-not-answer|no-answer' { $agg.no_answer++ }
        default                { $agg.other++ }
    }
    $sum = ''
    if ($c.PSObject.Properties['analysis'] -and $c.analysis -and $c.analysis.PSObject.Properties['summary']) { $sum = (([string]$c.analysis.summary) -replace "`n",' ') }
    $snip = if ($sum) { $sum.Substring(0,[Math]::Min(160,$sum.Length)) } else { '' }
    W ("SUM {0} {1} {2}s `${3} | {4}" -f $c.id.ToString().Substring(0,8), $reason, $d, $c.cost, $snip)
}
W ("TOTALS: voicemail={0} live-hangup={1} engaged={2} no-answer={3} other={4} | cost=`${5:N2}" -f $agg.voicemail,$agg.live_hangup,$agg.engaged,$agg.no_answer,$agg.other,$agg.cost)
W "audit log: $log"
Remove-Variable vapiKey
exit 0
