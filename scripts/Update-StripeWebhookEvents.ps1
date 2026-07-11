<#
.SYNOPSIS
  Broaden the Stripe webhook endpoint's event subscription so Samus can see
  its own revenue (one-shot; operator-run).

.DESCRIPTION
  ROOT CAUSE (found 2026-07-03): the enabled webhook endpoint
  (https://millard-unruffable-reginia.ngrok-free.dev/stripe_webhook) subscribes
  ONLY to checkout.session.* events. Kerry Brown's $300/mo subscription renews
  via invoice.* events, so finance's stripe_events.jsonl has been silent since
  2026-05-21 and the briefing called the business "pre-revenue". The finance
  webhook handler already persists every event type it receives — broadening
  the subscription is the whole fix on the ingest side.

  ADDS (idempotent — merges with whatever is already subscribed):
    invoice.paid, invoice.payment_succeeded, invoice.payment_failed,
    customer.subscription.created / .updated / .deleted,
    payment_intent.succeeded, charge.succeeded, charge.refunded, payout.paid

  Runs inside samus-finance (holds STRIPE_API_KEY). Read-modify-write; prints
  the endpoint's event list after update. Reversible: re-run Stripe dashboard
  -> Developers -> Webhooks and prune events, or re-set enabled_events.

.EXAMPLE
  .\Update-StripeWebhookEvents.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$py = @'
import os, json, urllib.request, urllib.parse
key = os.getenv("STRIPE_API_KEY")
def api(url, data=None):
    r = urllib.request.Request(url, data=urllib.parse.urlencode(data, doseq=True).encode() if data else None)
    r.add_header("Authorization", "Bearer " + key)
    return json.load(urllib.request.urlopen(r, timeout=20))
we = api("https://api.stripe.com/v1/webhook_endpoints?limit=10")
target = [e for e in we["data"] if e["status"] == "enabled" and "millard" in e["url"]][0]
add = ["invoice.paid","invoice.payment_succeeded","invoice.payment_failed",
       "customer.subscription.created","customer.subscription.updated","customer.subscription.deleted",
       "payment_intent.succeeded","charge.succeeded","charge.refunded","payout.paid"]
merged = sorted(set(target["enabled_events"]) | set(add))
out = api("https://api.stripe.com/v1/webhook_endpoints/" + target["id"], {"enabled_events[]": merged})
print("UPDATED", target["id"], "->", len(out["enabled_events"]), "events:")
for ev in out["enabled_events"]:
    print("  ", ev)
'@

# Piped via stdin (python -) — passing the code with `python -c $py` lets
# Windows native-arg quoting eat the inner double quotes (SyntaxError).
$py | & docker exec -i samus-finance python -
if ($LASTEXITCODE -ne 0) { throw "Stripe webhook update failed (exit $LASTEXITCODE)" }
Write-Host "`nDone. Verify: next Kerry Brown renewal (invoice.paid) should land in finance/stripe_events.jsonl."
