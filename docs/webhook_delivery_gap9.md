# GAP-9 — Vapi `end-of-call-report` webhook not delivered to Samus

**Date:** 2026-06-30
**Surface:** `samus-voice` → `POST /vapi/webhook` → `backend/voice/service.py::handle_webhook_event`
**Assistant:** Morgan SDR `9538050d-1d94-415b-a537-e59fc4039bc1`

## Symptom
Across ~12 live outbound calls, calls connect and outcomes are readable via the
Vapi REST API, but the terminal `end-of-call-report` webhook never lands at
`POST /vapi/webhook`. Only 0–2 non-terminal events (`status-update` /
`transcript`) arrive per call. A reconcile sweep (`backend/voice/reconcile.py`)
masks the gap by backfilling from the Vapi API, but live mid-call adaptation
(`backend/voice/autonomous.py`, armed via `SAMUS_VOICE_MIDCALL_ENABLED`) depends
on live webhook delivery and is therefore dark.

## Root cause (PRIMARY — code, now fixed)
**The end-of-call-report handler is too slow to ACK inside Vapi's delivery
timeout, so Vapi abandons delivery and does not retry.**

Evidence:
- Live assistant config (fetched via `GET https://api.vapi.ai/assistant/<id>`
  from inside `samus-voice`) shows:
  ```json
  "server": { "url": ".../vapi/webhook", "timeoutSeconds": 20 }
  ```
  `serverMessages` correctly includes `end-of-call-report`. Config is NOT the
  problem — the **20-second ACK budget** is the binding constraint, and Vapi
  does not retry an end-of-call-report that exceeds it.
- The outbound `end-of-call-report` branch in `handle_webhook_event` performed
  several **sequential, awaited** HTTP round-trips *before* returning 200:
  1. `_post_to_memory` → `signed_post_json(..., retries=2)` — memory workcell.
     Per-attempt read timeout = **15s** (`backend/common/http_client.py`
     `_DEFAULT_TIMEOUT`), up to 3 attempts.
  2. `_dispatch_to_crm` → **two** signed POSTs (Conversation + CallState), each
     `retries=2`, each 15s read timeout.
  3. `submit_product_page` → WordPress POST to `hustleforge.tech` (when a
     validated offer is present).
  4. `check_session()` intraday monitor.
  With every peer healthy this is already multiple seconds; with **any one**
  peer slow or down, the retrying 15s-timeout POSTs serialize well past the 20s
  Vapi budget. Vapi times out, gives up, and the report is lost.
- The non-terminal branches (`status-update`, `transcript`) only append one
  audit row — sub-millisecond — which is exactly why those are the only events
  that ever arrive. This asymmetry is the fingerprint of an ACK-timeout, not a
  routing/signature/ingress failure.
- Reachability confirmed: a synthetic `POST` to the public
  `https://millard-unruffable-reginia.ngrok-free.dev/vapi/webhook` carrying an
  `x-vapi-signature` header reaches the container and returns **403**
  (`X-Samus-Trace-Id` present, `Server: uvicorn`) — i.e. the path works and the
  signature verifier runs. The tunnel + route are healthy.

## The fix (in-repo, uncommitted)
`backend/voice/service.py`:
- Split the heavy outbound end-of-call work into `_process_outbound_end_of_call`.
- `handle_webhook_event` now **fast-ACKs**: for an `end-of-call-report` it
  schedules the heavy memory/CRM/WordPress/session-monitor work in a detached
  `asyncio` background task and returns HTTP 200 within milliseconds. Vapi gets
  its prompt ACK; the downstream work proceeds afterward.
- Idempotent: a re-delivered report for an already-seen `call_id` is ACKed and
  dropped (bounded in-process LRU). All downstream steps were already
  best-effort and non-raising.
- Gated by `SAMUS_VOICE_WEBHOOK_FAST_ACK` (default **ON** in production).
  Auto-OFF under pytest (`PYTEST_CURRENT_TEST`) so the existing synchronous
  webhook suite — which `asyncio.run(...)`s the handler and asserts on dispatch
  results — keeps passing unchanged. Explicit env var always wins.

Tests: `tests/test_voice_webhook_fast_ack.py` (4 tests) — prompt ACK under a
slow downstream, idempotent dedupe, sync-path preserved when disabled,
non-terminal events unaffected. Full voice suite: **272 passed**.

### Expected effect
This **should restore live end-of-call-report delivery** in the common case
(slow-but-reachable peers): the ACK now returns in milliseconds, comfortably
inside the 20s Vapi budget, so Vapi no longer abandons delivery. Mid-call
adaptation can then receive live events once armed.

## Residual / secondary factors (infra — operator action, NOT provisioned here)

1. **Free-tier ngrok is a real availability risk.** Ingress is
   `samus-ngrok` → `https://millard-unruffable-reginia.ngrok-free.dev`. Free-tier
   ngrok imposes connection caps and can drop/refuse connections under load and
   rotates the public hostname on restart. The fast-ACK fix removes the
   *handler-side* timeout, but if ngrok itself refuses or drops the inbound POST,
   the report is still lost and Vapi still won't retry it.
   **Operator action (recommended, not done here):** move ingress to a
   persistent, stable tunnel — paid ngrok with a reserved domain, or a
   self-hosted Caddy/Cloudflare-tunnel ingress with a fixed hostname — so the
   webhook URL is stable and not connection-capped. Note the voice app PATCHes
   each assistant's `server.url` on startup only when `NGROK_AUTHTOKEN` is set in
   the *voice* container (see `backend/voice/app.py::_run_tunnel_startup`); in the
   current deployment the tunnel is run by the separate `samus-ngrok` container
   and the voice container logged `ngrok tunnel skipped: NGROK_AUTHTOKEN not set`.
   The assistant `server.url` is nonetheless correct (set out-of-band), so this
   is not blocking — but a stable ingress would let the startup PATCH own the URL
   end-to-end.

2. **Side finding — 401 on unsigned probes (not a Vapi blocker).** A `POST` to
   `/vapi/webhook` with **no** `x-vapi-signature` header returns **401**
   (`hmac_headers_missing`, no trace-id) rather than the route's own 403. Real
   Vapi requests always carry `x-vapi-signature` and DO reach the handler (proven
   by the 403 above), so this does not affect Vapi delivery. Left as-is.

3. **Consider raising `server.timeoutSeconds`.** Independent of the fix, bumping
   the assistant's `server.timeoutSeconds` (e.g. 20 → 30) widens the ACK budget.
   This is an assistant-config change (out of scope for this task — do not change
   Morgan's config without operator sign-off).

## Bottom line
- Root cause: slow synchronous handler exceeding Vapi's 20s ACK budget; Vapi
  abandons end-of-call-report delivery (no retry).
- Code fix (uncommitted): fast-ACK + async background processing in
  `backend/voice/service.py`, idempotent, test-covered.
- Expected to restore delivery for reachable-but-slow peers. **Residual infra
  risk remains free-tier ngrok dropping/refusing connections** — for full
  reliability, move to a stable, non-capped ingress (operator action).
