"""Voice workcell — Vapi outbound caller + inbound webhook receiver.

Two call directions share one Vapi REST client, one ngrok tunnel, and one
``POST /vapi/webhook`` surface:

  - **Outbound** — the Morgan SDR loop (``recovery/vapi_sales_agent_config.md``):
    ``POST /voice/call`` initiates a call; the morning dialer
    (``POST /voice/dial_call_list``) works a CSV callsheet. End-of-call
    reports extract Morgan's ``lead_summary``, forward it to the memory
    workcell, and upsert a CRM Conversation + CallState row.

  - **Inbound** — the AI Digital Receptionist. A caller rings a client's
    DID; Vapi answers with that client's inbound assistant and posts the
    end-of-call report here. :mod:`backend.voice.service` detects the
    inbound direction, resolves the owning client via
    :mod:`backend.voice.receptionist_config`, and hands off to
    :mod:`backend.voice.inbound`, which persists the call
    (recording / transcript / ``call.json``), writes an inbound CRM
    Conversation + Artifact rows, opens OperatorTasks for appointment /
    callback requests, alerts the client on voicemail / urgent calls, and
    reports billable minutes to the Stripe Meter (via the finance workcell).

All Vapi REST calls go through :class:`backend.voice.client.VapiClient` (thin
httpx wrapper, no SDK — mirrors :mod:`backend.finance.stripe_client`). The
webhook signature is verified by :mod:`backend.voice.signature` (HMAC-SHA256
over the raw body, constant-time compare).

Capability slots in :mod:`backend.common.capabilities`: ``initiate_call``,
``fetch_call``, ``list_calls``, ``handle_webhook``, ``adapt_call``,
``handle_inbound``, ``send_call_summary``.

Architecture context: the Samus architecture doc reserves Caddy port :8080
for the Vapi webhook receiver — this workcell is the receiver. Outbound
end-of-call events persist to the audit ledger + memory + CRM; inbound calls
persist to the per-client artifact tree (``customers/<slug>/calls/``) + CRM.
"""
