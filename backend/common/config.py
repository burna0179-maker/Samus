"""Pydantic Settings + LRU-cached ``get_settings`` per doc §3.2.

Reads from environment variables at construction time. Workcells call
``get_settings()`` to read a single cached instance. ``reload_settings()`` is
for tests + secret-rotation tooling.

``bootstrap_settings()`` (in ``settings.py``) is the .env-loading constructor.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    # --- service identity / runtime ----------------------------------------
    service_name: str = "unknown"
    env: str = "development"
    port: int = 8000
    log_level: str = "INFO"

    # --- inter-service HMAC -----------------------------------------------
    shared_hmac_key: str = ""
    hmac_window_seconds: int = 300

    # --- R-1 per-service HMAC keys + caller authorization -----------------
    # Per-service HMAC keys, env ``SAMUS_HMAC_KEY_<SERVICE>`` (service name
    # uppercased). The SENDER signs with its own key; the receiver resolves
    # the caller's key from the signed ``X-Samus-Caller`` header. Any service
    # without a dedicated key falls back to ``shared_hmac_key`` — so a stack
    # with only the shared key keeps working unchanged (additive, non-breaking
    # by design). This map is populated for /show-config visibility ONLY; the
    # authoritative resolver is ``security.resolve_service_key`` which reads
    # the env live so a freshly-seeded key is picked up on the next reload.
    # Values are never logged in full — see ``hmac_keys_summary``.
    per_service_hmac_keys: dict[str, str] = Field(default_factory=dict)
    # Caller→callee authorization enforcement mode. One of off|audit|enforce
    # (env ``SAMUS_AUTHZ_MODE``). DEFAULT ``off`` — the caller-authz matrix is
    # dormant and behaviour is exactly pre-R-1 until the operator opts in.
    # ``audit`` evaluates the matrix and logs would-be denials but allows;
    # ``enforce`` raises 403 on a denial. See backend/common/capabilities.py.
    # The live mode is read fresh by ``capabilities.authz_mode()``; this field
    # is the boot-time snapshot for /show-config.
    authz_mode: str = "enforce"

    # --- operator_console PACK auth gate ----------------------------------
    # When True (the default — ecosystem-wide tightened posture as of
    # 2026-05-18), /api/console/* routes (mounted on the gateway workcell)
    # require Authorization: Bearer <api_token>; when False, the pack's
    # _token_dep short-circuits. Env binding via SN_BEARER_REQUIRED (the
    # cross-agent convention) populated by bootstrap_settings(). Distinct
    # from the gateway-level auth surface; this only gates the operator
    # console pack routes that the gateway workcell exposes.
    bearer_required: bool = True

    # Master kill-switch for ALL outbound HTTP calls to external services
    # (Stripe, Gemini, Wix, SEO/prospecting fetches, etc.). Default ON.
    # Set False to block all external egress without stopping Samus.
    # Inter-agent calls to localhost are NOT affected. The runtime guard
    # is not wired yet; this field exists so the flag spec in
    # backend/common/flags/catalog.py can resolve its default via
    # getattr(Settings(), name) without raising AttributeError. Env
    # binding: SAMUS_OUTBOUND_CALLS_ENABLED.
    outbound_calls_enabled: bool = True

    # --- canonical audit ledger (Hustleforge agent §6) --------------------
    # Dedicated ledger-signing secret, split from shared_hmac_key so
    # rotating session / replay / nonce keys does NOT invalidate the
    # tamper-evident audit chain. Empty -> falls back to shared_hmac_key
    # (which itself falls back to a dev placeholder in
    # ``backend/common/audit_ledger.py``); operators SHOULD seal a
    # dedicated SAMUS_LEDGER_SECRET_KEY before any production-trust audit
    # value accrues. Port of Anita's sn_ledger_secret_key split.
    samus_ledger_secret_key: str = ""
    # Epoch length for the rotating HMAC. 24h is canonical; tests
    # override to verify rotation logic with short epochs.
    samus_ledger_epoch_sec: int = 86400
    # Overlap window inside which a verifier will try the neighbouring
    # epoch's key. 5 min accommodates clock skew at the epoch boundary.
    samus_ledger_epoch_overlap_sec: int = 300

    # --- CORE key/secret management (E5 D15) ------------------------------
    # The HKDF key vault (backend/common/keys/key_vault.py) derives all
    # purpose-specific sub-keys from ONE long-lived master so Samus carries
    # a single root secret. The master resolves
    # ``samus_ledger_secret_key`` -> ``shared_hmac_key`` -> a deterministic
    # per-process dev key (degraded posture; never raises so boot is never
    # blocked). The secret-rotation manager bumps the vault generation on an
    # interval and records a fingerprint backup; it never rewrites the
    # operator's on-disk DPAPI .cred store. Default OFF — the rotation
    # cycle is dormant until an operator arms it.
    samus_secret_rotation_enabled: bool = False  # SAMUS_SECRET_ROTATION_ENABLED
    # Interval between vault-generation rotations once enabled. 24h canonical.
    samus_secret_rotation_interval_sec: int = 86400
    # Overlap window in which the prior generation's sub-keys remain
    # derivable after a rotate (clock-skew + in-flight-token tolerance).
    samus_secret_rotation_overlap_sec: int = 3600

    # --- pluggable append-ledger backend (W-1) ----------------------------
    # Where append-only ledgers (the Stripe webhook idempotency log, the
    # upsell nurture queue) persist. "jsonl" — append-only files on a bind
    # mount (local / HustleForge-VM, the default); "firestore" — Firestore
    # collections (GCP Cloud Run, where the container filesystem is
    # ephemeral + per-instance so a JSONL ledger silently corrupts on any
    # scale-to-zero). Read directly from SAMUS_LEDGER_BACKEND by
    # ``backend/common/persistence.py:open_ledger``.
    samus_ledger_backend: str = "jsonl"

    # --- Pattern-B heartbeat (Hustleforge ecosystem §9) -------------------
    # Major's heartbeat ingestion URL. Defaults to VM-internal loopback
    # (Major runs in the same Compose project / on the same VM post-
    # migration). Empty disables the HTTP channel but the file channel
    # still fires. Reuses the env var name ``SECURITY_SENTINEL_URL`` for
    # back-compat with the older heartbeat shim and other agents that
    # share the same Major target.
    security_sentinel_url: str = "http://localhost:8434"
    # Observable file path consumed by peer agents without going through
    # Major. Resolved at runtime by ``backend/common/heartbeat.py`` via
    # SAMUS_HEARTBEAT_PATH so OS-specific default selection happens
    # there (Windows path on host, POSIX path inside the container).
    # Stored here for /show-config visibility; empty -> resolver default.
    samus_heartbeat_path: str = ""
    # Beat interval. 30s matches the ecosystem convention.
    samus_heartbeat_interval_sec: float = 30.0
    # Agent ID embedded in every beat envelope. Must equal Major's
    # registry entry for Samus.
    samus_agent_id: str = "samus"

    # --- inter-service routing --------------------------------------------
    gateway_urls: dict[str, str] = Field(default_factory=dict)
    sqs_queue_urls: dict[str, str] = Field(default_factory=dict)

    # --- cash_engine (autonomous revenue pipeline; escalation front door) --
    # The Cash Engine routes a prospect through a single codex-gated ingress
    # (``POST /api/samus/review_opportunity``): it does NOT execute on its
    # own authority — it HALTS and forces the operator's Stake Sentence to be
    # the unlock for every revenue-critical sequence. ``cash_engine_enabled``
    # gates the engine as a whole. ``cash_engine_decay_threshold`` is the
    # DecayRiskScore (0.0-1.0) above which the signal_decay trigger fires.
    # ``cash_engine_stall_days`` is the silence window that saturates the
    # staleness component of that score. ``cash_engine_queue_service`` names
    # the SQS queue key used when one is configured in ``sqs_queue_urls``;
    # absent (the default), enqueue falls back to a local JSONL mock queue so
    # the flow is provable end-to-end without AWS (logic-first per the build
    # plan).
    cash_engine_enabled: bool = True
    cash_engine_decay_threshold: float = 0.6
    cash_engine_stall_days: float = 7.0
    cash_engine_queue_service: str = "cash_engine"
    # When True, the outreach stage doesn't just compose + draft the initial
    # packet — it hands it to outreach.send_message for a LIVE SES send.
    # Default OFF (and additionally requires ``ses_from_email`` set), so the
    # sequence runs fully end-to-end producing a reviewable draft + a
    # scheduled follow-up WITHOUT ever firing real email until the operator
    # deliberately flips this on.
    cash_engine_live_send_enabled: bool = False
    # --- constitutional caps (HOTL Tranche 5) ------------------------------
    # Hard daily ceiling on autonomous outbound messages (all send channels)
    # enforced in backend.outreach.service.send_message BEFORE any dispatch.
    # Default 15 matches the send-ramp warmup comment. Breaching it blocks the
    # send, files an operator task, and emits a decision.made event. This is a
    # constitutional constraint, not a pacing knob — it is always on.
    samus_max_sends_per_day: int = 15
    # Hard daily ceiling on autonomous Vapi outbound calls, enforced in
    # backend.voice.service.initiate_call the same way.
    samus_max_calls_per_day: int = 40
    # CAN-SPAM compliance fields stamped into every composed outreach footer.
    # Empty is tolerated for drafts; a live send should have both set.
    sender_postal_address: str = ""
    unsubscribe_url: str = ""
    # --- compliance guard (CAN-SPAM/FTC deterministic pre-send gate) -------
    # Mirrors the SAMUS_AUTHZ_MODE three-state pattern. "off" (default) =
    # dormant: the guard is wired into email_backend.send_email but never
    # consulted. "audit" = evaluate + log would-block reasons + inject
    # List-Unsubscribe headers, but never block. "enforce" = block sends that
    # fail a hard structural rule (suppressed recipient, missing unsubscribe /
    # postal address on commercial mail). Ramp off -> audit -> enforce. See
    # backend/common/compliance_guard.py.
    compliance_guard_mode: str = "off"
    # Optional extra sender domains (comma/space-separated) the guard treats as
    # legitimate From domains beyond sendgrid_from_email / ses_from_email.
    compliance_from_domains: str = ""
    # --- Heat Field (email deliverability/reputation throttle) -------------
    # When True, the cadence loop multiplies its per-sweep send budget by the
    # heat-derived multiplier and the live-send gate honors safe-mode (critical
    # band -> pause). Default OFF (wire-not-arm): event ingestion + the per-deal
    # halt fan-out still run, but no autonomous throttle until armed. See
    # backend/heat/.
    heat_field_enabled: bool = False
    # ECDSA public key (base64 DER) from the SendGrid "Signed Event Webhook"
    # settings; verifies the /api/sendgrid/events POST signature. Empty -> in a
    # production env the route fails closed; in dev it accepts unverified (tests).
    sendgrid_webhook_verification_key: str = ""
    # DynamoDB table for the daily heat-state counters (PK=scope). Empty -> the
    # JSON fallback (SAMUS_HEAT_STATE_PATH) is the sole store.
    ddb_heat_state_table: str = "samus_heat_state"
    # --- Karma-gated permissions (earned progressive autonomy) ------------
    # When True, the cash-engine gate + live-send path consult per-actor karma
    # (backend/governance/karma) and deny actions whose required AccessTier the
    # actor hasn't earned. Default OFF (wire-not-arm): karma deltas still accrue
    # a track record, but no action is denied until armed. Innocent-until-
    # penalized — arming is a no-op for a well-behaved Samus.
    karma_gate_enabled: bool = False
    # DynamoDB table for per-actor karma vectors (PK=actor). Empty -> the JSON
    # fallback (SAMUS_KARMA_PATH) is the sole store.
    ddb_karma_table: str = "samus_karma"
    # --- inbound reply-handling pod chain ---------------------------------
    # When True, the Gmail poller classifies each inbound reply (intent),
    # auto-honors opt-outs (suppress + halt), fires the cash-engine re-engage
    # signal on positive intents, and attaches a compliance-checked follow-up
    # DRAFT (never auto-sent) for the operator. Default OFF: the poller's
    # current observe-only behavior (artifact + reply task) is unchanged until
    # armed. See backend/intake/reply_classifier + follow_up_drafter.
    reply_handling_enabled: bool = False
    # --- email-variant attribution (template/subject/CTA bandit) ----------
    # When True, the campaign compose path lets the attribution engine choose
    # among >1 configured variants per slot (UCB1). Default OFF: deterministic
    # passthrough (first/only variant = current behavior). record_outcome
    # accrues stats regardless so a track record exists before arming. Pays off
    # only with real send volume + a multi-variant catalog. See backend/attribution.
    attribution_enabled: bool = True
    # DynamoDB table for per-variant bandit stats (PK=scope=arm_id). Empty ->
    # the JSON fallback (SAMUS_ATTRIBUTION_PATH) is the sole store.
    ddb_attribution_table: str = "samus_attribution_variants"

    # --- cognitive self-model tier (ported from recovery/) ----------------
    # backend/memory/persona_self_model.py — a WAL+JSON persistent self-model /
    # emotional-history tier. When True it loads prior state on construct and
    # records emotion/reflection events to disk (under the writable state root
    # via state_paths; SAMUS_PERSONA_SELF_MODEL_PATH overrides). Default OFF
    # (dormant): the store is a no-op (no load, no disk writes) until armed.
    # No live caller — it is the substrate the persona-frame tier reads. See
    # backend/memory/persona_self_model.
    persona_self_model_enabled: bool = False
    # --- cognitive persona-frame (Stage 1; COMPUTE-ONLY) ------------------
    # backend/cognitive/persona_frame.py — derives an operating-mode frame
    # (NORMAL/CAUTIOUS/RECOVERY/EXPLORE) from the persona self-model's
    # emotional/drift state. When False, transform() emits a disabled neutral
    # frame and performs no derivation. Default OFF (dormant). COMPUTE-ONLY:
    # even when armed, no frame is attached to any live send/chat/outreach/
    # cash_engine path — wiring a consumer is deferred to an operator decision.
    # See backend/cognitive/persona_frame.
    persona_frame_enabled: bool = False

    # --- autonomy layer (backend.autonomy.* subsystems; build-plan Phase B) -
    # The three flagged META-REFLECTION subsystems the (dormant, Phase-D)
    # MetaCognitionEngine wraps around the CognitiveLoop. ALL DEFAULT OFF
    # (wire-not-arm). When a flag is False its subsystem is INERT — a no-op /
    # ""/False/empty return — byte-identical to the subsystem being absent, so
    # nothing the wrapper does has any effect until an operator arms it. No live
    # caller constructs these; they are the substrate the autonomy wrapper uses.
    # See backend/autonomy/.
    #   * reinforcement — bounded RL weight evolution per situation type,
    #     persisted under state_path("autonomy","heuristic_weights.json").
    #     Off => get_weight returns the neutral 1.0, reinforce is a no-op,
    #     snapshot is empty. SAMUS_AUTONOMY_REINFORCEMENT_ENABLED.
    autonomy_reinforcement_enabled: bool = False
    #   * autotuner — advisory bounded runtime self-tuning from latency/error/
    #     compliance signals. NOTHING is wired to a live runtime parameter in
    #     this phase. Off => adjust is a no-op. SAMUS_AUTONOMY_AUTOTUNER_ENABLED.
    autonomy_autotuner_enabled: bool = False
    #   * upgrade — PROPOSE-ONLY architecture-upgrade evaluator. execute() only
    #     ever APPENDS a proposal record to a JSONL ledger for human/governance
    #     review; it NEVER self-modifies code/config/behaviour. Off => evaluate
    #     returns False, execute is a no-op. SAMUS_AUTONOMY_UPGRADE_ENABLED.
    autonomy_upgrade_enabled: bool = False
    # Governed autonomous dial policy (ADR-016). Default OFF: voice_dial stays
    # VR-G5-blocked. When armed, the Codex ALLOWS voice_dial ONLY under a fully-
    # attested policy (stake_sentence + within_call_hours + cooldown_ok +
    # under_daily_cap + dnc_ok, all enforced by the actuation path). Flipping this
    # off is an instant hard block again. SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED.
    governed_autonomous_dial_enabled: bool = False
    # Idle-production drive (self-pacing engine). Default OFF: when armed, the
    # control-tick loop lets Samus DECIDE — when production has gone idle during
    # business hours and it is behind pace — to progress it through the governed
    # channels (email/stake, and calls via the governed dial policy above). This
    # is what replaces an external prompter with the agent's own reasoning.
    # SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED.
    idle_production_drive_enabled: bool = False
    #   * meta — the MetaCognitionEngine wrapper (backend/cognitive/
    #     meta_cognition_engine.py; build-plan Phase D) that layers
    #     META-PERCEPTION (situation index + heuristic pre-bias) before and
    #     META-REFLECTION (autotune + upgrade-eval + persistence + reinforcement +
    #     persona notify) after a wrapped CognitiveLoop.cycle(). DEFAULT OFF: when
    #     False ``MetaCognitionEngine.cycle`` is a PURE PASSTHROUGH — it delegates
    #     straight to the wrapped ``loop.cycle(inp)`` and runs no meta stage, so
    #     the result is byte-identical to calling the loop with no wrapper. No
    #     live caller constructs it. SAMUS_AUTONOMY_META_ENABLED.
    autonomy_meta_enabled: bool = False
    #   * act-proposals — the CognitiveLoop's PROPOSE-ONLY ACT stage
    #     (build-plan Phase E). When armed, ACT records ONE structured proposal
    #     artifact to state_path("cognition","proposals.jsonl") derived from the
    #     read-only domain perception — it calls NO effector and NO gate
    #     (review_opportunity / cash_engine / outreach send / finance all
    #     untouched). Off => ACT is a PURE NO-OP that writes nothing (dormant
    #     default; byte-identical to the prior no-op stub). Disposition of any
    #     recorded proposal stays with the governance gates + a Phase-G
    #     (operator-gated) promoter. SAMUS_COGNITIVE_ACT_PROPOSALS_ENABLED.
    cognitive_act_proposals_enabled: bool = False
    #   * cognitive-loop — the MASTER switch for the FIRST LIVE CALLER of the
    #     cognitive stack (build-plan Phase F). DEFAULT OFF (wire-not-arm). This
    #     gates whether ANY live caller constructs/runs the loop at all: when
    #     False, the Phase-F entrypoint (POST /api/samus/cognition/cycle, and any
    #     future cadence hook) is a pure no-op/503 — it builds NOTHING (no
    #     CognitiveLoop, no MetaCognitionEngine, no LiveDomainProvider, no LLM
    #     reasoner), touches no CRM/finance/LLM backend, and there is NO
    #     import-time construction. When True, the caller assembles the stack
    #     (backend/cognitive/runner.py::build_cognition_runtime) and runs exactly
    #     one cycle. Each sub-behaviour STILL respects its OWN flag
    #     (autonomy_meta_enabled / cognitive_act_proposals_enabled / persona_*),
    #     so arming the loop alone keeps ACT propose-only and meta-cognition off
    #     until those are independently armed. SAMUS_COGNITIVE_LOOP_ENABLED.
    cognitive_loop_enabled: bool = False
    #   * proposal-promotion — Phase G [build-plan]: the FIRST path by which a
    #     propose-only ACT artifact can influence live revenue behaviour. When
    #     armed, backend/cognitive/proposal_promoter.py routes unactioned
    #     proposals through review_opportunity (the cash-engine front door),
    #     which itself enforces Stake Sentence + Codex + karma + downstream
    #     compliance/EFH — Phase G calls those gates, NEVER bypasses them. A
    #     BLOCK from the gates is sticky (the proposal is marked actioned and
    #     never retried; the cycle's next proposal is a fresh chance). DEFAULT
    #     True per operator direction (the operator's "wire ON" stance) — flip
    #     to False to disable the path fast; with False, the runner seam is a
    #     pure no-op (zero ledger reads, zero gate calls, zero telemetry).
    #     SAMUS_COGNITION_PROPOSAL_PROMOTION_ENABLED.
    cognition_proposal_promotion_enabled: bool = True
    #   * promotion BLOCK cooldown — per-prospect suppression window after a
    #     BLOCK verdict. The active cognition cadence re-promotes the same
    #     pending-stake prospect every ~60s tick; each attempt re-runs
    #     review_opportunity and gets the SAME sticky BLOCK (e.g. "no
    #     operator-authored Stake Sentence"), spamming the promotion ledger
    #     with identical BLOCK->escalated rows. After a BLOCK for a prospect_id
    #     the promoter suppresses re-promotion of that SAME prospect_id for
    #     this many seconds: within the window the proposal is SKIPPED
    #     (reason='block_cooldown') WITHOUT calling review_opportunity. Only a
    #     BLOCK sets/refreshes the cooldown; PASS / ERROR / no-prospect_id SKIP
    #     do not. State is module-level (resets on gateway restart). Default 600
    #     (~10 ticks at the 60s cadence). SAMUS_COGNITION_PROMOTION_BLOCK_COOLDOWN_SEC.
    cognition_promotion_block_cooldown_sec: int = 600
    #   * cognition-cadence — the ACTIVE autonomous tick loop wrapping the
    #     Phase-F runner (``backend/cognitive/cadence.py``). When True
    #     (the operator-directed default), the gateway lifespan starts an
    #     asyncio loop that calls ``run_one_cycle`` on a base+jitter cadence
    #     from INSIDE the always-on container, so the cognitive stack self-
    #     drives without depending on an external scheduler. When False the
    #     lifespan does NOT start the task at all (pure no-op).
    #     **The cadence honours the MASTER switch every tick**: when
    #     ``cognitive_loop_enabled`` is False, ticks fire but invoke nothing
    #     (the cadence loop stays alive, so a runtime arm wakes the stack
    #     without a gateway restart). And each commercial sub-behaviour
    #     (meta / ACT-proposals / promotion / persona) still honours its OWN
    #     flag inside the runner — disabling any one layer leaves the cadence
    #     loop itself running. Safety rails are built in: single-flight (no
    #     overlapping cycles), exception isolation (a cycle fault never kills
    #     the loop), and broker backoff (5 consecutive Budget/LlmCall errors
    #     trip a cool-off, capped at 10× the base interval).
    #     SAMUS_COGNITION_CADENCE_ENABLED.
    cognition_cadence_enabled: bool = True
    # Base interval between cadence ticks (seconds). The actual gap is
    # ``interval + Uniform(0, jitter)`` so a multi-instance fleet doesn't
    # synchronise. SAMUS_COGNITION_CADENCE_INTERVAL_SECONDS.
    cognition_cadence_interval_seconds: int = 60
    # Per-tick jitter window in seconds; 0 disables jitter (deterministic
    # cadence — useful for tests). SAMUS_COGNITION_CADENCE_JITTER_SECONDS.
    cognition_cadence_jitter_seconds: int = 10

    #   * codb-reasoner — wire-not-arm switch for the CODB investment reasoner
    #     (``backend/cognitive/codb_reasoner.py``). When True, the End-of-Day
    #     review calls the reasoner and includes a
    #     ``codb_investment_recommendations`` section, and the finance
    #     deal-closed stimulus emits guidance-ledger records ranking which CODB
    #     scale-up to buy next. When False (DEFAULT — wire-not-arm), the EOD
    #     section is omitted and the stimulus hook is a pure no-op. The reasoner
    #     is RECOMMEND-ONLY in either state: it computes affordability + ROI and
    #     writes guidance records, but NEVER purchases or spends. Actual spend
    #     stays an operator action. SAMUS_CODB_REASONER_ENABLED.
    codb_reasoner_enabled: bool = False

    # --- soft-no re-engagement sweep (cooldown -> promotional resurfacing) -
    # backend/crm/reengagement_sweep.py rescans samus_call-State for prospects
    # whose last_outcome was "not_interested" (a soft no per call_outcomes.py)
    # AND whose updated_at is older than the cooldown window. Each match fires
    # one Cash Engine review with trigger_source="reengagement", which routes
    # the outreach stage through a distinct promotional template that leads
    # with a discount/new-product angle rather than the cold-outreach opener.
    # Default OFF (dormant): the sweep is a no-op until an operator arms it.
    # do_not_call / disqualified are HARD nos and are excluded by construction
    # (filter on last_outcome=="not_interested" only), with a defence-in-depth
    # skip pass inside the sweep.
    samus_reengagement_enabled: bool = False
    # Days a prospect must have been quiet since the not_interested log before
    # they qualify for resurfacing. Operator-tunable. 30 mirrors the soft-no
    # convention (long enough for the prospect to have a different situation;
    # short enough to keep cashflow alive).
    samus_reengagement_cooldown_days: int = 30
    # Back-pressure: max prospects requeued per sweep run. Keeps a single
    # sweep from saturating the cash_engine queue or the daily send budget.
    samus_reengagement_max_per_run: int = 25

    # --- per-number dial cooldown -----------------------------------------
    # After any actual dial attempt (not a skipped_* row), block the same
    # prospect_id / phone from re-entering the dialer for this many days.
    # Operator spec: 7-14 day floor so a number called Tuesday isn't redialled
    # until the following Mon-Fri at earliest. Set to 0 to disable.
    # IMPORTANT: this also short-circuits the 24-36h no-answer/voicemail retry
    # queue — by design. Same-day retries violate the per-number floor.
    samus_dialer_cooldown_days: int = 7
    # Same-day voicemail-retry exemption: if the prior dial left voicemail
    # (or machine_detected_*) AND a retry queue entry for this prospect/phone
    # is still within ``samus_dialer_voicemail_exempt_hours``, the per-number
    # cooldown is bypassed so the retry queue can run its short window. Set
    # to 0 to disable the exemption (cooldown wins on every voicemail). The
    # voicemail retry queue itself is in backend/voice/retry.py.
    samus_dialer_voicemail_exempt_hours: int = 36
    # Business-hours gate (wired-DORMANT). When armed, the dialer skips a
    # prospect whose business_hours say it is closed at the prospect-local
    # moment of the dial (outcome=skipped_closed), cutting wasted voicemails.
    # Fail-OPEN: unparseable/missing hours never block a dial. DEFAULT OFF.
    samus_dialer_business_hours_gate: bool = False

    # Buying-signal email route (wired-DORMANT). When a voice call surfaces real
    # intent, the end-of-call handler enrolls the prospect into the warm
    # ``buying_signal`` sequence instead of leaving follow-up to the cold path.
    # Two keys, mirroring cash_engine:
    #   * ``outreach_buying_signal_route_enabled`` arms ENROLLMENT (the
    #     end-of-call handler writes enrollments). Default OFF -> no enrollment.
    #   * actual SENDING is a second step: ``dispatch_due_buying_signal`` is
    #     dry-run by default and has no scheduler wired, so even with enrollment
    #     armed nothing leaves until the operator runs/wires dispatch live.
    # OUTREACH_BUYING_SIGNAL_ROUTE_ENABLED.
    outreach_buying_signal_route_enabled: bool = False
    # Intent floor (0-100) that qualifies a call as a buying signal. book_call /
    # tier in (high, priority) always qualify regardless of score; disqualify
    # never qualifies. OUTREACH_BUYING_SIGNAL_INTENT_THRESHOLD.
    outreach_buying_signal_intent_threshold: int = 70

    # Site telemetry ingest (wired-DORMANT). When armed, POST /intake/telemetry
    # records page/form/pricing/CTA events into a JSONL ledger for the morning
    # briefing to reason over ("started 3 checkouts, completed 0" is only
    # meaningful with a matching "pricing page viewed 40 times"). When OFF the
    # endpoint returns 200 with status='dropped_disabled' so the site's beacon
    # is never a source of noise. Storage-bounded on purpose: JSONL only, no
    # DDB, so the schema stays soft while the operator iterates on the shape
    # of the funnel report. SAMUS_TELEMETRY_INGEST_ENABLED.
    intake_telemetry_ingest_enabled: bool = False

    # Website-form warm-lead enrollment (wired-DORMANT). When a hustleforge.tech
    # onboarding form submission carries declared service_interest (i.e. not
    # empty and not just 'not_sure'), the intake handler enrolls the lead into
    # the same warm buying_signal sequence used by voice + email_reply paths.
    # Independent from the voice-path flag above: the website-form gate is its
    # own arming key, so the operator can arm site-sourced leads without also
    # arming voice enrollments. Dispatch is still the separate second step
    # (dispatch_due_buying_signal), so even fully armed, nothing leaves until
    # a composer + non-dry-run scheduler is wired.
    # Rationale: without this path, every website-sourced lead sits in
    # DynamoDB and never enters any warm cadence — the "missing warm/hot leads
    # from the site the agent owns" gap flagged during the funnel audit.
    # OUTREACH_WEBSITE_FORM_WARM_ENROLL_ENABLED.
    outreach_website_form_warm_enroll_enabled: bool = False

    # --- post-call remediate -> next-touch loop (wired-DORMANT) -----------
    # Closes the outbound "audit -> remediate -> next-touch" loop. When a call
    # completes with a POSITIVE/INTERESTED outcome (same classifier the
    # buying-signal route uses — recommended_action=book_call / tier in
    # high|priority / intent_score >= buying-signal threshold; disqualify never
    # qualifies), the end-of-call handler ALSO (a) delivers the SEO remediation
    # deliverable to the prospect via the existing backend.fulfill seam and
    # (b) enqueues a follow-up touch via the existing buying_signal enrollment
    # seam — with NO operator hand-run. Both steps are best-effort + fail-soft
    # (a delivery/enroll error is logged, never breaks the webhook). This NEVER
    # initiates a phone call: "next-touch" = enrollment/queue only; any dial is
    # still fenced behind VR-G5/ADR-002 + the human FIRE gate.
    # DEFAULT OFF: with the flag off the post-call path is byte-identical to
    # today (no delivery, no enroll fan-out). SAMUS_POSTCALL_REMEDIATE_ENABLED.
    postcall_remediate_enabled: bool = False

    # --- open-no-click nudge watcher (default DORMANT) -------------------
    # When a tracked outbound email gets an ``open`` event but no ``click``
    # within the dwell window, fire one soft-nudge email. Reads the SendGrid
    # event journal already written by heat/service.py
    # (``engagement_<day>.jsonl``); never fires more than once per watch record;
    # gated by the flag so it can be wired without sending.
    # OUTREACH_OPEN_NO_CLICK_NUDGE_ENABLED.
    outreach_open_no_click_nudge_enabled: bool = False
    # Hours after the first observed ``open`` to wait before firing the nudge.
    # OUTREACH_OPEN_NO_CLICK_DWELL_HOURS.
    outreach_open_no_click_dwell_hours: int = 24

    # --- send-lint mode (off | audit | enforce; default off) ----------------
    # Pre-send check that extracts buy URLs from a prepared message,
    # resolves them to catalog SKUs, and warns when the subject implies a
    # different SKU than the hero button sells. Backs the
    # ``send_promotional()`` wrapper. ``audit`` logs warnings and sends
    # anyway; ``enforce`` raises SendLintBlocked on misalignment.
    # OUTREACH_SEND_LINT_MODE.
    outreach_send_lint_mode: str = "off"

    # --- prospecting in-stack daily cadence (ADR-014; default dormant) ----
    # ADR-014 (forward-ported to samus 2026-06-23) converges daily prospecting
    # discovery + CRM persistence onto the already-running samus-prospecting
    # container on an in-stack asyncio cadence, mirroring the gateway
    # control-tick loop. When True, the prospecting app lifespan starts a
    # daily loop that composes the active zipcode set from the geo-ring
    # catalog (backend/prospecting/geo_ring.py) and runs discovery with
    # persist_prospects=True, eliminating the divergent-host-runtime seams.
    #
    # DEFAULT OFF (dormant): forward-port lands without behaviour change.
    # Armed via SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED. The live flag is
    # read fresh by the loop via flags.runtime.is_enabled; this field is the
    # boot-time snapshot + single-source default the catalog mirrors. Cadence
    # + first-run-delay tuned by SAMUS_PROSPECTING_CADENCE_INTERVAL_SEC /
    # _INITIAL_DELAY_SEC (read directly by the loop).
    prospecting_in_stack_cadence_enabled: bool = False

    # --- self-supply (starvation-aware input replenishment) ---------------
    # When the idle-drive's campaign portfolio initiates ZERO campaigns
    # because a required input is missing (today's daily call list), the
    # self-supply hook (backend/cash_engine/self_supply.py) fires the owning
    # workcell's producer itself (prospecting /work action=daily_supply)
    # instead of depending on a host scheduled task that can silently skip.
    # Diagnosis/awareness always runs and is reported on the tick record;
    # ONLY the replenish actuation is gated by this flag.
    #
    # DEFAULT OFF (wire-not-arm). Armed via SAMUS_SELF_SUPPLY_ENABLED; the
    # hook reads the live flag through flags.runtime so an operator flip
    # takes effect without a restart. Bounded: 3 replenish attempts per
    # input per business day, then an operator alert artifact is written
    # (cash_engine/supply_alerts/).
    self_supply_enabled: bool = False

    # --- Apollo.io enrichment (last-resort owner-contact lookup) ----------
    # The on-site enrichment cascade covers homepage + /contact + /about +
    # Facebook About. Small-business websites often omit owner contact
    # entirely, so prospects sit with empty owner_email and the cash engine
    # can't reach them. Apollo's people-search by org domain gives us a
    # third-party fallback. Empty key = adapter is a no-op (default state).
    # Paid Apollo plans return unmasked emails; free tier masks them, in
    # which case the adapter returns no signal. Daily $-cap enforced by
    # the SHARED G11 store (backend/common/apollo_budget.py +
    # SAMUS_APOLLO_DAILY_BUDGET_USD, default $5/day) — the same ledger
    # backend.outreach.apollo_source uses, so both Apollo paths reconcile
    # against ONE budget. DPAPI-sealed at the Samus scope; Start-SamusStack.ps1
    # exports as APOLLO_API_KEY.
    apollo_api_key: str = ""

    # --- DocuSeal document signing (self-hosted; backend.contracts) -------
    # Samus generates service-agreement signing links for campaign steps and
    # advances the opportunity on the ``submission.completed`` webhook. Secrets
    # are DPAPI-sealed at the Samus scope; Start-SamusStack.ps1 exports them
    # (DOCUSEAL_API_TOKEN / DOCUSEAL_WEBHOOK_SECRET). Default OFF.
    docuseal_enabled: bool = False
    # In-network API base for the self-hosted instance (Samus -> DocuSeal).
    docuseal_api_base: str = "http://samus-docuseal:3000/api"
    # X-Auth-Token API key (secret).
    docuseal_api_token: str = ""
    # Public base for customer-facing signing links via the Caddy ingress,
    # e.g. https://sign.hustleforge.tech ; links are ``<base>/s/<slug>``.
    docuseal_public_base: str = ""
    # The single service-agreement template id (created once in DocuSeal).
    docuseal_service_agreement_template_id: str = ""
    # Shared secret required on the inbound webhook X-Docuseal-Secret header.
    docuseal_webhook_secret: str = ""
    docuseal_http_timeout_sec: int = 15

    # --- auto-stake sweep (force-multiplier for autonomous outreach) ------
    # The cash_engine fires only on STAKED opportunities. Without this sweep
    # the operator is the only stake-gate (manual review per prospect), so
    # the autonomous pipeline can't expand outreach in parallel with the
    # operator's hand-dialed calls. When True, control_tick runs a sweep
    # every tick that finds prospects matching (lead_score >= threshold,
    # owner_email present, not already in an opportunity, not recently
    # contacted) and auto-stakes them with a generated stake sentence;
    # the cash_engine then walks them through audit -> proposal -> contact
    # -> outreach with the existing CAN-SPAM / suppression / 15-per-day
    # send-ramp guardrails. DEFAULT OFF. Knobs (env-tunable without
    # redeploy): SAMUS_AUTO_STAKE_MIN_LEAD_SCORE (75), MAX_PER_SWEEP (5),
    # RECENT_DAYS (14), SCAN_LIMIT (200).
    auto_stake_enabled: bool = False

    # --- website builder (dormant fulfillment capability) -----------------
    # Samus delivers a customer's website on Wix via a staged, double-gated
    # walk (see backend/website/). Default posture is dormant-but-present, the
    # same shape as cash_engine: the machinery is on (``website_builder_enabled``)
    # while every outward or credentialed step stays fenced.
    #   * ``website_autonomous_enabled`` lifts the per-stage operator-approval
    #     gate so a proven sequence runs unattended. Default OFF -> supervised
    #     walk-through (show -> approve -> advance per stage).
    #   * ``website_live_publish_enabled`` is the second key the irreversible
    #     publish/deliver stages need. Default OFF -> the build runs end-to-end
    #     (provision + populate an UNPUBLISHED site) but never goes live or
    #     contacts the customer.
    #   * ``wix_api_key`` + ``wix_account_id`` are the Wix REST credentials;
    #     absent, no transport is built and every Wix-touching stage parks.
    #   * ``website_content_collection_id`` is the per-template CMS collection
    #     content items are inserted into, confirmed during the walk-through.
    website_builder_enabled: bool = True
    website_autonomous_enabled: bool = False
    website_live_publish_enabled: bool = False
    wix_api_key: str = ""
    wix_account_id: str = ""
    website_content_collection_id: str = ""
    # --- taste-governed content generation (the "frontend codegen") -------
    # When True (default), the `generate` stage produces each page's copy +
    # SEO before the CMS insert: it builds a TasteProfile from the brief and
    # generates on-brand, anti-slop copy via the budgeted Anthropic path
    # (workcell `website`), falling back to a deterministic on-brand template
    # when there is no API key / the $1-day cap is hit / the call fails — so a
    # site can be built on a $0 budget. The copy is taste-audited fail-closed
    # before it is used. False -> legacy behavior (operator supplies page copy).
    # This is internal + reversible (an unpublished site); the outward gates
    # (website_live_publish_enabled) are unchanged. SAMUS_WEBSITE_CONTENT_GENERATION_ENABLED.
    website_content_generation_enabled: bool = True
    # Model for content generation (Control B requires the Haiku family).
    website_content_model: str = "claude-haiku-4-5-20251001"
    # Token ceiling for the single consolidated generation call.
    website_content_max_tokens: int = 1200
    # --- SEO + security enrichment (enforced before delivery) -------------
    # The `seo` stage builds the structured-data (LocalBusiness/Organization
    # JSON-LD) + per-page meta package; the deliver stage runs a live SEO +
    # passive security audit. Posture per gate: off | audit | enforce.
    #   off     — generate the package only; no checks.
    #   audit   — generate + check + record gaps, never block.
    #   enforce — generate + check; a sub-par site PARKS (cannot be delivered)
    #             until it passes. Top-notch is guaranteed before delivery.
    # SAMUS_WEBSITE_SEO_GATE / SAMUS_WEBSITE_SECURITY_GATE.
    # Headless frontend generation (Samus owns the markup; Wix optional backend).
    # When True, the headless site generator (backend/website/site_builder.py) is
    # available to produce a self-contained taste-governed static site. Dormant
    # by default; the artifact is generated on demand, not in the Wix walk.
    website_headless_enabled: bool = False
    # Industry design intelligence (vendored ui-ux-pro-max data): maps the
    # business to an industry-appropriate palette + Google-Fonts pairing + UI
    # style + anti-patterns, applied by the headless generator. Pure local data,
    # zero cost; default ON (strictly improves output, brand colors still win).
    website_design_intelligence_enabled: bool = True
    # Jurisdiction-aware legal sub-pages (Privacy Policy + Terms) generated from
    # the client's registered location. Default ON (sites need them; templates
    # are not legal advice — see backend/website/legal.py compliance_report).
    website_legal_pages_enabled: bool = True
    # Marketing sub-pages (Services/Gallery/FAQ/Reviews/service-area) for a
    # multi-page, agency-grade site. Default ON.
    website_multipage_enabled: bool = True
    # --- headless deploy (Cloudflare Pages) ------------------------------
    # Deploys the generated headless site. Dormant + keyless-safe: nothing
    # deploys without website_deploy_enabled + a token/account/project. The
    # adapter writes a `_headers` file (CSP/HSTS/etc.) so the deployed site
    # reaches security grade A — the headers Wix-subdomain hosting can't set.
    website_deploy_enabled: bool = False
    website_deploy_host: str = "cloudflare"  # cloudflare (others later)
    cloudflare_api_token: str = ""  # CLOUDFLARE_API_TOKEN (Pages:Edit)
    cloudflare_account_id: str = ""  # CLOUDFLARE_ACCOUNT_ID
    cloudflare_pages_project: str = ""  # CLOUDFLARE_PAGES_PROJECT
    website_seo_enrich_enabled: bool = True
    website_seo_gate: str = "enforce"
    website_security_gate: str = "enforce"
    # Live-audit thresholds enforced at the deliver stage.
    website_seo_min_score: int = 70
    website_security_min_grade: str = "B"
    # --- operator-prompted demo sites (cold-call pre-builds) --------------
    # Gates backend/website/demo_sites.py: pre-build + deploy one small demo
    # site per no-website prospect on the morning call list ("I already built
    # you a site"). Default OFF — this is an explicit operator money/outreach
    # decision, fired only via scripts/Build-DemoSites.ps1. No scheduler ever
    # flips it. SAMUS_WEBSITE_DEMO_SITES_ENABLED.
    website_demo_sites_enabled: bool = False
    # HARD total-cost ceiling (USD) for one demo-sites run. LLM copy polish
    # stops (deterministic fallback continues, $0) once the conservative
    # cumulative estimate would cross this. SAMUS_DEMO_SITES_CEILING_USD.
    demo_sites_ceiling_usd: float = 5.0
    # When ON, a callsheet website-ABSENCE finding is re-verified against Google
    # Places before it reaches Morgan (backend.prospecting.callsheet
    # .verified_top_finding), so he never opens with "you have no website" to a
    # business that actually has one (the crawler's bot-blocked false positive).
    # Adds one Places lookup per no-website prospect at callsheet-build time.
    # SAMUS_CALLSHEET_VERIFY_PRESENCE_ENABLED.
    callsheet_verify_presence_enabled: bool = False
    # Phone/address DEEP-VERIFICATION layer (backend.website.deep_verify). When a
    # name search can't tell a business apart from same-named companies, gather
    # candidate domains (Gemini grounded search over the name AND the prospect's
    # phone), FETCH each, and confirm ownership by matching the prospect's phone /
    # street#+ZIP on the page. High precision — only a contact match blocks the
    # build; falls back to Gemini's named site otherwise. Fail-soft. Adds up to a
    # few HTTP fetches + 1-2 Gemini calls per no-website prospect that reaches the
    # web-search stage. SAMUS_PRESENCE_DEEP_VERIFY_ENABLED (default on).
    presence_deep_verify_enabled: bool = True
    # Web-search layer for presence verification. Google Places only knows a site
    # if it's linked in the business's Google Business Profile — many real sites
    # aren't (verified: webtechsolution.org, myfocusmanagement.com,
    # dynamicagservices.com all missed by Places but found by a plain search).
    # When both are set, verify_presence ALSO does a "{name} {city}" web search
    # via a Programmable Search Engine and treats a non-directory result as the
    # business's own site — closing the Places-misses-non-GMB-sites gap.
    # GOOGLE_CSE_API_KEY (a Google key with Custom Search API enabled) +
    # GOOGLE_CSE_ID (the whole-web search engine's cx).
    google_cse_api_key: str = ""
    google_cse_id: str = ""
    # Brand-asset isolation gate (backend/website/client_assets.py) STRICT
    # mode: remote references outside the infrastructure allowlist (fonts +
    # Tailwind CDN + the client's own domain) are STRIPPED from generated
    # sites instead of merely warned about. The gate itself (no cross-client
    # asset reuse, fail-closed) always runs — this flag only hardens the
    # remote-ref posture. SAMUS_WEBSITE_ASSET_ISOLATION_STRICT.
    website_asset_isolation_strict: bool = False

    # --- AI media generation (Gemini image / Veo video) ------------------
    # Headless, taste-governed visual generation for the website deliverable:
    # hero/section imagery + full-motion hero video. PAID (Google-billed) and
    # therefore metered by a SEPARATE budget from the Anthropic $1/day cap (see
    # backend/common/media_budget.py). Dormant by default and keyless-safe: with
    # no gemini_api_key or website_media_gen_enabled off, the generator returns
    # nothing and never spends. Veo video is gated again behind its own flag +
    # per-video operator approval because it is the expensive part.
    gemini_api_key: str = ""  # GEMINI_API_KEY
    # 21st.dev Magic key — design-time component/icon MCP, not a Samus runtime
    # dependency; stored for /show-config visibility + the editing workflow.
    twentyfirst_api_key: str = ""  # TWENTYFIRST_API_KEY
    website_media_gen_enabled: bool = False
    # Image model: gemini-2.5-flash-image (cheap) | gemini-3-pro-image (studio).
    website_media_image_model: str = "gemini-2.5-flash-image"
    website_media_image_size: str = "2K"  # 512 | 1K | 2K | 4K
    # Full-motion video (Veo). Extra-gated: needs this flag AND, per video,
    # operator approval when media_video_requires_approval is True.
    website_media_video_enabled: bool = False
    # Veo 3.1 Fast: good quality at the cheaper per-second tier; muted hero bg so
    # audio (always-on for 3.1) is unused. 720p/8s ~= $1.20. Options:
    # veo-3.1-generate-preview (standard) | veo-3.1-fast-generate-preview |
    # veo-3.1-lite-generate-preview | veo-3.0-generate-001.
    website_media_video_model: str = "veo-3.1-fast-generate-preview"
    website_media_video_resolution: str = "720p"  # 720p | 1080p
    website_media_video_seconds: str = "8"  # 4 | 6 | 8
    media_video_requires_approval: bool = True
    # Paid-media daily USD ceiling across all media generation (fail-closed).
    media_daily_dollar_cap: float = 2.0
    # Rough per-asset cost estimates used for pre-spend budgeting (USD).
    media_image_cost_usd: float = 0.04
    media_video_cost_usd: float = 1.50
    # Representation directive appended to every people-depicting image/video
    # prompt. Default requests an ethnically diverse cast — set/override per
    # brand. Empty string disables it. SAMUS_MEDIA_PEOPLE_DIRECTIVE.
    website_media_people_directive: str = (
        "Feature an ethnically diverse, multi-racial team of varied ages and genders "
        "that authentically represents the local community."
    )

    # --- Social reel generation (MoneyPrinterTurbo-style short video) ------
    # Assembles a narrated, subtitled, vertical (9:16) social reel from a
    # script: edge-tts voiceover (free) + AI footage (Gemini stills by default,
    # Veo motion clips as an approval-gated premium) + optional bg music,
    # composed with MoviePy. PAID footage reuses the SAME media budget as the
    # website generator (media_daily_dollar_cap) — there is no second cap.
    # Dormant + fail-closed: with social_reel_enabled off the pipeline produces
    # nothing and spends nothing; with the heavy deps (edge-tts, moviepy,
    # ffmpeg) absent it degrades to a clean failure result, never an exception.
    social_reel_enabled: bool = False
    # ECO-ADR-0003 §7. When true, the finance snapshot records a "Cone of
    # Plausibility" revenue-futures report (probable burn vs. preferable target
    # vs. the irreversible insolvency tail) to the finance audit ledger.
    # Observation-only — never changes any finance figure or decision.
    # ARMED (default ON) per operator decision 2026-06-08; set
    # SAMUS_FORESIGHT_ENABLED=false to disable.
    foresight_enabled: bool = True
    # edge-tts neural voice id. en-US-AriaNeural is a clear, free default.
    social_reel_tts_voice: str = "en-US-AriaNeural"
    # Footage source: "image" = Gemini stills + Ken-Burns motion (cheap, the
    # cost-sane default ~$0.04/shot) | "video" = Veo motion clips per shot.
    social_reel_footage_mode: str = "image"
    # Veo per-shot motion is extra-gated: needs this flag AND, per reel,
    # operator approval (reuses media_video_requires_approval). A full multi-
    # shot reel of Veo clips can exceed the daily cap, so this stays off.
    social_reel_video_enabled: bool = False
    social_reel_aspect: str = "9:16"  # 9:16 vertical | 16:9 | 1:1
    # Max narration shots per reel — caps both LLM script size and paid footage.
    social_reel_max_segments: int = 5
    # Optional operator-supplied royalty-free music directory. Empty = no music
    # (no copyrighted audio is ever bundled). One random track is ducked under
    # the voiceover when present.
    social_reel_music_dir: str = ""

    # --- Medusa internal commerce (product-sales income stream) -----------
    # Samus talks to an operator-run Medusa instance (Node+Postgres+Redis, MIT)
    # over its Admin REST API to manage products + reconcile orders. Medusa uses
    # its OWN Stripe account = the `products` revenue stream. Dormant + keyless-
    # safe: with the flag off or no base_url/token the client is a no-op.
    commerce_medusa_enabled: bool = False
    medusa_base_url: str = ""  # e.g. https://store.hustleforge.tech
    medusa_admin_token: str = ""  # x-medusa-access-token

    # --- TikTok Shop (research + campaigns now; selling dormant) ----------
    # Trending-product research via a PLUGGABLE provider (the official TikTok
    # Research API forbids commercial use, so we never use it to drive selling).
    # Content posting + the Shop Seller API are dormant + dry-run by default and
    # require operator-approved TikTok apps before they can transact.
    tiktok_research_provider: str = "none"  # none | netrows | echotik | custom
    tiktok_research_api_key: str = ""
    tiktok_research_base_url: str = ""  # for the `custom` provider
    tiktok_content_posting_enabled: bool = False
    tiktok_content_access_token: str = ""
    tiktok_shop_enabled: bool = False  # Seller API (needs approved seller app)
    tiktok_shop_app_key: str = ""
    tiktok_shop_app_secret: str = ""
    tiktok_shop_access_token: str = ""
    tiktok_dry_run: bool = True  # belt+braces: never posts/sells live by default

    # --- n8n workflow generation (workflow-automation deliverables) --------
    # Compiles the scope_planner TaskPlan into a deployable n8n workflow JSON +
    # runbook — the runnable deliverable behind the Workflow Rescue / Buildout /
    # AI Ops SKUs. Generation is pure, offline, and $0, so it defaults ON (like
    # website_multipage). We generate JSON DATA the customer imports into THEIR
    # n8n; we do not vendor/redistribute n8n (fair-code). Live REST deploy + the
    # optional LLM parameter-enrichment pass are dormant by default.
    workflow_n8n_deliverable_enabled: bool = True
    workflow_n8n_llm_enrich: bool = False  # budget-gated param enrichment
    workflow_n8n_deploy_enabled: bool = False  # live POST to an n8n instance
    workflow_n8n_dry_run: bool = True  # deploy dry-run gate (belt+braces)
    n8n_base_url: str = ""  # e.g. https://n8n.example.com
    n8n_api_key: str = ""  # X-N8N-API-KEY

    # --- AWS ---------------------------------------------------------------
    aws_region: str = "us-west-1"
    ddb_task_state_table: str = "samus_task_state"
    ddb_idempotency_table: str = "samus_idempotency"
    ddb_suppression_table: str = "samus_suppression"
    # Email -> prospect/opportunity index (PK=email). Written when the cash
    # engine composes outreach to a prospect; read on an SES bounce/complaint
    # to resolve the address back to a deal and HALT it. Empty disables the
    # index (record/lookup become no-ops) so dev/test without DDB is unaffected.
    ddb_recipient_index_table: str = "samus_recipient_index"
    ddb_feedback_table: str = "samus_feedback_events"
    ddb_onboarding_leads_table: str = "samus_onboarding_leads"
    ddb_llm_budgets_table: str = "samus_llm_budgets"
    # Strategy multi-armed bandit durable store. One row per arm
    # (PK=arm_id); flat ``industry`` and hierarchical ``industry::policy``
    # arms share this single table. Empty string disables DDB and forces the
    # JSON-file fallback (tests + local dev). See backend/strategy/bandit_store.py.
    ddb_strategy_bandit_table: str = "samus_strategy_bandit"
    # --- CRM workcell (samus-crm owns these 7) ----------------------------
    ddb_prospects_table: str = "samus_prospects"
    ddb_contacts_table: str = "samus_contacts"
    ddb_conversations_table: str = "samus_conversations"
    ddb_call_state_table: str = "samus_call-State"
    ddb_opportunities_table: str = "samus_opportunities"
    ddb_operator_tasks_table: str = "samus_operator_tasks"
    # HOTL Tranche 1 approval queue (ADR-0019 severity + TTL semantics).
    ddb_approvals_table: str = "samus_approvals"
    # HOTL Tranche 4 planning layer (goal tree + multi-horizon plans). Both
    # follow the samus_approvals DDB-first + JSON-fallback pattern; an empty
    # value (or unreachable DDB) selects the JSON fallback under the state root.
    ddb_goals_table: str = "samus_goals"
    ddb_plans_table: str = "samus_plans"
    ddb_artifacts_table: str = "samus_artifacts"
    # G8 — needs_warm_path divert queue. Empty value selects the JSON
    # fallback at <SAMUS_ARTIFACT_ROOT>/needs_warm_path.json.
    ddb_needs_warm_path_table: str = ""
    # --- CRM closed-won hard cap (redteam FIN-10) -------------------------
    # Max legitimate deal size (USD). A close (closed_won / closed_won_retainer)
    # with won_amount_usd ABOVE this cap is anomalous — a crafted-large Stripe
    # amount_total or a forged SQS close_opportunity_from_payment message — and
    # is BLOCKED (the opportunity is NOT advanced; it stays in its current
    # state) with a loud anomaly event. This is the hard fail-closed control;
    # the in-process EMA sensor (_check_closed_won_anomaly) remains the soft,
    # advisory signal. Active out of the box at the operator-chosen ceiling.
    # 0 or negative DISABLES the cap (operator who explicitly wants no ceiling).
    # SAMUS_CRM_MAX_CLOSE_AMOUNT_USD.
    crm_max_close_amount_usd: float = 1000.0
    sns_event_topic_arn: str = ""
    ses_from_email: str = ""

    # --- Neo4j (memory workcell) -----------------------------------------
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "samus"
    neo4j_required: bool = False
    # --- knowledge-graph tiering (W-4 — CLOUD_RUN_DEPLOY.md §5.2 / D-6) ----
    # SAMUS_KG_TIER_MODE selects how the private/`hivemind` two-tier split is
    # represented. ``off`` (default) — no ``tier`` property is written and the
    # local Compose stack is unchanged. ``label`` — every ingested knowledge
    # node carries a ``tier`` property (``private`` on ingest, ``hivemind``
    # once promoted) on the single AuraDB ``neo4j`` database. Cloud Run sets
    # ``label``; see backend/memory/tiers.py and graph_client.promote_node.
    kg_tier_mode: str = "off"

    # --- CRM Hivemind projection (CRM Phase-2) ---------------------------
    # When True, CRM opportunity create/advance AND conversation upserts also
    # project the prospect -> contact -> {opportunity, conversation} sub-graph
    # into the local Neo4j knowledge graph via backend.common.graph_client (the
    # same path the memory workcell uses). DEFAULT ON: the projection is a
    # non-destructive, idempotent (MERGE) mirror. Local-default + fail-closed:
    # when Neo4j is unreachable (neo4j_required=False) every projection write is
    # a clean no-op, so a graph outage can never fail or block a CRM mutation —
    # the authoritative store stays the DDB row, and installs without Neo4j are
    # unaffected. Set SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED=false to disable.
    # Promotion of projected nodes to the exportable ``hivemind`` tier is a
    # SEPARATE opt-in governed by SAMUS_KG_TIER_MODE=label (off by default), so
    # default-on projection stays private-tier until explicitly promoted.
    crm_hivemind_projection_enabled: bool = True

    # --- ChromaDB vector layer (memory workcell, Phase-3 S8) --------------
    # Bounded semantic-retrieval store wrapping a ChromaDB collection
    # (backend/memory/vector_store.py). Default OFF: the layer is a no-op
    # (upsert=skipped, query=empty) and never imports chromadb. When armed it
    # fails closed if chromadb is absent or the backend is unreachable, so a
    # vector outage can't take down a consulting dispatch path. chromadb is an
    # OPTIONAL dependency (lazy-imported); add it to requirements.txt before
    # enabling. SAMUS_VECTOR_STORE_ENABLED.
    vector_store_enabled: bool = False
    # Collection name; mirrors the recovery seeder's COLLECTION_NAME.
    # SAMUS_VECTOR_STORE_COLLECTION.
    vector_store_collection: str = "samus_knowledge"
    # On-disk persistent-client path (used when host/port are unset).
    # SAMUS_VECTOR_STORE_PATH.
    vector_store_path: str = "/opt/samus/data/vectorstore"
    # Optional chromadb HTTP server. When BOTH host and port are set the HTTP
    # client is used instead of the persistent on-disk client.
    # SAMUS_VECTOR_STORE_HOST / SAMUS_VECTOR_STORE_PORT.
    vector_store_host: str = ""
    vector_store_port: int = 0

    # --- workcell-specific external keys ----------------------------------
    google_places_api_key: str = ""
    anthropic_api_key: str = ""  # Unused — LM Studio backend needs no auth
    anthropic_admin_api_key: str = ""  # Admin key for /v1/organizations/cost_report
    # EFH semantic evaluator — when ON, the Ethical Failure Handler augments its
    # deterministic keyword heuristic with an LLM semantic classifier that can
    # only ADD axiom breaches (never remove one), so an LLM outage or a
    # misconfigured key leaves the fail-closed heuristic floor fully intact.
    # Default OFF: the heuristic alone is the load-bearing gate until the
    # operator arms this via SAMUS_EFH_SEMANTIC=1.
    samus_efh_semantic_enabled: bool = True
    lm_studio_base_url: str = ""
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""
    # --- Revenue-stream partition (separate income sources) ---------------
    # Each earning stream (services / products / tiktok_shop) can use its OWN
    # Stripe account so the books separate cleanly. Empty keys POOL under the
    # default account (stripe_api_key) — so today's single-account behavior is
    # unchanged until an operator arms a stream with its own key. "One LLC now,
    # split later": set a stream's key + entity to promote it to its own legal
    # entity with NO code change. See backend/finance/revenue_streams.py.
    stripe_api_key_products: str = ""  # STRIPE_API_KEY_PRODUCTS
    stripe_api_key_tiktok: str = ""  # STRIPE_API_KEY_TIKTOK
    stripe_webhook_secret_products: str = ""
    stripe_webhook_secret_tiktok: str = ""
    revenue_entity_default: str = "HustleForge LLC"
    revenue_entity_products: str = ""  # override -> promotes products to its own entity
    revenue_entity_tiktok: str = ""  # override -> promotes tiktok to its own entity
    vapi_api_key: str = ""
    vapi_webhook_secret: str = ""
    # Vapi outbound dialer requires the operator's assistant + phone number IDs
    # (UUIDs surfaced in the Vapi dashboard). Not credentials per se, but
    # account-binding so we store them in DPAPI alongside the API key rather
    # than .env. Empty -> morning dialer refuses to run (no silent fallback).
    vapi_assistant_id: str = ""
    vapi_phone_number_id: str = ""
    # Callsheet placeholder substitution for the voice path. The prospecting
    # callsheet templates carry human-rep placeholders ``[NAME]`` (the caller)
    # and ``[PHONE]`` (the callback number) — fine on a printed callsheet, but
    # fed verbatim to Vapi the assistant reads them literally ("this is name...
    # call me at phone"). The dialer substitutes these before injecting the
    # opener/voicemail as Vapi variables. SAMUS_VOICE_REP_NAME /
    # SAMUS_VOICE_CALLBACK_NUMBER.
    samus_voice_rep_name: str = "Morgan"
    samus_voice_callback_number: str = "(530) 555-0105"
    # Prior-call context injection (default OFF). When True, the dialer calls
    # backend.voice.prospect_profile.build_prospect_context(prospect_id) and
    # merges {prior_outcome, prior_notes, prior_contact_summary} into Vapi
    # variableValues so Morgan dials INFORMED by the operator's logged outcome +
    # notes (read from the HF_DATA_DIR/call_outcomes/ host journal — latest
    # entry wins, fail-soft to empties). SAMUS_VOICE_PROFILE_CONTEXT.
    samus_voice_profile_context_enabled: bool = False
    # Gate the outreach service's call/voicemail send path through the real Vapi
    # provider (via the voice workcell). Default OFF — without this an outbound
    # dial never fires from backend.outreach.send_message; it returns a
    # structured degraded receipt instead. Arm only once the assistant/phone +
    # API key are provisioned.
    outreach_voice_send_enabled: bool = False
    # AI Digital Receptionist — the INBOUND assistant + its bound DID. A
    # separate Vapi assistant (receptionist persona, recording enabled) from
    # the outbound sales dialer above. Empty -> the inbound receptionist
    # path is disabled (the workcell still serves the outbound surface).
    vapi_inbound_assistant_id: str = ""
    vapi_inbound_phone_number_id: str = ""
    # Own-Twilio credentials — used ONLY by the operator import path
    # (backend/voice/provision.py import-twilio) to register numbers you own
    # in your own Twilio account as Vapi twilio-provider DIDs. Vapi will not
    # port out numbers bought inside Vapi, so the migration is buy-in-Twilio →
    # import → cut over. Credentials, so seeded via DPAPI (TwilioAccountSid /
    # TwilioAuthToken, Scope=Samus) and exported as env by the launcher.
    # Empty -> the Twilio import path is disabled (fail-closed); the existing
    # Vapi-managed voice surface is unaffected.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    # Scoped, revocable Twilio API key (SK...) + its secret — preferred over
    # the account-wide auth token for the import path. When both are set they
    # take precedence over twilio_auth_token. Empty -> auth-token path is used.
    twilio_api_key: str = ""
    twilio_api_secret: str = ""

    # --- ngrok embedded tunnel (voice workcell webhook ingress) -----------
    # When NGROK_AUTHTOKEN is set, samus-voice opens its own tunnel at
    # startup and auto-PATCHes the Vapi assistant's serverUrl with the
    # freshly-issued URL. Empty -> tunnel skipped (degraded mode); workcell
    # still serves /vapi/webhook but it's only reachable through the
    # docker network (i.e. tests / sibling workcells).
    # NGROK_RESERVED_DOMAIN pins a reserved domain (paid ngrok plan); when
    # set, the URL is stable across restarts and the Vapi PATCH is still
    # idempotent. Empty -> ngrok assigns a random *.ngrok-free.app URL.
    ngrok_authtoken: str = ""
    ngrok_reserved_domain: str = ""

    # --- email backend (selector + per-backend config) --------------------
    # Architecture: adapter selector in common/email_backend.py dispatches to
    # one of common/email_backends/{sendgrid,ses}.py. SES is not yet lifted
    # into common/ (still in outreach/ses_adapter.py); selecting "ses" via
    # the common adapter raises NotImplementedError until that lift happens.
    email_backend: str = "sendgrid"  # "sendgrid" | "ses"
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = ""
    sendgrid_from_name: str = "HustleForge"
    # Reply-To header. Empty = no header set; mail clients fall back to From,
    # which on this account (ahartman@hustleforge.tech) is forwarded to
    # samushustleforge@gmail.com (the Samus billing-monitor inbox). Override
    # to a different address if/when the forwarding chain changes.
    sendgrid_reply_to: str = ""
    # EU SendGrid subusers must override to https://api.eu.sendgrid.com
    sendgrid_base_url: str = "https://api.sendgrid.com"

    # --- Stripe webhook -> auto-fulfill ----------------------------------
    # Whitelist of hf_offer_code values that trigger automatic fulfillment
    # when the customer also supplies a website URL via Stripe checkout
    # custom_fields. Comma-separated env-var SAMUS_AUTO_FULFILL_OFFERS.
    # Empty list = operator-only (manual Run-Fulfill.ps1 for every sale).
    auto_fulfill_offer_codes: list[str] = Field(default_factory=list)

    # --- intake workcell (public onboarding form receiver) ----------------
    # Comma-separated env-var SAMUS_INTAKE_ALLOWED_ORIGINS controls CORS for
    # /intake/onboarding. Default covers the production marketing site; add
    # staging or www variants there. An empty list locks the endpoint to
    # same-origin only (cross-site browser POSTs fail preflight).
    intake_allowed_origins: list[str] = Field(
        default_factory=lambda: ["https://hustleforge.tech"],
    )
    # Hard cap on the intake dedup window. Identical lead (same email +
    # website_url + first 200 chars of pain_points) submitted again inside
    # this window is short-circuited with status=duplicate and skipped at
    # the DDB write. Configurable for tests; SAMUS_INTAKE_DEDUP_WINDOW_S.
    intake_dedup_window_seconds: int = 86400
    # Operator inbox that receives a structured email copy of every accepted
    # onboarding lead. Belt-and-suspenders for the "never miss" requirement:
    # if DDB is down or the workcell otherwise can't persist, the email is
    # still the durable record. Empty string disables the mirror (default
    # for dev/test). SAMUS_INTAKE_OPERATOR_EMAIL.
    intake_operator_email: str = ""

    # --- inbound email (Gmail API poller) ---------------------------------
    # Samus reads its company inbox via the Gmail REST API (OAuth 2.0
    # over TCP/443) — the IMAP/993 path was blocked by an upstream firewall
    # on the host-dev network, and the API is the long-term-correct
    # architecture anyway (rate-limited by Google, supports labels, no
    # app-password / 2FA dance). The outbound From is ahartman@hustleforge.tech
    # which forwards to the Samus inbox per the email_backend Reply-To note,
    # so customer *replies* land in this mailbox and the poller turns them
    # into CRM artifacts + operator tasks.
    # Why pure-httpx over google-api-python-client: SDK adds ~10MB to the
    # container and the API surface we need is three endpoints (list,
    # get-raw, modify). Mirrors the StripeClient pattern in finance/.
    # OAuth scope: https://www.googleapis.com/auth/gmail.modify
    # (read + mark as read; no send, no delete).
    # Empty inbox_email OR empty client credentials disable the poller
    # (drain function returns early).
    gmail_inbox_email: str = ""  # SAMUS_GMAIL_INBOX_EMAIL
    gmail_oauth_client_id: str = ""  # SAMUS_GMAIL_OAUTH_CLIENT_ID
    gmail_oauth_client_secret: str = ""  # SAMUS_GMAIL_OAUTH_CLIENT_SECRET
    # Path to the JSON file persisting the refresh + access tokens. Written
    # by the one-time consent flow (backend.intake.gmail_oauth) and read by
    # every drain pass. SAMUS_GMAIL_OAUTH_TOKEN_PATH overrides.
    gmail_oauth_token_path: str = "/opt/samus/data/intake/gmail_oauth_token.json"
    # Hard cap on messages processed per drain pass. Excess messages stay
    # UNREAD for the next pass. SAMUS_GMAIL_INBOX_MAX_PER_PASS.
    gmail_inbox_max_per_pass: int = 25
    # JSONL ledger of processed Message-IDs (idempotency). Default lives
    # alongside the other Samus on-disk ledgers under /opt/samus/data.
    # SAMUS_GMAIL_INBOX_LEDGER overrides.
    gmail_inbox_ledger_path: str = "/opt/samus/data/intake/inbound_email.jsonl"

    # --- per-workcell LLM token budgets (Anthropic spend control) ----------
    # See backend/common/llm_budget.py for the full design. Daily base quota
    # in tokens; scaled by an EMA-smoothed efficiency factor:
    #   factor = 0.5 + 1.5 * ema   (0.0 -> 0.5x, 1.0 -> 2.0x)
    #   quota  = max(base * floor_pct, base * factor)
    # Override per workcell via env (SAMUS_LLM_BASE_TOKENS).
    llm_workcell_base_token_budget: int = 100_000
    # EMA smoothing constant. 0.05 -> ~60 calls to fully respond to a step
    # change in success rate. Lower = slower / more stable; higher = more
    # responsive to recent calls.
    llm_budget_ema_alpha: float = 0.05
    # Hard floor on adaptive quota. A workcell stuck in a failure loop can
    # still spend up to ``base * floor_pct`` per day to recover.
    llm_budget_floor_pct: float = 0.10

    # --- LLM cost-control layer (token-cost-hardening 2026-05-18) ----------
    # Control A: global cross-workcell $-cap. Enforced in
    # backend/common/llm_global_budget.py BEFORE the per-workcell token
    # quota. Default-deny on cap breach; allow-on-persistence-failure
    # mirrors the per-workcell store.
    #
    # NOTE: The 29ba2df hardening commit originally defaulted this to
    # $25/day as a defense-in-depth ceiling. Per the operator-chosen
    # production policy (memory: project_samus_llm_token_policy, the
    # $1/day cap rule), Samus runs with a hard $1/day cap across all
    # workcells. This is intentionally tighter than the 29ba2df default
    # — Lever 1.1 of the LLM token-min plan.
    llm_global_daily_dollar_cap: float = 1.0
    # Dynamic cap scaler: when SAMUS_LLM_DYNAMIC_CAP=true (auto-enabled on
    # OpenAI backend), the daily cap tracks MRR via llm_budget_scaler.
    # These are the knobs; env vars override (see llm_budget_scaler.py).
    llm_reinvest_pct: float = 0.05  # fraction of MRR allocated to LLM
    llm_daily_floor_usd: float = 0.50  # minimum daily cap regardless of MRR
    llm_daily_ceiling_usd: float = 25.0  # hard max daily cap
    # Control B: model floor regex. Any model id that doesn't match this
    # pattern raises ModelNotPermitted unless the caller explicitly passes
    # allow_expensive_model=True. Default = Haiku family only.
    llm_cheap_model_pattern: str = r"^claude-haiku-"
    # Control C: circuit breaker on consecutive LLM-call errors. After
    # this many errors in a row for one workcell, can_spend denies for
    # the cooldown window. Reset by any single success/failure outcome.
    llm_circuit_breaker_threshold: int = 10
    llm_circuit_breaker_cooldown_sec: int = 300

    # --- portfolio trigger layer (Lever 3.2; backend/strategy/triggers.py) -
    # Portfolio re-plan is event-driven (5 signal-change triggers walked by
    # ``check_and_fire``); each fire = one ``propose_allocation()`` LLM call
    # routed through the workcell budget gate. Expected cadence 5-15 fires/day.
    # ``portfolio_workcell_dollar_cap`` is the slice of the per-day $1 hard
    # cap allocated to the strategy workcell (informational; the live cap
    # is enforced by llm_budget). ``portfolio_min_signal_change`` is the
    # pipeline-EV step threshold (fraction; 0.15 -> 15%).
    # ``portfolio_tick_interval_sec`` is the tick cadence used by
    # ``start_tick_loop`` (15 min default).
    portfolio_workcell_dollar_cap: float = 0.20
    portfolio_min_signal_change: float = 0.15
    portfolio_tick_interval_sec: int = 900

    # --- SEO passive security & trust-posture audit -----------------------
    # When True (the default), backend.seo.audit.audit_url runs the passive
    # security audit (backend/seo/security_audit.py) and the customer-facing
    # report gains a "Security & Trust Posture" section. The audit is strictly
    # passive (benign GETs + one TLS handshake + DNS) and zero-LLM. When False
    # the audit is skipped entirely and the report section is omitted.
    # Env var SAMUS_SEO_SECURITY_AUDIT_ENABLED.
    seo_security_audit_enabled: bool = True

    # --- SEO durable input_hash result cache (WIRED-DORMANT) --------------
    # When True, backend.seo.audit_cache short-circuits a repeat audit whose
    # request payload hashes to an input_hash already seen within the TTL
    # window (default 24h; SAMUS_AUDIT_CACHE_TTL_SECONDS), returning the prior
    # result and skipping the fetch/audit recompute + ledger + CRM dispatch.
    # This closes the Cloud-Run cold-cache duplication where the same site is
    # audited N times/day under one input_hash. DEFAULT FALSE: with the flag
    # off nothing is cached or read and audit_site behaves exactly as before —
    # an operator flips it to activate. Reversible. Sidecar path defaults to a
    # sibling of SAMUS_SEO_AUDIT_PATH (override: SAMUS_SEO_AUDIT_CACHE_PATH).
    # Env var SAMUS_AUDIT_CACHE_ENABLED.
    seo_audit_cache_enabled: bool = False

    # ======================================================================
    # --- intake-hardening section (feat/samus-intake-hardening) -----------
    # ======================================================================
    # Self-contained block of settings for the public-ingestion hardening
    # work: per-IP rate limiting, optional CAPTCHA, trusted-proxy XFF hop
    # count, and the production webhook live-mode gate. Kept delimited so a
    # union merge with a sibling feature branch stays trivial.
    #
    # --- intake rate limiting (DynamoDB-backed, Cloud Run safe) -----------
    # Per-source-IP fixed-window limiter on POST /intake/onboarding. Backed
    # by the existing samus_idempotency DynamoDB table so the counters are
    # shared across Cloud Run instances (a pure in-process dict would only
    # see one instance's traffic). The limiter fails OPEN — a DynamoDB
    # hiccup must never block a legitimate lead. Counts are stored as TTL'd
    # rows so DynamoDB reaps them automatically.
    intake_rate_limit_per_minute: int = 5
    intake_rate_limit_per_hour: int = 30
    # Global hourly ceiling across ALL source IPs — a backstop against a
    # distributed flood that stays under the per-IP cap. 0 disables the
    # global ceiling (per-IP limits still apply).
    intake_rate_limit_global_per_hour: int = 600
    # When False (test default), the rate limiter is bypassed entirely so
    # the existing endpoint tests don't have to reason about counters.
    # bootstrap_settings() defaults this True for real deployments.
    intake_rate_limit_enabled: bool = False
    # DynamoDB TTL grace (seconds) added to each window when writing the
    # `expires_at` attribute so a counter row outlives its window slightly
    # before DynamoDB reaps it.
    intake_rate_limit_ttl_grace_seconds: int = 120

    # --- intake CAPTCHA (Cloudflare Turnstile) ----------------------------
    # When set, POST /intake/onboarding requires a `captcha_token` field and
    # server-side-verifies it against the Turnstile siteverify API; a
    # missing or invalid token is rejected with HTTP 400. When empty (the
    # default), CAPTCHA is skipped entirely — the feature is complete but
    # dormant until the operator seals a secret. CAPTCHA verification fails
    # CLOSED: when a secret is configured and verification cannot complete,
    # the request is rejected. SAMUS_INTAKE_CAPTCHA_SECRET.
    intake_captcha_secret: str = ""
    # Turnstile siteverify endpoint. Overridable for tests / self-hosted
    # verification proxies. SAMUS_INTAKE_CAPTCHA_VERIFY_URL.
    intake_captcha_verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    # --- trusted-proxy X-Forwarded-For hop count --------------------------
    # Number of trusted proxy hops that append to X-Forwarded-For in front
    # of the workcell. Cloud Run and a single Caddy reverse proxy each
    # append exactly one hop, so the real client IP is the entry that many
    # positions from the RIGHT of the XFF list. The leftmost entries are
    # fully attacker-controlled and must never be trusted. When XFF has
    # fewer entries than this count, the limiter falls back to the direct
    # socket peer (request.client.host). SAMUS_INTAKE_TRUSTED_PROXY_HOPS.
    intake_trusted_proxy_hops: int = 1

    # --- Stripe webhook production live-mode gate -------------------------
    # When True AND the runtime env is a production env, the Stripe webhook
    # rejects events with livemode=false (a test-mode event must not
    # advance a customer to "paid" or send a receipt in production). In
    # non-production env the gate is inert so the test suite's
    # livemode=False fixtures still process. SAMUS_STRIPE_REJECT_TEST_MODE.
    stripe_reject_test_mode_in_production: bool = True

    # --- Cross-agent Quorum Hub (MCP JSON-RPC 2.0 over HTTP) --------------
    # The shared hub at _shared/quorum_hub runs on the host at 127.0.0.1:8090
    # and exposes tools: governance_publish, governance_log, governance_stats.
    # Samus publishes EFH vetoes + PDC BLOCK/ESCALATE findings as cross-agent
    # advisory events; the hub is informational, never blocking.
    # Dormant by default — flip SAMUS_QUORUM_PUBLISH_ENABLED=1 to activate.
    samus_quorum_publish_enabled: bool = False
    samus_quorum_hub_url: str = "http://host.docker.internal:8090"
    samus_quorum_hub_hmac_key: str = ""  # hex; empty == dev mode (no hub-side HMAC)
    # Cross-agent Quorum VOTE protocol (2026-06-01 rework). When True, Samus's
    # POST /quorum/vote VOTER endpoint accepts HMAC-signed AgentEnvelope
    # proposals FROM the collector (Major), runs the 3-axis policy reasoning
    # (backend/standard/inter_agent/quorum_voter.py), and returns a ballot.
    # Default False — the endpoint is coded in place but DORMANT: while unset
    # the route returns 503 before doing any work. Second dormancy layer:
    # Samus is intentionally NOT in the operator-signed quorum roster, so Major
    # will not broadcast to it until the operator adds it. The live flag is
    # read fresh by quorum_vote_route.is_voting_enabled(); this field is the
    # boot-time snapshot for /show-config. SAMUS_QUORUM_VOTING_ENABLED.
    samus_quorum_voting_enabled: bool = False
    # PDC composite observer: when True, commit_commercial_action runs the
    # PDC composite (EFH + confusion_meter + elegance_scorer + adversarial
    # classifier) as a parallel observer after EFH passes. Findings publish
    # to the quorum hub on BLOCK/ESCALATE. Default False — observer is
    # informational, never refuses the commit; EFH stays the only gate.
    samus_pdc_observe_enabled: bool = False

    # --- design-taste deliverable-quality subsystem (backend/taste) -------
    # Anti-slop frontend/copy governance for client-facing deliverables
    # (scaffold proposals, brand copy, and — later — generated frontend code).
    # Two halves: a deterministic Pre-Flight audit (em-dash ban, eyebrow
    # restraint, banned palette/font families, AI-tell scan) and a dial /
    # design-system profile resolver. The audit is pure-stdlib + zero-LLM so
    # it respects the $1/day global cap. All three flags default OFF — nothing
    # in the deliverable path or the PDC composite changes until armed.
    #
    # When True, deliverable producers (scaffold, …) run audit_deliverable()
    # over the rendered artifact and attach the result under
    # payload["taste_audit"]. SAMUS_TASTE_AUDIT_ENABLED.
    taste_audit_enabled: bool = False
    # When True AND a taste artifact is supplied, run_pdc folds the taste
    # verdict into the composite as a soft (REVIEW) signal, mirroring the
    # elegance floor. Never BLOCKs on taste alone. SAMUS_TASTE_PDC_GATE_ENABLED.
    taste_pdc_gate_enabled: bool = False
    # When True, a hard-fail taste violation (e.g. an em-dash) is allowed to
    # mark the deliverable taste verdict as failing so the producer can refuse
    # to ship. Default False = warn-only (record but never block).
    # SAMUS_TASTE_AUDIT_BLOCK_ON_FAIL.
    taste_audit_block_on_fail: bool = False
    # PDC taste review floor: a passing audit below this score still trips a
    # soft REVIEW signal when taste_pdc_gate_enabled. SAMUS_TASTE_REVIEW_FLOOR.
    taste_review_floor: float = 0.6

    # --- v0.5/v0.6 Directed Capability Protocol layer ---------------------
    # ISV consumer: pulls Major-signed InitialStateVectors from Optimus
    # dispatch each session; gates commercial actions to ISV scope.
    samus_isv_consumer_enabled: bool = True
    # Dual-channel mirror: every commercial action must produce a
    # synchronously-acked ActionTakenEnvelope to Major via Optimus before
    # the commercial commit lands.
    samus_dual_channel_enabled: bool = True
    samus_dual_channel_audit_timeout_sec: int = 180
    # Template registry: pre-approved action templates that bypass EFH
    # per-instance evaluation for Samus's three signed templates.
    samus_template_registry_enabled: bool = True
    # Path to Major's Ed25519 public key (used to verify ISV envelopes).
    # Empty -> ISV verification is permissive in development; refuses in
    # production. SAMUS_MAJOR_PUBLIC_KEY_PATH.
    samus_major_public_key_path: str = ""
    # Samus's own Ed25519 signing key path (for ActionTakenEnvelope sigs).
    # Empty -> dual-channel mirror runs in "stub-sign" mode in dev only.
    samus_signing_key_path: str = ""
    # Optimus dispatch base URL — where to poll for inbound ISVs and post
    # ActionTakenEnvelopes. Empty -> falls back to local-disk queue at
    # Samus/state/dispatch_inbox/.
    samus_optimus_dispatch_url: str = ""
    # Major RBL status probe URL. Empty -> read from local cache file.
    samus_major_rbl_status_url: str = ""

    # --- meta-governance L1 resource broker client (B.4) ------------------
    # SAMUS-side client of the broker that runs on Anita
    # (feat/anita-l1-broker). Every Anthropic call through
    # backend/common/llm_client.anthropic_messages() now passes through
    # broker.reserve() AFTER the local budget gates and BEFORE the HTTP
    # call, then broker.release() on completion (success or failure).
    # The broker is fail-closed: any network/server failure is treated
    # as deny, mapped into BudgetExceeded at the boundary.
    samus_broker_enabled: bool = True
    # Broker base URL — Anita's operator-console port (HTTPS, self-signed
    # on loopback; broker_client._post_signed turns verify off only for
    # 127.0.0.1 / localhost).
    samus_broker_base_url: str = "https://127.0.0.1:8420"
    # Reserve timeout — short cap because reserve is on the LLM hot path.
    samus_broker_reserve_timeout_sec: float = 1.5
    # Release timeout — slightly looser, release is best-effort and the
    # broker auto-reclaims at TTL anyway.
    samus_broker_release_timeout_sec: float = 1.5
    # When True (default) AND the inter-agent HMAC key (SS_HMAC_KEY_SAMUS
    # / SAMUS_AGENT_HMAC_SECRET) is NOT configured, the broker client
    # treats itself as disabled at call time so pytest stays green out
    # of the box. Operators in production set the key (and this flag is
    # then a no-op because the key check passes).
    samus_broker_disable_in_dev_when_no_key: bool = True
    # Optional JSON map of workcell-name → broker priority overrides
    # (lower number = higher priority). Empty string means "use built-in
    # workcell-class table"; see broker_client._BROKER_PRIORITY_BY_WORKCELL_CLASS.
    samus_broker_priority_json: str = ""

    # --- cross-agent operator-relief forwarder (inter_agent.relief) -------
    # When enabled, Samus mirrors its OWN stale operator-pending deals (CRM
    # opportunities awaiting a Stake Sentence) to Anita's POST
    # /api/relief/intake so they reach the Agora for tribal deliberation while
    # the operator is away. Resolves nothing (Axiom A); Anita applies the
    # authoritative staleness/away/dedup gate. Reuses samus_broker_base_url
    # (= Anita) for the egress. Dormant by default — needs this flag AND
    # Anita's sn_agora_relief_intake_enabled.
    samus_agora_relief_forward_enabled: bool = False
    # Coarse local staleness before a deal is mirrored (Anita re-gates). 6h.
    samus_agora_relief_forward_staleness_sec: int = 21600
    # Max deals mirrored per tick (back-pressure).
    samus_agora_relief_forward_max_per_run: int = 5
    # Forwarder tick cadence. 30 min.
    samus_agora_relief_forward_interval_sec: int = 1800

    def hmac_keys_summary(self) -> dict[str, str]:
        """Non-secret summary of which services have a dedicated HMAC key.

        Returns ``{service: "configured"}`` for every service with a
        per-service key and never exposes the key material itself. Safe to
        surface in /show-config / boot logs.
        """
        return {svc: "configured" for svc in sorted(self.per_service_hmac_keys)}

    @property
    def is_production(self) -> bool:
        """True when the runtime env is a production-class environment.

        Used by the Stripe webhook live-mode gate. ``env`` is sourced from
        SAMUS_ENV; "production" and "prod" both count.
        """
        return (self.env or "").strip().lower() in ("production", "prod")

    @property
    def sqs_enabled(self) -> bool:
        return any(v for v in self.sqs_queue_urls.values())

    def require_env(self, *names: str) -> None:
        """Raise RuntimeError if any named attribute is empty / unset."""
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            raise RuntimeError(f"required settings missing: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    from .settings import bootstrap_settings

    return bootstrap_settings()


def reload_settings() -> Settings:
    """Drop the LRU cache and re-bootstrap. Used by tests + rotation tooling."""
    get_settings.cache_clear()
    return get_settings()
