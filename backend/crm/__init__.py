"""CRM workcell — owns the 7 cross-cutting customer-pipeline tables + in-memory feedback metrics.

Tables (all DDB, single-PK access patterns):

  - ``samus_prospects``       (PK=prospect_id)        — companies in pursuit
  - ``samus_contacts``        (PK=contact_id)         — people at those companies
  - ``samus_conversations``   (PK=conversation_id)    — multi-turn exchange threads
  - ``samus_call-State``      (PK=prospect_id)        — current FSM state of an outbound
                                                        dialer attempt, one per prospect
  - ``samus_opportunities``   (PK=opportunity_id)     — sales deals + pipeline stage
  - ``samus_operator_tasks``  (PK=operator_task_id)   — human-facing to-do items
  - ``samus_artifacts``       (PK=artifact_id)        — generated deliverables
                                                        (proposals, SEO reports, callsheets)
                                                        linked to a prospect/opportunity

Feedback engine (v2, persistent, see :mod:`backend.crm.feedback_engine`):
  Tracks objection frequencies + close counts per product + win/loss per pitch
  angle. Now a thin delegator over the shared, durable
  :mod:`backend.common.feedback_store` (JSON on the samus-data volume), which
  also backs ``backend.outreach.metrics`` — so CRM- and outreach-logged
  outcomes accumulate into ONE store and survive restarts (was a process-local
  dict per workcell). Win-rates feed ``prospecting.intelligence`` angle
  selection. A DDB atomic-counter upgrade can later sit behind the same API.

Phase 1 surface (this workcell, current revision):

  GET  /crm/prospects/{prospect_id}
  GET  /crm/contacts/{contact_id}
  GET  /crm/contacts?prospect_id=...               (full-table scan + filter)
  GET  /crm/conversations/{conversation_id}
  GET  /crm/conversations?prospect_id=...
  GET  /crm/call-state/{prospect_id}
  GET  /crm/opportunities/{opportunity_id}
  GET  /crm/opportunities?stage=...
  GET  /crm/operator-tasks/{operator_task_id}
  GET  /crm/operator-tasks?status=open
  GET  /crm/artifacts/{artifact_id}
  GET  /crm/artifacts?owner_id=...
  POST /crm/convert/lead                           — converts an intake lead
                                                     into a Prospect + Contact pair
  POST /feedback/log                               — record call outcome + objection + angle
  GET  /feedback/snapshot                          — read current METRICS snapshot

Phase 2 — DONE (code-half):

  - pipeline FSM completion: ``closed_won_retainer`` is now a first-class
    won-terminal stage in :mod:`backend.crm.pipeline` (it was declared in the
    model + advance-request schema + analytics but the FSM rejected every
    transition into it as ``unknown_target`` — a latent bug). Reachable from
    qualified / proposal / negotiation, win-probability 1.0.
  - Hivemind graph projection: :mod:`backend.crm.hivemind_projection` mirrors
    the prospect -> contact -> opportunity sub-graph (+ pipeline stage) into the
    local Neo4j KG via :mod:`backend.common.graph_client` on every successful
    opportunity create/advance. Flag-gated default-OFF
    (``SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED``); local-default + fail-closed
    (clean no-op when off OR when Neo4j is unreachable). Promotion of projected
    nodes to the exportable ``hivemind`` tier rides the existing
    ``SAMUS_KG_TIER_MODE=label`` opt-in.

Phase 2+ (still deferred):

  - voice end-of-call -> Conversation + CallState writes (voice workcell
    will dispatch here on webhook receipt)
  - autonomous opportunity creation when lead.intent_score >= threshold
    (deal_scoring_agent port from recovery/)
  - operator_task generators triggered by lifecycle events
  - artifact upload from seo/proposal workcells
  - GSIs on prospect_id / stage / status / owner_id for sub-second lookups
    when full-table scans get too slow (the DDB-GSI / cloud-persistence half of
    Phase-2 — creds-gated opt-in, intentionally not built in the code-half)
  - feedback engine v3: DDB atomic-counter store (ADD updates) behind the
    backend.common.feedback_store API, for cross-container exactness under
    concurrent writes (v2 JSON-on-volume already survives restart)

Architectural pattern: ownership-first. ``samus-crm`` is the only workcell
that writes to these 7 tables. Other workcells (voice, intake, outreach,
finance) call ``samus-crm`` via signed HMAC HTTP for any CRM mutation —
mirrors how every workcell calls ``samus-memory`` for k/v + graph state
instead of touching Neo4j directly.

Capability slots reserved in :mod:`backend.common.capabilities`:
``read_prospects``, ``read_contacts``, ``read_conversations``,
``read_call_state``, ``read_opportunities``, ``read_tasks``,
``read_artifacts``, ``convert_lead``, ``log_feedback``,
``get_feedback_snapshot``.
"""
