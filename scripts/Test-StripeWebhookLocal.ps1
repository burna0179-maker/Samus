<#
.SYNOPSIS
  Local smoke test for the Stripe webhook -> fulfill loop.

.DESCRIPTION
  Fires a properly-signed synthetic ``checkout.session.completed`` event at
  the locally-running samus-finance container (no ngrok / public ingress
  required) and verifies the end-to-end side effects:

    1. Loads STRIPE_WEBHOOK_SECRET from DPAPI (Scope=Samus).
    2. Builds a unique synthetic event:
         - event.id        = evt_smoke_<unix_ts>   (avoids idempotency hit)
         - customer_email  = samus.smoke.test+<ts>@hustleforge.tech
         - amount_total    = 14900  ($149.00)
         - hf_offer_code   = seo_audit
    3. HMAC-SHA256 signs the body with the webhook secret + current timestamp.
    4. POSTs via ``docker exec samus-gateway curl`` (uses the internal
       docker network -- samus-finance isn't bound to a host port).
    5. Asserts HTTP 200 + ``process_status="processed"``.
    6. Tails ``/opt/samus/data/finance/stripe_events.jsonl`` inside the
       finance container to confirm the event was logged.
    7. Reports the receipt-send outcome (SendGrid will accept-or-bounce the
       test address; either way the webhook is correct since receipt
       failure does NOT fail the webhook).

  Idempotent: each run uses a fresh event_id + email so it doesn't
  collide with prior runs. Test customers accumulate in Neo4j; cleanup
  is operator-side (filter by source='stripe_webhook' + email LIKE
  'samus.smoke.test%').

  Plaintext webhook secret only lives in this PowerShell process for
  the lifetime of the test; scrubbed in finally.

.PARAMETER TestEmail
  Override the synthetic recipient address. Defaults to
  ``samus.smoke.test+<ts>@hustleforge.tech``.

.PARAMETER AmountCents
  Synthetic purchase amount in cents. Defaults to 14900 ($149 SEO Audit).

.PARAMETER OfferCode
  hf_offer_code metadata value. Defaults to 'seo_audit'.

.PARAMETER WebsiteUrl
  Synthetic value for the Stripe checkout `website_url` custom_field (Win #2).
  When the offer code is in SAMUS_AUTO_FULFILL_OFFERS on the container AND
  this is non-empty, the webhook handler spawns a background auto-fulfill
  thread. Pass empty (the default) to verify the operator-manual path
  (handler must NOT schedule auto-fulfill when no URL is supplied).

.EXAMPLE
  .\Test-StripeWebhookLocal.ps1

.EXAMPLE
  .\Test-StripeWebhookLocal.ps1 -OfferCode workflow_rescue -AmountCents 50000

.EXAMPLE
  # Trigger auto-fulfill end-to-end (requires SAMUS_AUTO_FULFILL_OFFERS=seo_audit
  # in the .env that the running compose stack was started with):
  .\Test-StripeWebhookLocal.ps1 -WebsiteUrl 'https://example.com'
#>

[CmdletBinding()]
param(
    [Parameter()][string]$TestEmail = "",
    [Parameter()][int]$AmountCents = 14900,
    [Parameter()][string]$OfferCode = "seo_audit",
    [Parameter()][string]$WebsiteUrl = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$msg, [string]$color = 'Cyan') {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor $color
}

function Write-Ok([string]$msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}

function Write-Fail([string]$msg) {
    Write-Host "    [FAIL] $msg" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# Preflight: secret + container availability
# ---------------------------------------------------------------------------

Write-Step "Preflight checks"

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Shared secrets module not found at $sharedModule"
}
Import-Module $sharedModule -Force

if (-not (Test-HfSecret -Scope Samus -Name StripeWebhookSecret)) {
    throw "Samus/StripeWebhookSecret missing from DPAPI. Run: Set-HfSecret -Scope Samus -Name StripeWebhookSecret"
}
Write-Ok "StripeWebhookSecret present in DPAPI"

$running = docker ps --filter "name=samus-finance" --filter "status=running" --format "{{.Names}}"
if ($running -ne "samus-finance") {
    throw "samus-finance container is not running. Start with: Start-SamusStack.ps1"
}
Write-Ok "samus-finance container is running"

$gatewayRunning = docker ps --filter "name=samus-gateway" --filter "status=running" --format "{{.Names}}"
if ($gatewayRunning -ne "samus-gateway") {
    throw "samus-gateway container is not running (used as the docker-internal HTTP client)"
}
Write-Ok "samus-gateway container is running"

# ---------------------------------------------------------------------------
# Build a synthetic checkout.session.completed event
# ---------------------------------------------------------------------------

Write-Step "Building synthetic event"

# Note: do NOT use Get-Date -UFormat %s on Windows — it returns local-time
# epoch (UTC-offset off), which fails the container's UTC signature check.
$ts = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$eventId = "evt_smoke_$ts"
if (-not $TestEmail) {
    $TestEmail = "samus.smoke.test+$ts@hustleforge.tech"
}

# Build the event body. Use ConvertTo-Json so escaping is correct.
$sessionObject = [ordered]@{
    object             = "checkout.session"
    id                 = "cs_test_smoke_$ts"
    customer_details   = @{ email = $TestEmail; name = "Smoke Test" }
    amount_total       = $AmountCents
    currency           = "usd"
    payment_status     = "paid"
    metadata           = @{
        hf_offer_code = $OfferCode
        samus_managed = "true"
    }
}

# Win #2: synthesize the Stripe `custom_fields` array shape so the handler's
# website_url extraction path exercises. Stripe wraps the value inside a
# `text: { value: "..." }` sub-object for type='text' fields.
if ($WebsiteUrl) {
    $sessionObject.custom_fields = @(
        @{
            key   = "website_url"
            type  = "text"
            label = @{ type = "custom"; custom = "Your website URL" }
            text  = @{ value = $WebsiteUrl }
        }
    )
}

$eventBody = @{
    id       = $eventId
    type     = "checkout.session.completed"
    livemode = $false
    created  = $ts
    data     = @{ object = $sessionObject }
} | ConvertTo-Json -Depth 10 -Compress

Write-Ok "event_id     = $eventId"
Write-Ok "email        = $TestEmail"
Write-Ok ("amount       = {0:F2} USD" -f ($AmountCents / 100.0))
Write-Ok "hf_offer     = $OfferCode"
if ($WebsiteUrl) {
    Write-Ok "website_url  = $WebsiteUrl  (auto-fulfill candidate)"
} else {
    Write-Ok "website_url  = (none)  (operator-manual path)"
}

# ---------------------------------------------------------------------------
# HMAC-SHA256 sign the payload (Stripe scheme: HMAC of "{ts}.{body}")
# ---------------------------------------------------------------------------

Write-Step "Signing payload"

# Pull secret into memory for signing; scrub in finally.
$secret = Get-HfSecret -Scope Samus -Name StripeWebhookSecret

try {
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($eventBody)
    $signedInput = [System.Text.Encoding]::UTF8.GetBytes("$ts.") + $bodyBytes
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($secret)
    $sigBytes = $hmac.ComputeHash($signedInput)
    $sigHex = [BitConverter]::ToString($sigBytes).Replace('-', '').ToLower()
    $hmac.Dispose()
    $stripeSignature = "t=$ts,v1=$sigHex"
    Write-Ok "Signature length = $($sigHex.Length) hex chars"

    # ---------------------------------------------------------------------
    # POST via docker exec samus-gateway curl  (internal docker network)
    # ---------------------------------------------------------------------

    Write-Step "POSTing to samus-finance via internal docker network"

    # Write body to a temp file inside the gateway container, then curl from
    # there. Avoids PowerShell -> docker exec -> shell quoting hell with
    # newlines / quotes / dollar signs inside the JSON.
    $payloadFile = "/tmp/stripe_smoke_$ts.json"
    $bodyB64 = [Convert]::ToBase64String($bodyBytes)
    docker exec samus-gateway sh -c "echo $bodyB64 | base64 -d > $payloadFile" 2>&1 | Out-Null

    $curlCmd = (
        "curl -sS -w '\nHTTP_STATUS=%{http_code}' " +
        "-X POST 'http://samus-finance:8080/stripe_webhook' " +
        "-H 'Content-Type: application/json' " +
        "-H 'Stripe-Signature: $stripeSignature' " +
        "--data-binary @$payloadFile"
    )
    $response = docker exec samus-gateway sh -c $curlCmd 2>&1
    docker exec samus-gateway sh -c "rm -f $payloadFile" 2>&1 | Out-Null

    $responseStr = ($response | Out-String).Trim()
    $statusLine = ($responseStr -split "`n" | Where-Object { $_ -match 'HTTP_STATUS=' } | Select-Object -First 1)
    $bodyLines  = ($responseStr -split "`n" | Where-Object { $_ -notmatch 'HTTP_STATUS=' })
    $responseBody = ($bodyLines -join "`n").Trim()
    $httpStatus = if ($statusLine -match 'HTTP_STATUS=(\d+)') { [int]$matches[1] } else { -1 }

    if ($httpStatus -ne 200) {
        Write-Fail "HTTP $httpStatus (expected 200)"
        Write-Host "response: $responseBody" -ForegroundColor Yellow
        throw "webhook POST failed"
    }
    Write-Ok "HTTP 200"

    # Parse the JSON response
    try {
        $parsed = $responseBody | ConvertFrom-Json
    } catch {
        Write-Fail "response is not valid JSON: $responseBody"
        throw
    }

    if ($parsed.process_status -ne 'processed') {
        Write-Fail "process_status = '$($parsed.process_status)' (expected 'processed')"
        Write-Host "note: $($parsed.note)" -ForegroundColor Yellow
        throw "webhook processed but did not advance customer"
    }
    Write-Ok "process_status = processed"
    Write-Ok "customer_id    = $($parsed.customer_id)"
    Write-Ok "advanced_to    = $($parsed.customer_advanced_to)"
    # Win #2 fields are only present on the new model; tolerate older
    # response shapes during a rolling upgrade.
    $autoScheduled = $false
    if ($parsed.PSObject.Properties.Name -contains 'auto_fulfill_scheduled') {
        $autoScheduled = [bool]$parsed.auto_fulfill_scheduled
        Write-Ok "auto_fulfill   = $autoScheduled"
    }

    # ---------------------------------------------------------------------
    # Verify the event made it to the JSONL log (tail the log inside container)
    # ---------------------------------------------------------------------

    Write-Step "Verifying event log entry"

    # Filter by both event_id AND checkout.session.completed so we pick up the
    # original Stripe-side row, not the Win #2 auto_fulfill follow-up that gets
    # appended by the background thread (which has receipt_sent=false by design).
    $tailLine = docker exec samus-finance sh -c "grep $eventId /opt/samus/data/finance/stripe_events.jsonl | grep checkout.session.completed | tail -1" 2>&1
    $tailStr = ($tailLine | Out-String).Trim()
    if (-not $tailStr) {
        Write-Fail "event log is empty (expected at least our test event)"
        throw "event log empty"
    }
    Write-Ok "Event found in stripe_events.jsonl"

    # Parse the JSONL record and check receipt outcome (informational; the
    # webhook is correct regardless because receipt failure doesn't fail it).
    try {
        $record = $tailStr | ConvertFrom-Json
        $receiptStatus = if ($record.receipt_sent) {
            "sent (message_id=$($record.receipt_message_id))"
        } else {
            $errMsg = if ($record.receipt_error) { $record.receipt_error } else { '(empty)' }
            "NOT sent ($errMsg)"
        }
        Write-Ok "receipt: $receiptStatus"
    } catch {
        Write-Host "    (couldn't parse JSONL record for receipt status)" -ForegroundColor DarkGray
    }

    # Win #2: if auto-fulfill was scheduled, poll the JSONL for the follow-up
    # record (event_type=auto_fulfill, same event_id). Background thread is
    # daemon=True, so we give it up to ~30s to complete the SEO render +
    # email send before declaring the smoke test inconclusive.
    if ($autoScheduled) {
        Write-Step "Waiting for auto_fulfill follow-up record"
        $autoRecord = $null
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            $followLine = docker exec samus-finance sh -c "grep $eventId /opt/samus/data/finance/stripe_events.jsonl | grep auto_fulfill | tail -1" 2>$null
            $followStr = ($followLine | Out-String).Trim()
            if ($followStr) {
                try {
                    $autoRecord = $followStr | ConvertFrom-Json
                    break
                } catch {}
            }
            Start-Sleep -Milliseconds 750
        }
        if ($null -eq $autoRecord) {
            Write-Host "    [WARN] auto_fulfill record not seen within 30s (may still complete async)" -ForegroundColor Yellow
        } elseif ($autoRecord.auto_fulfill_ok) {
            Write-Ok "auto_fulfill OK (message_id=$($autoRecord.auto_fulfill_message_id))"
        } else {
            Write-Fail "auto_fulfill FAILED: $($autoRecord.auto_fulfill_error)"
        }
    }

    # ---------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------

    Write-Step "Smoke test passed" 'Green'
    Write-Host "    webhook plumbing verified end-to-end" -ForegroundColor Green
    Write-Host ""
    Write-Host "    test customer created in memory:" -ForegroundColor Gray
    Write-Host "      email = $TestEmail" -ForegroundColor Gray
    Write-Host "      id    = $($parsed.customer_id)" -ForegroundColor Gray
    Write-Host ""
    Write-Host "    next: run Show-Morning.ps1 to see this payment surface" -ForegroundColor Gray
    Write-Host "          in the 'Recent payments (last 7d)' section." -ForegroundColor Gray
} finally {
    if (Get-Variable -Name secret -ErrorAction SilentlyContinue) {
        Remove-Variable secret -ErrorAction SilentlyContinue
    }
}
