"""Intake workcell — public onboarding form receiver.

Serves the endpoint the hustleforge.tech marketing site's onboarding form
POSTs to:

  - ``POST /intake/onboarding`` — accepts the form payload, validates,
    persists to ``samus_onboarding_leads`` (DDB), audit-logs, returns
    ``{status: queued|duplicate, lead_id, ts}``.
  - ``GET  /intake/leads``       — operator-only paginated list of recent
    leads (capability gated; gateway brokers operator access).

This workcell is the public-facing trust boundary mirror of
:mod:`backend.feedback` — same isolation pattern (own container, own image,
own resource caps, edge + internal networks). The route is browser-callable
(no HMAC, no signature) because the form posts unauthenticated, so the
abuse story relies on:

  - Pydantic validation + length caps (matches the HTML maxlength attrs)
  - 24h dedup keyed by sha256(email|website_url|first 200 chars of pain)
  - CORS allow-list (settings.intake_allowed_origins) — defaults to
    ``https://hustleforge.tech`` only
  - Cloud Run max-instances cap as the natural throughput ceiling

Captcha and IP-rate-limit are deferred — when needed, drop in Turnstile on
the page side and a small token-bucket here.

Capability slots reserved in :mod:`backend.common.capabilities`:
``submit_lead``, ``list_leads``.
"""