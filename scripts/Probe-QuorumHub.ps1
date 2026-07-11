<#
.SYNOPSIS
  Probe the cross-agent Quorum Hub for Samus.

.DESCRIPTION
  One-shot operator probe that calls the hub's three MCP tools
  (governance_stats, governance_log, governance_publish-test) over
  JSON-RPC 2.0 at the configured URL.

  Run this AFTER Start-SamusStack.ps1 to confirm:
    1. Hub is reachable from the operator host (same network as containers
       via host.docker.internal).
    2. HMAC key in DPAPI matches what the hub expects.
    3. Publish round-trip works.

  Does NOT seed any state, does NOT publish unless -Live is passed.

.PARAMETER Live
  Send a real governance_publish event (caller=samus, action=probe).
  Default: dry-run -- only stats + log are called.

.PARAMETER HubUrl
  Override the hub URL. Defaults to SAMUS_QUORUM_HUB_URL env or
  http://127.0.0.1:8090.

.EXAMPLE
  pwsh D:\Hustleforge\.worktrees\samus\Samus\scripts\Probe-QuorumHub.ps1

.EXAMPLE
  pwsh D:\Hustleforge\.worktrees\samus\Samus\scripts\Probe-QuorumHub.ps1 -Live
#>
[CmdletBinding()]
param(
    [switch]$Live,
    [string]$HubUrl = $env:SAMUS_QUORUM_HUB_URL
)

if (-not $HubUrl) { $HubUrl = 'http://127.0.0.1:8090' }
$HubUrl = $HubUrl.TrimEnd('/')

function Invoke-HubRpc {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][hashtable]$Params
    )
    $body = @{ jsonrpc = '2.0'; id = 1; method = $Method; params = $Params } | ConvertTo-Json -Depth 6 -Compress
    $headers = @{ 'Content-Type' = 'application/json' }

    $keyHex = $env:SAMUS_QUORUM_HUB_HMAC_KEY
    if ($keyHex) {
        try {
            $bytes = [byte[]]::new($keyHex.Length / 2)
            for ($i = 0; $i -lt $keyHex.Length; $i += 2) {
                $bytes[$i/2] = [Convert]::ToByte($keyHex.Substring($i, 2), 16)
            }
            $hmac = New-Object System.Security.Cryptography.HMACSHA256
            $hmac.Key = $bytes
            $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
            $sigBytes = $hmac.ComputeHash($bodyBytes)
            $sig = [BitConverter]::ToString($sigBytes) -replace '-',''
            $headers['X-Hub-HMAC'] = $sig.ToLower()
        } catch {
            Write-Warning "Failed to compute HMAC; sending unsigned: $($_.Exception.Message)"
        }
    }

    try {
        $resp = Invoke-RestMethod -Uri "$HubUrl/mcp" -Method Post -Body $body `
                                  -Headers $headers -TimeoutSec 5 -ErrorAction Stop
        return @{ ok = $true; payload = $resp }
    } catch {
        return @{ ok = $false; error = $_.Exception.Message }
    }
}

Write-Host "Quorum Hub probe -> $HubUrl/mcp" -ForegroundColor Cyan
if (-not $env:SAMUS_QUORUM_HUB_HMAC_KEY) {
    Write-Host "  HMAC key: NOT SET (dev mode; hub may reject in production)" -ForegroundColor DarkGray
} else {
    Write-Host "  HMAC key: present ($($env:SAMUS_QUORUM_HUB_HMAC_KEY.Length) hex chars)" -ForegroundColor DarkGray
}
Write-Host ''

# 1) stats
Write-Host '[1/3] governance_stats ...' -ForegroundColor Yellow
$stats = Invoke-HubRpc -Method 'tools/call' -Params @{
    name = 'governance_stats'
    arguments = @{}
}
if ($stats.ok) {
    Write-Host "      OK: $($stats.payload.result.content[0].text)" -ForegroundColor Green
} else {
    Write-Host "      FAIL: $($stats.error)" -ForegroundColor Red
    exit 1
}

# 2) log
Write-Host '[2/3] governance_log (limit=5) ...' -ForegroundColor Yellow
$log = Invoke-HubRpc -Method 'tools/call' -Params @{
    name = 'governance_log'
    arguments = @{ limit = 5 }
}
if ($log.ok) {
    Write-Host '      OK' -ForegroundColor Green
    $log.payload.result.content[0].text | Write-Host -ForegroundColor DarkGray
} else {
    Write-Host "      FAIL: $($log.error)" -ForegroundColor Red
    exit 1
}

# 3) publish (live only)
if ($Live) {
    Write-Host '[3/3] governance_publish (LIVE) ...' -ForegroundColor Yellow
    $pub = Invoke-HubRpc -Method 'tools/call' -Params @{
        name = 'governance_publish'
        arguments = @{
            caller = 'samus'
            action = 'probe'
            risk_score = 0.0
            approved = $true
            approval_score = 1.0
            threshold = 0.5
            votes = @(@{ voter = 'probe_script'; vote = 'APPROVE'; weight = 1.0 })
            reason = "Probe-QuorumHub.ps1 sanity-check at $((Get-Date).ToString('o'))"
        }
    }
    if ($pub.ok) {
        Write-Host "      OK: $($pub.payload.result.content[0].text)" -ForegroundColor Green
    } else {
        Write-Host "      FAIL: $($pub.error)" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host '[3/3] governance_publish skipped (use -Live to send a real event)' -ForegroundColor DarkGray
}

Write-Host ''
Write-Host 'Quorum Hub probe complete.' -ForegroundColor Cyan
