<#
.SYNOPSIS
  Pin Morgan's Vapi server.url + server.secret to the stable ingress domain.

.DESCRIPTION
  Since 2026-07-03 the Samus stack fronts ONE ngrok tunnel on a stable dev
  domain -> samus-ingress (Caddy) -> path-routed to voice/finance. Morgan's
  webhook URL therefore no longer rotates, so this script no longer hunts for
  a live "voice" tunnel (that model is gone). It now simply PINS the assistant's
  server config to the stable domain:

    server.url    = https://<Domain>/vapi/webhook
    server.secret = <VapiWebhookSecret>          (== container VAPI_WEBHOOK_SECRET)

  WHY BOTH FIELDS, ALWAYS: a Vapi `PATCH {server:{url}}` REPLACES the whole
  `server` object, silently dropping any existing `secret`. On 2026-07-03 the
  ingress migration re-pointed server.url without the secret, so Vapi stopped
  sending the x-vapi-secret header and the voice handler 403'd EVERY
  end-of-call-report (silent capture outage). Setting url+secret together every
  time makes that failure structurally impossible. Vapi never echoes the secret
  back in GET/PATCH responses (write-only), so this is idempotent-by-design and
  cannot be "verified" by reading it back — instead we POST a signed probe to
  the public edge and assert it is accepted (not 403).

  RUN CONTEXTS
    * From Start-SamusStack.ps1 (the startup guard): the VAPI_* env vars are
      already hydrated from DPAPI, so this reads them from the environment.
    * Standalone / disaster recovery: no env vars present, so it falls back to
      the DPAPI store (Scope=Samus: VapiApiKey, VapiAssistantId,
      VapiWebhookSecret).

  WDAC/CLM SAFETY: uses curl.exe (native, ships with Windows 10+) instead of
  Invoke-RestMethod. PS 5.1 defaults to TLS 1.0 (Vapi/ngrok require 1.2+) and
  the TLS override is a static property setter that ConstrainedLanguage mode
  (WDAC) blocks outright. curl.exe negotiates TLS itself and needs no static
  calls, so this stays green under the WDAC baseline.

.PARAMETER Domain
  The stable ngrok ingress domain (from docker/compose/ngrok.yml). The webhook
  URL is https://<Domain>/vapi/webhook.

.PARAMETER AssistantId
  Override the assistant id. Default: env VAPI_ASSISTANT_ID, else DPAPI
  Samus/VapiAssistantId.

.EXAMPLE
  .\Sync-VoiceWebhook.ps1
#>

[CmdletBinding()]
param(
    [Parameter()][string]$Domain = 'millard-unruffable-reginia.ngrok-free.dev',
    [Parameter()][string]$AssistantId = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- 1. Resolve creds: environment first, DPAPI fallback --------------------
# When invoked from Start-SamusStack the VAPI_* vars are already exported; a
# standalone DR run has none, so we hydrate from the shared DPAPI store.
# CLM-safe env read (this host runs WDAC ConstrainedLanguage; [Environment]::
# static calls are blocked). Get-Item on the Env: drive + .Value property access
# is allowed.
function Resolve-Cred([string]$EnvVar, [string]$DpapiName) {
    $item = Get-Item -LiteralPath "Env:\$EnvVar" -ErrorAction SilentlyContinue
    if ($item -and $item.Value) { return $item.Value }
    return (Get-HfSecret -Scope Samus -Name $DpapiName)
}

$needDpapi = -not ($env:VAPI_API_KEY -and $env:VAPI_WEBHOOK_SECRET -and
                   ($AssistantId -or $env:VAPI_ASSISTANT_ID))
if ($needDpapi) {
    $sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
    if (-not (Test-Path $sharedModule)) { throw "secrets module not found at $sharedModule" }
    Import-Module $sharedModule -Force
}

$key    = Resolve-Cred 'VAPI_API_KEY'        'VapiApiKey'
$asst   = if ($AssistantId) { $AssistantId } else { Resolve-Cred 'VAPI_ASSISTANT_ID' 'VapiAssistantId' }
$secret = Resolve-Cred 'VAPI_WEBHOOK_SECRET' 'VapiWebhookSecret'

if (-not $key)    { throw "VAPI_API_KEY unavailable (env + DPAPI Samus/VapiApiKey both empty)" }
if (-not $asst)   { throw "VAPI_ASSISTANT_ID unavailable (env + DPAPI Samus/VapiAssistantId both empty)" }
if (-not $secret) { throw "VAPI_WEBHOOK_SECRET unavailable (env + DPAPI Samus/VapiWebhookSecret both empty)" }

$webhookUrl = "https://$Domain/vapi/webhook"
Write-Host "pinning Morgan ($asst) server.url -> $webhookUrl (+ secret)" -ForegroundColor Cyan

# --- 2. PATCH server.url + server.secret via curl.exe -----------------------
# Body goes to a temp file so the secret never appears on the command line /
# in the process table. Removed in the finally block.
$bodyFile = Join-Path $env:TEMP ("vapi_pin_{0}.json" -f (Get-Random))
try {
    @{ server = @{ url = $webhookUrl; secret = $secret } } |
        ConvertTo-Json -Compress | Out-File -FilePath $bodyFile -Encoding ascii -NoNewline

    $resp = & curl.exe -s -m 20 -X PATCH "https://api.vapi.ai/assistant/$asst" `
        -H "Authorization: Bearer $key" `
        -H "Content-Type: application/json" `
        --data "@$bodyFile" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "curl PATCH failed (exit $LASTEXITCODE)" }

    # Confirm the PATCH was accepted (server.url echoes back; secret never does).
    # -like (not -match) so the URL's regex-special chars aren't interpreted, and
    # so we avoid [Regex]::Escape (blocked under ConstrainedLanguage). The URL
    # contains no PowerShell wildcard chars (*?[), so -like matches it literally.
    if ("$resp" -notlike "*$webhookUrl*") {
        Write-Warning "PATCH response did not echo the pinned URL. Raw: $($resp -replace '(?s)^(.{200}).+$','$1...')"
    } else {
        Write-Host "PATCH accepted (server.url pinned)." -ForegroundColor Green
    }
} finally {
    Remove-Item -Path $bodyFile -ErrorAction SilentlyContinue
}

# --- 3. Self-check: POST a signed probe to the public edge ------------------
# Vapi won't echo the secret, so the only true verification is that the live
# webhook path ACCEPTS a request carrying x-vapi-secret. A 403 here means the
# secret on the assistant and the secret the voice handler validates against
# have diverged (or the edge is down). Best-effort — never fails the pin.
$probeBody = '{"message":{"type":"status-update","call":{"id":"sync-probe"}}}'
$edgeCode = & curl.exe -s -o NUL -w '%{http_code}' -m 15 -X POST $webhookUrl `
    -H "Content-Type: application/json" -H "x-vapi-secret: $secret" `
    --data $probeBody 2>$null
if ($edgeCode -eq '200') {
    Write-Host "edge self-check: 200 — Vapi end-of-call reports will be accepted." -ForegroundColor Green
} elseif ($edgeCode -eq '403') {
    Write-Warning "edge self-check: 403 — the assistant secret and voice VAPI_WEBHOOK_SECRET disagree, OR the edge rejected auth. Webhooks WILL drop."
} else {
    Write-Warning "edge self-check: HTTP $edgeCode (expected 200). Check: docker logs samus-ngrok / samus-ingress / samus-voice"
}

exit 0
