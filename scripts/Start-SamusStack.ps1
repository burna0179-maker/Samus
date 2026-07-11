<#
.SYNOPSIS
  Bring up the Samus Docker Compose stack with secrets pulled from DPAPI.

.DESCRIPTION
  Sources every Samus secret from the cross-agent DPAPI store at
  ``D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1`` (Scope = Samus)
  and exports each as the env var that docker-compose expects. The plaintext
  values only live in this PowerShell process's environment for the lifetime
  of the child ``docker compose`` invocation; they are scrubbed in the
  ``finally`` block before the script returns.

  Mapping (DPAPI name -> env var consumed by compose):

    HivemindPassword     -> NEO4J_PASSWORD        (required for memory graph)
    SharedHmacKey        -> SAMUS_SHARED_HMAC_KEY (required for HMAC middleware)
    AwsAccessKeyId       -> AWS_ACCESS_KEY_ID     (required for SQS / DDB / SNS)
    AwsSecretAccessKey   -> AWS_SECRET_ACCESS_KEY (required for SQS / DDB / SNS)
    GooglePlacesApiKey   -> GOOGLE_PLACES_API_KEY (optional; prospecting)
    AnthropicApiKey      -> ANTHROPIC_API_KEY     (optional; callsheet + seo content)
    StripeApiKey         -> STRIPE_API_KEY        (optional; finance Stripe ingest)
    StripeWebhookSecret  -> STRIPE_WEBHOOK_SECRET (optional; finance webhook HMAC verify)
    SendgridApiKey       -> SENDGRID_API_KEY      (optional; finance payment-receipt email)
    SendgridFromEmail    -> SENDGRID_FROM_EMAIL   (optional; verified SendGrid sender)
    SendgridReplyTo      -> SENDGRID_REPLY_TO     (optional; Reply-To header, defaults to From)
    VapiApiKey           -> VAPI_API_KEY          (optional; voice outbound caller)
    VapiWebhookSecret    -> VAPI_WEBHOOK_SECRET   (optional; voice webhook HMAC verify)
    VapiAssistantId      -> VAPI_ASSISTANT_ID     (optional; required for morning dialer)
    VapiPhoneNumberId    -> VAPI_PHONE_NUMBER_ID  (optional; required for morning dialer)
    NgrokAuthToken       -> NGROK_AUTHTOKEN       (optional; voice opens embedded tunnel)
    VoiceConsoleToken    -> SAMUS_VOICE_CONSOLE_TOKEN (optional; operator browser console gate)
    GooglePagespeedApiKey -> GOOGLE_PAGESPEED_API_KEY (optional; seo real CWV via PSI)
    HmacKey_<service>    -> SAMUS_HMAC_KEY_<SERVICE> (optional; R-1 per-service HMAC,
                            21 workcells; bulk-seed via Seed-SamusHmacKeys.ps1)

  Missing optional secrets are tolerated -- workcells fall back gracefully.
  Missing required secrets abort before any container starts.

  Prereq: secrets populated via the shared module's interactive prompt:

    Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1
    Set-HfSecret -Scope Samus -Name HivemindPassword
    Set-HfSecret -Scope Samus -Name SharedHmacKey
    Set-HfSecret -Scope Samus -Name AwsAccessKeyId
    Set-HfSecret -Scope Samus -Name AwsSecretAccessKey
    Set-HfSecret -Scope Samus -Name GooglePlacesApiKey   # optional
    Set-HfSecret -Scope Samus -Name AnthropicApiKey      # optional
    Set-HfSecret -Scope Samus -Name StripeApiKey         # optional
    Set-HfSecret -Scope Samus -Name StripeWebhookSecret  # optional
    Set-HfSecret -Scope Samus -Name SendgridApiKey       # optional
    Set-HfSecret -Scope Samus -Name SendgridFromEmail    # optional
    Set-HfSecret -Scope Samus -Name SendgridReplyTo      # optional
    Set-HfSecret -Scope Samus -Name VapiApiKey           # optional
    Set-HfSecret -Scope Samus -Name VapiWebhookSecret    # optional
    Set-HfSecret -Scope Samus -Name VapiAssistantId      # optional (dialer)
    Set-HfSecret -Scope Samus -Name VapiPhoneNumberId    # optional (dialer)
    Set-HfSecret -Scope Samus -Name NgrokAuthToken       # optional (voice tunnel)
    Set-HfSecret -Scope Samus -Name VoiceConsoleToken    # optional (operator console)
    Set-HfSecret -Scope Samus -Name GooglePagespeedApiKey # optional (seo CWV)

.PARAMETER Detached
  Pass -d to docker compose (default true).

.PARAMETER Rebuild
  Pass --build to force image rebuild.

.EXAMPLE
  .\Start-SamusStack.ps1
  .\Start-SamusStack.ps1 -Rebuild
#>

[CmdletBinding()]
param(
    [Parameter()][switch]$Detached = $true,
    [Parameter()][switch]$Rebuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent (Resolve-Path $MyInvocation.MyCommand.Path)
$samusRoot = Resolve-Path (Join-Path $here '..')
$repoRoot  = Resolve-Path (Join-Path $samusRoot '..')
$composeFile = Join-Path $samusRoot 'docker\compose\docker-compose.samus.yml'
$envFile     = Join-Path $samusRoot 'docker\compose\.env'

if (-not (Test-Path $composeFile)) { throw "compose file not found: $composeFile" }
if (-not (Test-Path $envFile))     { throw "env file not found: $envFile (copy .env.example to start)" }

# Import the shared cross-agent secrets module. Falls back to the per-agent
# module if the shared one isn't installed yet.
$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
$perAgentModule = Join-Path $here 'Samus.Secrets.psm1'
if (Test-Path $sharedModule) {
    Import-Module $sharedModule -Force
    $useShared = $true
} elseif (Test-Path $perAgentModule) {
    Write-Warning "Shared module not found at $sharedModule -- falling back to per-agent module."
    Import-Module $perAgentModule -Force
    $useShared = $false
} else {
    throw "No secrets module found. Expected $sharedModule or $perAgentModule."
}

function readSecret([string]$Name) {
    if ($useShared) {
        return Get-HfSecret -Scope Samus -Name $Name
    } else {
        return Get-SamusSecret -Name $Name
    }
}

function hasSecret([string]$Name) {
    if ($useShared) {
        return Test-HfSecret -Scope Samus -Name $Name
    } else {
        return Test-SamusSecret -Name $Name
    }
}

# Required secrets -- abort if any are missing.
$required = @(
    @{ DpapiName='HivemindPassword';   EnvVar='NEO4J_PASSWORD' },
    @{ DpapiName='SharedHmacKey';      EnvVar='SAMUS_SHARED_HMAC_KEY' },
    @{ DpapiName='AwsAccessKeyId';     EnvVar='AWS_ACCESS_KEY_ID' },
    @{ DpapiName='AwsSecretAccessKey'; EnvVar='AWS_SECRET_ACCESS_KEY' },
    # Postgres password for samus-docuseal-db (compose requires it:
    # POSTGRES_PASSWORD: ${DOCUSEAL_DB_PASSWORD:?...}). One value covers both the
    # db (POSTGRES_PASSWORD) and the docuseal app's DB connection. FIRST bring-up
    # initializes postgres with this on the empty samus-docuseal-db-data volume --
    # pick once, keep stable (changing it later won't re-init an existing volume).
    # Seed with:  Set-HfSecret -Scope Samus -Name DocusealDbPassword
    @{ DpapiName='DocusealDbPassword'; EnvVar='DOCUSEAL_DB_PASSWORD' }
)

# Optional secrets -- warn if missing, continue without them.
$optional = @(
    @{ DpapiName='GooglePlacesApiKey';   EnvVar='GOOGLE_PLACES_API_KEY' },
    # NOTE: web-presence cross-referencing (backend.website.presence_check) uses
    # the Gemini grounded search (GeminiApiKey, seeded below) — NOT the Google
    # Custom Search JSON API, which is closed to new customers and 403s on this
    # project. See presence_check.web_search_finds_site.
    @{ DpapiName='AnthropicApiKey';      EnvVar='ANTHROPIC_API_KEY' },
    # Mercury READ-ONLY bank API token — the live real-cash source (runway +
    # affordability) once Stripe payouts land in Mercury. Read-only: cannot move
    # money. Consumed by samus-finance when SAMUS_MERCURY_ENABLED=1.
    @{ DpapiName='MercuryApiToken';      EnvVar='MERCURY_API_TOKEN' },
    @{ DpapiName='StripeApiKey';         EnvVar='STRIPE_API_KEY' },
    @{ DpapiName='StripeWebhookSecret';  EnvVar='STRIPE_WEBHOOK_SECRET' },
    # Gmail company inbox OAuth credentials for backend.intake.gmail_poller.
    # Consumed by samus-intake's in-container drain loop
    # (backend/intake/gmail_poll_task.py, wired 2026-07-07 in place of the
    # retired "Samus Inbox Poll" Windows scheduled task per operator
    # directive). All three optional -- absent means drain_once() returns
    # enabled=False as a clean no-op. Seed with:
    #   Set-HfSecret -Scope Samus -Name GmailInboxEmail
    #   Set-HfSecret -Scope Samus -Name GmailOauthClientId
    #   Set-HfSecret -Scope Samus -Name GmailOauthClientSecret
    # The OAuth REFRESH token itself is a JSON file on the samus-data
    # volume (/opt/samus/data/intake/gmail_oauth_token.json inside the
    # container); populate it via scripts/Authorize-Gmail.ps1 targeting
    # the on-disk path the volume is mounted from.
    @{ DpapiName='GmailInboxEmail';        EnvVar='SAMUS_GMAIL_INBOX_EMAIL' },
    @{ DpapiName='GmailOauthClientId';     EnvVar='SAMUS_GMAIL_OAUTH_CLIENT_ID' },
    @{ DpapiName='GmailOauthClientSecret'; EnvVar='SAMUS_GMAIL_OAUTH_CLIENT_SECRET' },
    @{ DpapiName='SendgridApiKey';       EnvVar='SENDGRID_API_KEY' },
    @{ DpapiName='SendgridFromEmail';    EnvVar='SENDGRID_FROM_EMAIL' },
    @{ DpapiName='SendgridReplyTo';      EnvVar='SENDGRID_REPLY_TO' },
    # ECDSA public key from SendGrid's "Signed Event Webhook" UI — verifies the
    # Heat Field webhook (/api/sendgrid/events). Seal with:
    #   Set-HfSecret -Scope Samus -Name SendgridWebhookVerificationKey
    # Absent -> the webhook route fails closed in prod (no heat signal). The
    # compose anchor already references ${SENDGRID_WEBHOOK_VERIFICATION_KEY:-}.
    @{ DpapiName='SendgridWebhookVerificationKey'; EnvVar='SENDGRID_WEBHOOK_VERIFICATION_KEY' },
    @{ DpapiName='GeminiApiKey';         EnvVar='GEMINI_API_KEY' },
    # OpenAI key — the BACKUP backend for backend.common.local_llm. Local
    # LM Studio is primary (free); OpenAI takes over when local is empty/down
    # so the offline reasoning stack never goes dark. Absent -> local-only.
    @{ DpapiName='OpenAiApiKey';         EnvVar='OPENAI_API_KEY' },
    @{ DpapiName='VapiApiKey';           EnvVar='VAPI_API_KEY' },
    @{ DpapiName='VapiWebhookSecret';    EnvVar='VAPI_WEBHOOK_SECRET' },
    @{ DpapiName='VapiAssistantId';      EnvVar='VAPI_ASSISTANT_ID' },
    @{ DpapiName='VapiPhoneNumberId';    EnvVar='VAPI_PHONE_NUMBER_ID' },
    @{ DpapiName='VapiInboundAssistantId';   EnvVar='VAPI_INBOUND_ASSISTANT_ID' },
    @{ DpapiName='VapiInboundPhoneNumberId'; EnvVar='VAPI_INBOUND_PHONE_NUMBER_ID' },
    # Own-Twilio credentials for the number-import path (provision.py
    # import-twilio). Optional/dormant -- absent means the import path is
    # fail-closed and the Vapi-managed voice surface is unaffected.
    @{ DpapiName='TwilioAccountSid';     EnvVar='TWILIO_ACCOUNT_SID' },
    @{ DpapiName='TwilioAuthToken';      EnvVar='TWILIO_AUTH_TOKEN' },
    @{ DpapiName='TwilioApiKey';         EnvVar='TWILIO_API_KEY' },
    @{ DpapiName='TwilioApiSecret';      EnvVar='TWILIO_API_SECRET' },
    @{ DpapiName='NgrokAuthToken';       EnvVar='NGROK_AUTHTOKEN' },
    # Cloudflare Tunnel connector token for samus-cloudflared, which routes
    # sign.hustleforge.tech -> samus-docuseal:3000 (branded contract-signing
    # links). Absent -> the tunnel container fails to authenticate and
    # DocuSeal is reachable on the internal network only. Seal with:
    #   Set-HfSecret -Scope Samus -Name CloudflareTunnelToken
    @{ DpapiName='CloudflareTunnelToken'; EnvVar='CLOUDFLARE_TUNNEL_TOKEN' },
    @{ DpapiName='VoiceConsoleToken';    EnvVar='SAMUS_VOICE_CONSOLE_TOKEN' },
    @{ DpapiName='GooglePagespeedApiKey'; EnvVar='GOOGLE_PAGESPEED_API_KEY' },
    # Cross-agent Quorum Hub HMAC (hex). Hub at host:8090. Optional --
    # absent key means the publisher runs in dev mode (no body signing).
    # Publishing only fires when SAMUS_QUORUM_PUBLISH_ENABLED=1, which the
    # operator sets explicitly outside DPAPI.
    @{ DpapiName='QuorumHubHmacKey';     EnvVar='SAMUS_QUORUM_HUB_HMAC_KEY' },
    # WordPress draft-page submission creds for validated product calls
    # (voice/wordpress_pages.submit_product_page). Optional/dormant.
    @{ DpapiName='WordPressUsername';    EnvVar='WORDPRESS_USERNAME' },
    @{ DpapiName='WordPressAppPassword'; EnvVar='WORDPRESS_APP_PASSWORD' },
    # Apollo.io people-search key for the last-resort enrichment stage
    # (backend/prospecting/apollo_adapter.py). Also feeds the standalone
    # backend/outreach/apollo_source.py cold-mail flow. Empty key = both
    # paths are no-ops. Daily $-cap enforced by the SHARED G11 store
    # (apollo_budget.py + SAMUS_APOLLO_DAILY_BUDGET_USD, default $5/day).
    @{ DpapiName='ApolloApiKey';         EnvVar='APOLLO_API_KEY' },
    # Anthropic Admin API key (not the regular Claude API key). Required for
    # the /v1/organizations/cost_report endpoint that powers live CODB spend
    # tracking. Without it, Anthropic spend is a static YAML estimate only.
    @{ DpapiName='AnthropicAdminKey';   EnvVar='ANTHROPIC_ADMIN_API_KEY' }
)

# Per-service HMAC keys (R-1 security remediation, 2026-05-20). Each maps
# DPAPI 'HmacKey_<service>' -> env 'SAMUS_HMAC_KEY_<SERVICE>'. Optional and
# additive: a service with no per-service key falls back to
# SAMUS_SHARED_HMAC_KEY inside the workcell, so the stack boots identically
# whether 0 or all 21 are seeded. Bulk-seed with
# D:\tools\hustleforge-git\Seed-SamusHmacKeys.ps1.
$hmacServices = @(
    'crm','entropy','feedback','finance','fulfillment','gateway','intake',
    'leadgen','memory','optimizer','outreach','path_optimizer',
    'portfolio_controller','proposal','prospecting','scaffold','seo',
    'signal_filter','strategy','template_recovery','voice'
)
$perServiceHmac = @($hmacServices | ForEach-Object {
    @{ DpapiName = "HmacKey_$_"; EnvVar = "SAMUS_HMAC_KEY_$($_.ToUpperInvariant())" }
})

$missing = @()
foreach ($s in $required) {
    if (-not (hasSecret $s.DpapiName)) { $missing += $s.DpapiName }
}
if ($missing.Count -gt 0) {
    Write-Warning "Missing required secrets in DPAPI: $($missing -join ', ')"
    Write-Warning "Populate with:  Set-HfSecret -Scope Samus -Name <SecretName>"
    throw "Aborting -- required secrets are not in the DPAPI store."
}

# Pull required + optional into process env. Track names for the finally scrub.
$exportedVars = @()
try {
    foreach ($s in $required) {
        Set-Item -Path "Env:$($s.EnvVar)" -Value (readSecret $s.DpapiName)
        $exportedVars += $s.EnvVar
    }
    foreach ($s in $optional) {
        if (hasSecret $s.DpapiName) {
            Set-Item -Path "Env:$($s.EnvVar)" -Value (readSecret $s.DpapiName)
            $exportedVars += $s.EnvVar
        } else {
            Write-Host "  (optional secret '$($s.DpapiName)' not set -- workcell will degrade gracefully)" -ForegroundColor DarkGray
        }
    }

    # Per-service HMAC keys (R-1). Export whichever are seeded in DPAPI;
    # unseeded services fall back to SAMUS_SHARED_HMAC_KEY in the workcell.
    $hmacSeeded = 0
    foreach ($s in $perServiceHmac) {
        if (hasSecret $s.DpapiName) {
            Set-Item -Path "Env:$($s.EnvVar)" -Value (readSecret $s.DpapiName)
            $exportedVars += $s.EnvVar
            $hmacSeeded++
        }
    }
    Write-Host "  per-service HMAC keys: $hmacSeeded of $($perServiceHmac.Count) seeded (rest use shared key)" -ForegroundColor DarkGray

    # Phase C-Samus per-agent RBAC (2026-05-17): Samus's containers
    # authenticate as the per-agent Neo4j user `neo4j_samus`, not admin
    # `neo4j`. Username is not a secret (just an identifier) so it lives
    # in plain code. The matching password is in DPAPI at
    # Samus/HivemindPassword (seeded during Phase B-2 with the per-agent
    # password generated by provision_rbac.ps1; see ADR-001).
    # Compose file defaults NEO4J_USER to "neo4j" — this overrides it.
    $env:NEO4J_USER = 'neo4j_samus'
    $exportedVars += 'NEO4J_USER'

    # Operator console bearer for /api/console/* (operator_console pack
    # fail-closes 503 without it in production). Single source of truth =
    # ForgeUi_SamusApiToken in the LocalMachine DPAPI store (the same secret
    # forge-ui reads to call Samus). Reading it here -> SAMUS_OPERATOR_TOKEN
    # keeps both sides in sync without a parallel Samus-scope DPAPI entry.
    try {
        $opTok = Get-HfMachineSecret -Name 'ForgeUi_SamusApiToken' -ErrorAction Stop
        if ($opTok) {
            $env:SAMUS_OPERATOR_TOKEN = $opTok
            $exportedVars += 'SAMUS_OPERATOR_TOKEN'
            Write-Host "  SAMUS_OPERATOR_TOKEN: loaded from LocalMachine DPAPI (ForgeUi_SamusApiToken)" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "  (LocalMachine DPAPI 'ForgeUi_SamusApiToken' not sealed -- /api/console/* will 503 in production)" -ForegroundColor DarkYellow
    }

    Push-Location $repoRoot
    try {
        $composeArgs = @('compose', '-f', $composeFile, '--env-file', $envFile, 'up')
        if ($Rebuild)  { $composeArgs += '--build' }
        if ($Detached) { $composeArgs += '-d' }
        Write-Host "docker $($composeArgs -join ' ')" -ForegroundColor Cyan
        # docker writes build progress to STDERR; under EAP=Stop that surfaces as
        # a terminating NativeCommandError and ABORTS the compose mid-build (PS5.1
        # trap 3 — this bit a full-stack rebuild 2026-06-30). Drop to Continue for
        # the native call and rely on $LASTEXITCODE for the real result.
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & docker @composeArgs
        $composeExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEap
        if ($composeExit -ne 0) { throw "docker compose up failed (exit $composeExit)" }
    } finally {
        Pop-Location
    }

    # Startup guard (2026-07-03): re-pin Morgan's Vapi server.url + server.secret
    # to the stable ingress domain on EVERY start. A Vapi `PATCH {server:{url}}`
    # replaces the whole server object, so an earlier url-only re-point silently
    # dropped the secret and Vapi stopped sending x-vapi-secret -> the voice
    # handler 403'd every end-of-call-report (silent capture outage, found
    # 2026-07-03). Asserting url+secret together here makes that unrecoverable
    # drift impossible. Best-effort: a pin failure must never fail the stack.
    # Runs while VAPI_* are still hydrated in env (Sync-VoiceWebhook reads env
    # first, DPAPI second), before the finally-scrub below.
    if ($env:VAPI_API_KEY -and $env:VAPI_ASSISTANT_ID -and $env:VAPI_WEBHOOK_SECRET) {
        try {
            & (Join-Path $here 'Sync-VoiceWebhook.ps1')
        } catch {
            Write-Warning "voice webhook pin failed (Vapi end-of-call reports may drop): $($_.Exception.Message)"
        }
    } else {
        Write-Host "  (Vapi secrets not all present -- skipping webhook pin; voice capture stays as-is)" -ForegroundColor DarkGray
    }
} finally {
    # Scrub plaintext from this process's env before exiting -- nothing else
    # in the shell session can read the secrets after this script returns.
    foreach ($var in $exportedVars) {
        Remove-Item -Path "Env:$var" -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Samus stack is up. Probe with:" -ForegroundColor Green
Write-Host "  curl http://127.0.0.1:8100/health" -ForegroundColor Gray
Write-Host "  docker compose -f $composeFile ps" -ForegroundColor Gray
Write-Host ""

# Public ingress check. Since 2026-07-03 the stack fronts ONE ngrok tunnel
# (stable domain) -> samus-ingress (Caddy) -> path-routed to finance/voice/
# outreach, so the voice URL never rotates. The server config is still re-pinned
# on every start by the Sync-VoiceWebhook guard above (url+secret together, so
# the secret can't drift out) — this edge probe just confirms the public front
# door is actually answering; best-effort, never fails the start.
$edgeOk = $false
# curl.exe (ships with Windows 10+) rather than Invoke-RestMethod: PS 5.1
# defaults to TLS 1.0 (ngrok requires 1.2+) and the TLS override is a static
# property setter that ConstrainedLanguage mode (WDAC) blocks outright.
foreach ($attempt in 1..3) {
    $body = & curl.exe -s -m 15 'https://millard-unruffable-reginia.ngrok-free.dev/health' 2>$null
    if ($body -match '"service"\s*:\s*"ingress"') {
        Write-Host "Public ingress healthy (Stripe + Vapi + unsubscribe routes live)." -ForegroundColor Green
        $edgeOk = $true
        break
    } elseif ($body) {
        # -replace keeps this ConstrainedLanguage-safe (no .Substring/[Math]::)
        $preview = $body -replace '(?s)^(.{120}).+$', '$1...'
        Write-Warning "Public edge answered unexpectedly (expected service=ingress): $preview"
        $edgeOk = $true
        break
    }
    if ($attempt -lt 3) { Start-Sleep -Seconds 10 }  # containers may still be settling
}
if (-not $edgeOk) {
    Write-Warning "Public ingress unreachable after 3 probes -- webhooks will not arrive. Check: docker logs samus-ngrok / samus-ingress"
}
Write-Host ""
Write-Host "Live smoke-test the AWS auth + SQS poll round-trip:" -ForegroundColor Green
Write-Host "  docker exec samus-prospecting python -c `"import boto3,json;print(json.dumps(boto3.client('sts').get_caller_identity(),default=str))`"" -ForegroundColor Gray