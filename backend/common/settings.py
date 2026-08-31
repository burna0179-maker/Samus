"""``bootstrap_settings()`` — .env loader + Settings constructor per doc §3.23.

Reads ``.env``, ``.env.local``, ``.env.prod`` from the Samus project root, then
maps env vars to typed ``Settings`` fields. The cached singleton lives in
``config.get_settings()``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import Settings


def _env(key: str, default: Any = None) -> str | None:
    val = os.getenv(key, default)
    if val is None:
        return None
    val = str(val).strip()
    return val or None


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "y")


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _strategy_bandit_table() -> str:
    """Resolve ``DDB_STRATEGY_BANDIT_TABLE`` preserving an explicit empty value.

    Most ``ddb_*_table`` fields coalesce an unset/empty env var to their
    default. The strategy bandit table is different: an explicit
    ``DDB_STRATEGY_BANDIT_TABLE=""`` is the meaningful "disable DDB, use the
    JSON fallback" signal the pytest suite (conftest) sets. So we distinguish
    *unset* (-> default ``samus_strategy_bandit``) from *set-to-empty*
    (-> ``""``, DDB disabled). Mirrors the conftest treatment of
    ``DDB_LLM_BUDGETS_TABLE``.
    """
    raw = os.getenv("DDB_STRATEGY_BANDIT_TABLE")
    if raw is None:
        return "samus_strategy_bandit"
    return raw.strip()


def _parse_pairs(raw: str | None) -> dict[str, str]:
    """Parse 'a=b,c=d' env values into a dict."""
    out: dict[str, str] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _load_dotenv_files() -> None:
    """Load .env / .env.local / .env.prod from the Samus root, last-wins."""
    here = Path(__file__).resolve().parent.parent.parent
    for name in (".env", ".env.local", ".env.prod"):
        path = here / name
        if path.exists():
            load_dotenv(dotenv_path=path, override=False, encoding="utf-8")


# Canonical workcell roster. Used to scan for per-service HMAC keys
# (``SAMUS_HMAC_KEY_<SERVICE>``) and kept in sync with the gateway_urls
# iteration list below + capabilities.SERVICE_CAPABILITIES.
_KNOWN_SERVICES: tuple[str, ...] = (
    "gateway",
    "leadgen",
    "prospecting",
    "scaffold",
    "fulfillment",
    "memory",
    "feedback",
    "outreach",
    "optimizer",
    "proposal",
    "seo",
    "finance",
    "voice",
    "intake",
    "crm",
    "strategy",
    "signal_filter",
    "path_optimizer",
    "template_recovery",
    "portfolio_controller",
    "entropy",
    "retainer",
)


def _collect_per_service_hmac_keys() -> dict[str, str]:
    """Read every configured ``SAMUS_HMAC_KEY_<SERVICE>`` into a map.

    R-1: each workcell MAY have a dedicated HMAC key; absent one it falls
    back to the shared key. This map is for /show-config visibility — the
    authoritative live resolver is ``security.resolve_service_key``. Only
    services with a non-empty key appear in the map.
    """
    from .security import per_service_key_env_name  # local import: avoid cycle

    keys: dict[str, str] = {}
    for service in _KNOWN_SERVICES:
        val = _env(per_service_key_env_name(service))
        if val:
            keys[service] = val
    return keys


def _resolve_env() -> str:
    """Resolve SAMUS_ENV with a loud warning when unset in a production context.

    Silently defaulting to ``"development"`` has been a recurring source of
    security misconfigurations — fail-closed guards throughout the stack check
    ``settings.env != "development"`` and are silently bypassed when this
    defaults. Warn once at settings-load time so the operator sees it in
    the startup logs.
    """
    import logging as _logging

    raw = (_env("SAMUS_ENV", "") or "").strip()
    if not raw:
        _logging.getLogger("samus.settings").warning(
            "SAMUS_ENV is not set — defaulting to 'development'. "
            "Set SAMUS_ENV=production before any production deploy; "
            "many security fail-closed checks are bypassed in development mode."
        )
        return "development"
    return raw


def _resolve_authz_mode() -> str:
    """Resolve ``SAMUS_AUTHZ_MODE`` to one of off|audit|enforce.

    An unset or unrecognised value resolves to ``enforce`` — the caller-authz
    matrix is ARMED by default (HOTL Tranche 1 operator decision 2026-07-05).
    Set SAMUS_AUTHZ_MODE=off|audit explicitly to stand it down.
    """
    raw = (_env("SAMUS_AUTHZ_MODE", "enforce") or "enforce").lower()
    return raw if raw in ("off", "audit", "enforce") else "enforce"


def _resolve_kg_tier_mode() -> str:
    """Resolve ``SAMUS_KG_TIER_MODE`` to ``off`` or ``label`` (W-4).

    Unset or unrecognised resolves to ``off`` — the knowledge-graph tier
    convention stays dormant and no ``tier`` property is written, so the
    local Compose stack is unchanged. Cloud Run sets ``label`` (see
    CLOUD_RUN_DEPLOY.md §3 / D-6 option 6b).
    """
    raw = (_env("SAMUS_KG_TIER_MODE", "off") or "off").lower()
    return raw if raw in ("off", "label") else "off"


def bootstrap_settings() -> Settings:
    """Read env into a ``Settings`` instance."""
    _load_dotenv_files()

    gateway_urls = _parse_pairs(_env("SAMUS_GATEWAY_URLS"))
    for service in (
        "gateway",
        "leadgen",
        "prospecting",
        "scaffold",
        "fulfillment",
        "memory",
        "feedback",
        "outreach",
        "optimizer",
        "proposal",
        "seo",
        "finance",
        "voice",
        "intake",
        "crm",
        "strategy",
        "campaigns",
        "signal_filter",
        "path_optimizer",
        "template_recovery",
        "portfolio_controller",
        "entropy",
    ):
        env_key = f"{service.upper()}_URL"
        val = _env(env_key)
        if val:
            gateway_urls.setdefault(service, val)

    sqs_queue_urls: dict[str, str] = {}
    for service in (
        "leadgen",
        "prospecting",
        "scaffold",
        "fulfillment",
        "feedback",
        "outreach",
        "optimizer",
        "proposal",
        "seo",
        "cash_engine",
    ):
        val = _env(f"SQS_{service.upper()}_QUEUE_URL")
        if val:
            sqs_queue_urls[service] = val

    return Settings(
        service_name=_env("SAMUS_SERVICE", "unknown") or "unknown",
        env=_resolve_env(),
        port=_env_int("SAMUS_PORT", 8000),
        log_level=_env("SAMUS_LOG_LEVEL", "INFO") or "INFO",
        shared_hmac_key=_env("SAMUS_SHARED_HMAC_KEY", "") or "",
        hmac_window_seconds=_env_int("SAMUS_HMAC_WINDOW_SECONDS", 300),
        # R-1: per-service HMAC keys (additive, shared-key fallback) + the
        # caller-authz enforcement mode. Both default to dormant: an empty
        # key map means every service uses the shared key, and authz_mode
        # "off" means the caller-authz matrix is not evaluated.
        per_service_hmac_keys=_collect_per_service_hmac_keys(),
        authz_mode=_resolve_authz_mode(),
        # operator_console PACK auth gate — cross-agent SN_BEARER_REQUIRED
        # convention. Default True (ecosystem tightened posture); operator
        # flips to False to expose the gateway-mounted console without
        # bearer (loopback-only trust model).
        bearer_required=_env_bool("SN_BEARER_REQUIRED", True),
        samus_ledger_secret_key=_env("SAMUS_LEDGER_SECRET_KEY", "") or "",
        samus_ledger_epoch_sec=_env_int("SAMUS_LEDGER_EPOCH_SEC", 86400),
        samus_ledger_epoch_overlap_sec=_env_int("SAMUS_LEDGER_EPOCH_OVERLAP_SEC", 300),
        samus_ledger_backend=_env("SAMUS_LEDGER_BACKEND", "jsonl") or "jsonl",
        security_sentinel_url=_env("SECURITY_SENTINEL_URL", "http://localhost:8434")
        or "http://localhost:8434",
        samus_heartbeat_path=_env("SAMUS_HEARTBEAT_PATH", "") or "",
        samus_heartbeat_interval_sec=_env_float("SAMUS_HEARTBEAT_INTERVAL_SEC", 30.0),
        samus_agent_id=_env("SAMUS_AGENT_ID", "samus") or "samus",
        gateway_urls=gateway_urls,
        sqs_queue_urls=sqs_queue_urls,
        # --- DocuSeal document signing (self-hosted; backend.contracts) ----
        docuseal_enabled=_env_bool("DOCUSEAL_ENABLED", False),
        docuseal_api_base=_env("DOCUSEAL_API_BASE", "http://samus-docuseal:3000/api")
        or "http://samus-docuseal:3000/api",
        docuseal_api_token=_env("DOCUSEAL_API_TOKEN", "") or "",
        docuseal_public_base=_env("DOCUSEAL_PUBLIC_BASE", "") or "",
        docuseal_service_agreement_template_id=_env("DOCUSEAL_SERVICE_AGREEMENT_TEMPLATE_ID", "")
        or "",
        docuseal_webhook_secret=_env("DOCUSEAL_WEBHOOK_SECRET", "") or "",
        docuseal_http_timeout_sec=_env_int("DOCUSEAL_HTTP_TIMEOUT_SEC", 15),
        # cash_engine — autonomous revenue pipeline behind the codex-gated
        # /api/samus/review_opportunity front door. Defaults keep the engine
        # on with a logic-first (mock-queue) posture; no SQS queue is wired
        # for cash_engine in the loop above, so enqueue takes the JSONL
        # fallback until SQS_CASH_ENGINE_QUEUE_URL + the roster entry land.
        cash_engine_enabled=_env_bool("SAMUS_CASH_ENGINE_ENABLED", True),
        cash_engine_decay_threshold=_env_float("SAMUS_CASH_ENGINE_DECAY_THRESHOLD", 0.6),
        cash_engine_stall_days=_env_float("SAMUS_CASH_ENGINE_STALL_DAYS", 7.0),
        # CRM closed-won hard cap (redteam FIN-10). Active out of the box at
        # the operator-chosen ceiling; <=0 disables the cap.
        crm_max_close_amount_usd=_env_float("SAMUS_CRM_MAX_CLOSE_AMOUNT_USD", 1000.0),
        cash_engine_queue_service=_env("SAMUS_CASH_ENGINE_QUEUE_SERVICE", "cash_engine")
        or "cash_engine",
        cash_engine_live_send_enabled=_env_bool("SAMUS_CASH_ENGINE_LIVE_SEND", False),
        samus_max_sends_per_day=_env_int("SAMUS_MAX_SENDS_PER_DAY", 15),
        samus_max_calls_per_day=_env_int("SAMUS_MAX_CALLS_PER_DAY", 40),
        sender_postal_address=_env("SAMUS_SENDER_POSTAL_ADDRESS", "") or "",
        unsubscribe_url=_env("SAMUS_UNSUBSCRIBE_URL", "") or "",
        compliance_guard_mode=_env("SAMUS_COMPLIANCE_GUARD_MODE", "off") or "off",
        compliance_from_domains=_env("SAMUS_COMPLIANCE_FROM_DOMAINS", "") or "",
        heat_field_enabled=_env_bool("SAMUS_HEAT_FIELD_ENABLED", False),
        sendgrid_webhook_verification_key=_env("SENDGRID_WEBHOOK_VERIFICATION_KEY", "") or "",
        ddb_heat_state_table=_env("DDB_HEAT_STATE_TABLE", "samus_heat_state") or "samus_heat_state",
        karma_gate_enabled=_env_bool("SAMUS_KARMA_GATE_ENABLED", False),
        ddb_karma_table=_env("DDB_KARMA_TABLE", "samus_karma") or "samus_karma",
        reply_handling_enabled=_env_bool("SAMUS_REPLY_HANDLING_ENABLED", False),
        attribution_enabled=_env_bool("SAMUS_ATTRIBUTION_ENABLED", True),
        ddb_attribution_table=_env("DDB_ATTRIBUTION_TABLE", "samus_attribution_variants")
        or "samus_attribution_variants",
        # Cognitive self-model tier + Stage-1 persona-frame (ported from
        # recovery/). Both DEFAULT OFF (dormant): the self-model is a no-op
        # until armed, and the persona-frame is COMPUTE-ONLY with no live
        # consumer. See backend/memory/persona_self_model + backend/cognitive.
        persona_self_model_enabled=_env_bool("SAMUS_PERSONA_SELF_MODEL_ENABLED", False),
        persona_frame_enabled=_env_bool("SAMUS_PERSONA_FRAME_ENABLED", False),
        # Autonomy layer subsystems (backend.autonomy.*; build-plan Phase B).
        # ALL DEFAULT OFF (wire-not-arm): each subsystem is inert until armed and
        # no live caller constructs them. The upgrade engine, even when armed,
        # only writes PROPOSAL records — it never self-modifies code.
        autonomy_reinforcement_enabled=_env_bool(
            "SAMUS_AUTONOMY_REINFORCEMENT_ENABLED",
            False,
        ),
        autonomy_autotuner_enabled=_env_bool(
            "SAMUS_AUTONOMY_AUTOTUNER_ENABLED",
            False,
        ),
        autonomy_upgrade_enabled=_env_bool(
            "SAMUS_AUTONOMY_UPGRADE_ENABLED",
            False,
        ),
        # ADR-016 governed autonomous dial policy. DEFAULT OFF: voice_dial stays
        # VR-G5-blocked until armed. Flipping off = instant hard block again.
        governed_autonomous_dial_enabled=_env_bool(
            "SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED",
            False,
        ),
        # Idle-production drive (self-pacing). DEFAULT OFF: when off, the
        # control-tick idle stage is an observe-only no-op (never actuates).
        idle_production_drive_enabled=_env_bool(
            "SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED",
            False,
        ),
        # MetaCognitionEngine wrapper (build-plan Phase D). DEFAULT ON as of
        # Slice B (2026-07-06 activation): the runner constructs the wrapper
        # and each cycle runs META-PERCEPTION + loop.cycle + META-REFLECTION.
        # Set SAMUS_AUTONOMY_META_ENABLED=0 to fall back to pure passthrough
        # (byte-identical to no wrapper) without redeploying — the runner
        # re-reads this flag every construct. The autonomy SUBSYSTEM flags
        # below (reinforcement / autotuner / upgrade) remain default OFF so
        # activation adds only the situation-index + advisory bias +
        # observational reflection surface, not weight-writing or upgrade
        # proposals.
        autonomy_meta_enabled=_env_bool(
            "SAMUS_AUTONOMY_META_ENABLED",
            True,
        ),
        # Propose-only ACT stage of the CognitiveLoop (build-plan Phase E).
        # DEFAULT OFF (wire-not-arm): when off, ACT writes nothing (pure no-op).
        # When armed, ACT records ONE structured proposal artifact to
        # state_path("cognition","proposals.jsonl") and calls NO effector/gate.
        cognitive_act_proposals_enabled=_env_bool(
            "SAMUS_COGNITIVE_ACT_PROPOSALS_ENABLED",
            False,
        ),
        # Master switch for the LIVE CALLER of the cognitive stack (build-plan
        # Phase F). DEFAULT ON as of Slice B (2026-07-06 activation) — matches
        # the already-True defaults for cognition_proposal_promotion_enabled
        # and cognition_cadence_enabled, so the previously inconsistent
        # "cadence starts but calls a disabled loop" state is resolved.
        # Set SAMUS_COGNITIVE_LOOP_ENABLED=0 to fast-disarm: the /api/samus/
        # cognition/cycle route becomes a structured no-op/503 and the cadence
        # tick becomes a no-op (the cadence re-checks this flag every tick).
        # Each sub-behaviour still respects its own flag (meta, ACT proposals,
        # persona, autonomy subsystems).
        cognitive_loop_enabled=_env_bool(
            "SAMUS_COGNITIVE_LOOP_ENABLED",
            True,
        ),
        # Phase-G kill-switch: route propose-only proposals through the
        # cash-engine front door (review_opportunity). DEFAULT True per
        # operator direction (the "wire ON" stance) — the gates inside
        # review_opportunity (Stake Sentence + Codex + karma + downstream
        # compliance/EFH) remain authoritative; Phase G calls them, never
        # bypasses them. Flip to False to fast-disable: the runner seam
        # becomes a pure no-op.
        cognition_proposal_promotion_enabled=_env_bool(
            "SAMUS_COGNITION_PROPOSAL_PROMOTION_ENABLED",
            True,
        ),
        # Phase-G per-prospect BLOCK cooldown (seconds). After a BLOCK verdict
        # for a prospect_id the promoter suppresses re-promotion of that SAME
        # prospect for this window (SKIP without re-calling review_opportunity),
        # so the active cadence stops re-promoting the same sticky-BLOCKED
        # prospect every tick. Default 600 (~10 ticks at the 60s cadence).
        cognition_promotion_block_cooldown_sec=_env_int(
            "SAMUS_COGNITION_PROMOTION_BLOCK_COOLDOWN_SEC",
            600,
        ),
        # ACTIVE autonomous cognition cadence (backend/cognitive/cadence.py).
        # DEFAULT True per operator direction ("wire ON"): the gateway lifespan
        # starts the cadence loop, which calls run_one_cycle on the
        # interval+jitter cadence below. The cadence re-checks
        # cognitive_loop_enabled every tick, so a runtime arm/disarm doesn't
        # require a gateway restart. Set to 0/false to skip starting the task.
        cognition_cadence_enabled=_env_bool(
            "SAMUS_COGNITION_CADENCE_ENABLED",
            True,
        ),
        cognition_cadence_interval_seconds=_env_int(
            "SAMUS_COGNITION_CADENCE_INTERVAL_SECONDS",
            60,
        ),
        cognition_cadence_jitter_seconds=_env_int(
            "SAMUS_COGNITION_CADENCE_JITTER_SECONDS",
            10,
        ),
        # CODB investment reasoner — wire-not-arm. Default OFF: EOD omits the
        # codb_investment_recommendations section and the deal-closed stimulus
        # is a no-op. Recommend-only either way (never spends).
        codb_reasoner_enabled=_env_bool("SAMUS_CODB_REASONER_ENABLED", False),
        # Soft-no re-engagement sweep — promotional resurfacing of
        # not_interested prospects past the cooldown. Default OFF; armed
        # via SAMUS_REENGAGEMENT_ENABLED. Cooldown + per-run cap tunable.
        samus_reengagement_enabled=_env_bool(
            "SAMUS_REENGAGEMENT_ENABLED",
            False,
        ),
        samus_reengagement_cooldown_days=_env_int(
            "SAMUS_REENGAGEMENT_COOLDOWN_DAYS",
            30,
        ),
        samus_reengagement_max_per_run=_env_int(
            "SAMUS_REENGAGEMENT_MAX_PER_RUN",
            25,
        ),
        # Buying-signal email route. Default OFF; armed via
        # OUTREACH_BUYING_SIGNAL_ROUTE_ENABLED (enrollment only — sending stays
        # a separate dormant step gated on a composer).
        outreach_buying_signal_route_enabled=_env_bool(
            "OUTREACH_BUYING_SIGNAL_ROUTE_ENABLED",
            False,
        ),
        outreach_buying_signal_intent_threshold=_env_int(
            "OUTREACH_BUYING_SIGNAL_INTENT_THRESHOLD",
            70,
        ),
        # Website-form warm-lead enrollment (wire-not-arm). Default OFF —
        # form submissions land in DDB but don't enter any warm cadence
        # until this is armed. Independent of the voice-path flag above.
        outreach_website_form_warm_enroll_enabled=_env_bool(
            "OUTREACH_WEBSITE_FORM_WARM_ENROLL_ENABLED",
            False,
        ),
        # Site telemetry ingest (wire-not-arm). Default OFF — the endpoint
        # returns 200 (never breaks the site beacon) but persists nothing.
        # Armed via SAMUS_TELEMETRY_INGEST_ENABLED; ledger path overridable
        # via SAMUS_INTAKE_TELEMETRY_PATH.
        intake_telemetry_ingest_enabled=_env_bool(
            "SAMUS_TELEMETRY_INGEST_ENABLED",
            False,
        ),
        # Post-call remediate -> next-touch loop. Default OFF; armed via
        # SAMUS_POSTCALL_REMEDIATE_ENABLED. When on, a positive/interested
        # outbound call delivers the SEO remediation deliverable + enqueues a
        # follow-up touch. Never initiates a dial (enrollment/queue only).
        postcall_remediate_enabled=_env_bool(
            "SAMUS_POSTCALL_REMEDIATE_ENABLED",
            False,
        ),
        # Open-no-click nudge watcher. Default DORMANT — register/tick logic
        # runs and updates state; firing the nudge requires this flag ON.
        outreach_open_no_click_nudge_enabled=_env_bool(
            "OUTREACH_OPEN_NO_CLICK_NUDGE_ENABLED",
            False,
        ),
        outreach_open_no_click_dwell_hours=max(
            1,
            _env_int(
                "OUTREACH_OPEN_NO_CLICK_DWELL_HOURS",
                24,
            ),
        ),
        # Send-lint mode. off | audit | enforce. Default off.
        outreach_send_lint_mode=(os.getenv("OUTREACH_SEND_LINT_MODE", "off") or "off")
        .strip()
        .lower(),
        samus_dialer_cooldown_days=max(
            0,
            min(
                14,
                _env_int(
                    "SAMUS_DIALER_COOLDOWN_DAYS",
                    7,
                ),
            ),
        ),
        samus_dialer_voicemail_exempt_hours=max(
            0,
            _env_int(
                "SAMUS_DIALER_VOICEMAIL_EXEMPT_HOURS",
                36,
            ),
        ),
        samus_dialer_business_hours_gate=_env_bool(
            "SAMUS_DIALER_BUSINESS_HOURS_GATE",
            False,
        ),
        # prospecting in-stack daily cadence (ADR-014). DEFAULT OFF — the
        # in-container daily loop stays dormant until an operator arms it,
        # so the forward-port changes nothing at runtime by itself.
        prospecting_in_stack_cadence_enabled=_env_bool(
            "SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED",
            False,
        ),
        # starvation-aware self-supply (input replenishment from the idle
        # drive). DEFAULT OFF (wire-not-arm): diagnosis always reports;
        # replenish actuation held until an operator arms it.
        self_supply_enabled=_env_bool("SAMUS_SELF_SUPPLY_ENABLED", False),
        # auto-stake sweep (force-multiplier). DEFAULT OFF. Armed via
        # SAMUS_AUTO_STAKE_ENABLED. Knobs read directly from env at sweep
        # time: SAMUS_AUTO_STAKE_{MIN_LEAD_SCORE,MAX_PER_SWEEP,RECENT_DAYS,
        # SCAN_LIMIT} — see backend/cash_engine/auto_stake.py.
        auto_stake_enabled=_env_bool("SAMUS_AUTO_STAKE_ENABLED", False),
        # Apollo.io enrichment key (last-resort owner-contact lookup). Empty
        # = adapter is a no-op. Daily $-cap enforced by the SHARED G11
        # store (apollo_budget.py + SAMUS_APOLLO_DAILY_BUDGET_USD).
        apollo_api_key=_env("APOLLO_API_KEY", "") or "",
        # website builder — dormant fulfillment capability (backend/website/).
        # Machinery on; autonomy + live publish + Wix creds all default-off so
        # the supervised walk-through is the only path that does anything, and
        # only after per-stage operator approval.
        website_builder_enabled=_env_bool("SAMUS_WEBSITE_BUILDER_ENABLED", True),
        website_autonomous_enabled=_env_bool("SAMUS_WEBSITE_AUTONOMOUS_ENABLED", False),
        website_live_publish_enabled=_env_bool("SAMUS_WEBSITE_LIVE_PUBLISH", False),
        wix_api_key=_env("WIX_API_KEY", "") or "",
        wix_account_id=_env("WIX_ACCOUNT_ID", "") or "",
        website_content_collection_id=_env("WIX_CONTENT_COLLECTION_ID", "") or "",
        # taste-governed content generation (the "frontend codegen"). On by
        # default; degrades to deterministic copy without an API key / budget.
        website_content_generation_enabled=_env_bool(
            "SAMUS_WEBSITE_CONTENT_GENERATION_ENABLED",
            True,
        ),
        website_content_model=_env(
            "SAMUS_WEBSITE_CONTENT_MODEL",
            "claude-haiku-4-5-20251001",
        )
        or "claude-haiku-4-5-20251001",
        website_content_max_tokens=_env_int("SAMUS_WEBSITE_CONTENT_MAX_TOKENS", 1200),
        # SEO + security enrichment, enforced before delivery (off|audit|enforce).
        website_headless_enabled=_env_bool("SAMUS_WEBSITE_HEADLESS_ENABLED", False),
        website_design_intelligence_enabled=_env_bool(
            "SAMUS_WEBSITE_DESIGN_INTELLIGENCE_ENABLED", True
        ),
        website_legal_pages_enabled=_env_bool("SAMUS_WEBSITE_LEGAL_PAGES_ENABLED", True),
        website_multipage_enabled=_env_bool("SAMUS_WEBSITE_MULTIPAGE_ENABLED", True),
        website_deploy_enabled=_env_bool("SAMUS_WEBSITE_DEPLOY_ENABLED", False),
        website_deploy_host=_env("SAMUS_WEBSITE_DEPLOY_HOST", "cloudflare") or "cloudflare",
        cloudflare_api_token=_env("CLOUDFLARE_API_TOKEN", "") or "",
        cloudflare_account_id=_env("CLOUDFLARE_ACCOUNT_ID", "") or "",
        cloudflare_pages_project=_env("CLOUDFLARE_PAGES_PROJECT", "") or "",
        website_seo_enrich_enabled=_env_bool("SAMUS_WEBSITE_SEO_ENRICH_ENABLED", True),
        website_seo_gate=_env("SAMUS_WEBSITE_SEO_GATE", "enforce") or "enforce",
        website_security_gate=_env("SAMUS_WEBSITE_SECURITY_GATE", "enforce") or "enforce",
        website_seo_min_score=_env_int("SAMUS_WEBSITE_SEO_MIN_SCORE", 70),
        website_security_min_grade=_env("SAMUS_WEBSITE_SECURITY_MIN_GRADE", "B") or "B",
        # Operator-prompted cold-call demo-site builder — default OFF (an
        # explicit money/outreach decision; the ps1 wrapper sets the env).
        website_demo_sites_enabled=_env_bool("SAMUS_WEBSITE_DEMO_SITES_ENABLED", False),
        demo_sites_ceiling_usd=_env_float("SAMUS_DEMO_SITES_CEILING_USD", 5.0),
        # Brand-asset isolation gate STRICT mode (strip non-allowlisted
        # remote refs from generated sites; the gate itself always runs).
        website_asset_isolation_strict=_env_bool("SAMUS_WEBSITE_ASSET_ISOLATION_STRICT", False),
        # AI media generation (Gemini image / Veo video) — paid, dormant by
        # default, metered by media_budget (separate from the Anthropic cap).
        gemini_api_key=_env("GEMINI_API_KEY", "") or "",
        twentyfirst_api_key=_env("TWENTYFIRST_API_KEY", "") or "",
        website_media_gen_enabled=_env_bool("SAMUS_WEBSITE_MEDIA_GEN_ENABLED", False),
        website_media_image_model=_env(
            "SAMUS_WEBSITE_MEDIA_IMAGE_MODEL",
            "gemini-2.5-flash-image",
        )
        or "gemini-2.5-flash-image",
        website_media_image_size=_env("SAMUS_WEBSITE_MEDIA_IMAGE_SIZE", "2K") or "2K",
        website_media_video_enabled=_env_bool("SAMUS_WEBSITE_MEDIA_VIDEO_ENABLED", False),
        website_media_video_model=_env(
            "SAMUS_WEBSITE_MEDIA_VIDEO_MODEL",
            "veo-3.1-fast-generate-preview",
        )
        or "veo-3.1-fast-generate-preview",
        website_media_video_resolution=_env("SAMUS_WEBSITE_MEDIA_VIDEO_RESOLUTION", "720p")
        or "720p",
        website_media_video_seconds=_env("SAMUS_WEBSITE_MEDIA_VIDEO_SECONDS", "8") or "8",
        media_video_requires_approval=_env_bool("SAMUS_MEDIA_VIDEO_REQUIRES_APPROVAL", True),
        media_daily_dollar_cap=_env_float("SAMUS_MEDIA_DAILY_DOLLAR_CAP", 2.0),
        media_image_cost_usd=_env_float("SAMUS_MEDIA_IMAGE_COST_USD", 0.04),
        media_video_cost_usd=_env_float("SAMUS_MEDIA_VIDEO_COST_USD", 0.75),
        website_media_people_directive=_env(
            "SAMUS_MEDIA_PEOPLE_DIRECTIVE",
            "Feature an ethnically diverse, multi-racial team of varied ages and genders "
            "that authentically represents the local community.",
        )
        or "",
        # Social reel generation (MoneyPrinterTurbo-style). Dormant by default;
        # paid footage reuses the media budget above (no second cap).
        social_reel_enabled=_env_bool("SAMUS_SOCIAL_REEL_ENABLED", False),
        # ECO-ADR-0003 §7 revenue foresight cone (shadow). ARMED (default ON)
        # per operator decision 2026-06-08; SAMUS_FORESIGHT_ENABLED=false disables.
        foresight_enabled=_env_bool("SAMUS_FORESIGHT_ENABLED", True),
        social_reel_tts_voice=_env("SAMUS_SOCIAL_REEL_TTS_VOICE", "en-US-AriaNeural")
        or "en-US-AriaNeural",
        social_reel_footage_mode=_env("SAMUS_SOCIAL_REEL_FOOTAGE_MODE", "image") or "image",
        social_reel_video_enabled=_env_bool("SAMUS_SOCIAL_REEL_VIDEO_ENABLED", False),
        social_reel_aspect=_env("SAMUS_SOCIAL_REEL_ASPECT", "9:16") or "9:16",
        social_reel_max_segments=_env_int("SAMUS_SOCIAL_REEL_MAX_SEGMENTS", 5),
        social_reel_music_dir=_env("SAMUS_SOCIAL_REEL_MUSIC_DIR", "") or "",
        # Medusa internal commerce (products income stream). Dormant by default.
        commerce_medusa_enabled=_env_bool("SAMUS_COMMERCE_MEDUSA_ENABLED", False),
        medusa_base_url=_env("MEDUSA_BASE_URL", "") or "",
        medusa_admin_token=_env("MEDUSA_ADMIN_TOKEN", "") or "",
        # TikTok Shop. Research provider pluggable; posting + selling dormant.
        tiktok_research_provider=_env("TIKTOK_RESEARCH_PROVIDER", "none") or "none",
        tiktok_research_api_key=_env("TIKTOK_RESEARCH_API_KEY", "") or "",
        tiktok_research_base_url=_env("TIKTOK_RESEARCH_BASE_URL", "") or "",
        tiktok_content_posting_enabled=_env_bool("SAMUS_TIKTOK_CONTENT_POSTING_ENABLED", False),
        tiktok_content_access_token=_env("TIKTOK_CONTENT_ACCESS_TOKEN", "") or "",
        tiktok_shop_enabled=_env_bool("SAMUS_TIKTOK_SHOP_ENABLED", False),
        tiktok_shop_app_key=_env("TIKTOK_SHOP_APP_KEY", "") or "",
        tiktok_shop_app_secret=_env("TIKTOK_SHOP_APP_SECRET", "") or "",
        tiktok_shop_access_token=_env("TIKTOK_SHOP_ACCESS_TOKEN", "") or "",
        tiktok_dry_run=_env_bool("SAMUS_TIKTOK_DRY_RUN", True),
        # n8n workflow generation. Generation defaults ON (pure/$0/offline);
        # deploy + LLM enrichment dormant by default.
        workflow_n8n_deliverable_enabled=_env_bool("SAMUS_WORKFLOW_N8N_DELIVERABLE_ENABLED", True),
        workflow_n8n_llm_enrich=_env_bool("SAMUS_WORKFLOW_N8N_LLM_ENRICH", False),
        workflow_n8n_deploy_enabled=_env_bool("SAMUS_WORKFLOW_N8N_DEPLOY_ENABLED", False),
        workflow_n8n_dry_run=_env_bool("SAMUS_WORKFLOW_N8N_DRY_RUN", True),
        n8n_base_url=_env("N8N_BASE_URL", "") or "",
        n8n_api_key=_env("N8N_API_KEY", "") or "",
        aws_region=_env("AWS_REGION", "us-west-1") or "us-west-1",
        ddb_task_state_table=_env("DDB_TASK_STATE_TABLE", "samus_task_state") or "samus_task_state",
        ddb_idempotency_table=_env("DDB_IDEMPOTENCY_TABLE", "samus_idempotency")
        or "samus_idempotency",
        ddb_suppression_table=_env("DDB_SUPPRESSION_TABLE", "samus_suppression")
        or "samus_suppression",
        ddb_recipient_index_table=_env("DDB_RECIPIENT_INDEX_TABLE", "samus_recipient_index")
        or "samus_recipient_index",
        ddb_feedback_table=_env("DDB_FEEDBACK_TABLE", "samus_feedback_events")
        or "samus_feedback_events",
        ddb_onboarding_leads_table=_env("DDB_ONBOARDING_LEADS_TABLE", "samus_onboarding_leads")
        or "samus_onboarding_leads",
        ddb_llm_budgets_table=_env("DDB_LLM_BUDGETS_TABLE", "samus_llm_budgets")
        or "samus_llm_budgets",
        # Strategy bandit table. Unlike most ddb_*_table fields this one keeps
        # an explicit empty string when DDB_STRATEGY_BANDIT_TABLE is set to ""
        # — the empty value is the meaningful "DDB disabled, JSON only" signal
        # the test suite relies on (mirrors DDB_LLM_BUDGETS_TABLE handling in
        # conftest). ``_env`` returns None for an empty env value, so coalesce
        # to "" only when the var is genuinely unset is NOT what we want here;
        # instead check os.environ directly for the disable signal.
        ddb_strategy_bandit_table=_strategy_bandit_table(),
        ddb_prospects_table=_env("DDB_PROSPECTS_TABLE", "samus_prospects") or "samus_prospects",
        ddb_contacts_table=_env("DDB_CONTACTS_TABLE", "samus_contacts") or "samus_contacts",
        ddb_conversations_table=_env("DDB_CONVERSATIONS_TABLE", "samus_conversations")
        or "samus_conversations",
        ddb_call_state_table=_env("DDB_CALL_STATE_TABLE", "samus_call-State") or "samus_call-State",
        ddb_opportunities_table=_env("DDB_OPPORTUNITIES_TABLE", "samus_opportunities")
        or "samus_opportunities",
        ddb_operator_tasks_table=_env("DDB_OPERATOR_TASKS_TABLE", "samus_operator_tasks")
        or "samus_operator_tasks",
        ddb_approvals_table=_env("DDB_APPROVALS_TABLE", "samus_approvals") or "samus_approvals",
        ddb_artifacts_table=_env("DDB_ARTIFACTS_TABLE", "samus_artifacts") or "samus_artifacts",
        ddb_needs_warm_path_table=_env("DDB_NEEDS_WARM_PATH_TABLE", "") or "",
        sns_event_topic_arn=_env("SNS_EVENT_TOPIC_ARN", "") or "",
        ses_from_email=_env("SES_FROM_EMAIL", "") or "",
        neo4j_uri=_env("NEO4J_URI", "bolt://localhost:7687") or "bolt://localhost:7687",
        neo4j_user=_env("NEO4J_USER", "neo4j") or "neo4j",
        neo4j_password=_env("NEO4J_PASSWORD", "") or "",
        neo4j_database=_env("NEO4J_DATABASE", "samus") or "samus",
        neo4j_required=_env_bool("NEO4J_REQUIRED", False),
        kg_tier_mode=_resolve_kg_tier_mode(),
        # CRM Hivemind projection (CRM Phase-2). Default ON — opportunity +
        # conversation writes also mirror the prospect sub-graph into the local
        # KG (idempotent MERGE). No-ops cleanly when Neo4j is unreachable
        # (local-default, fail-closed), so installs without Neo4j are
        # unaffected. Set the env var to false to disable.
        crm_hivemind_projection_enabled=_env_bool(
            "SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED",
            True,
        ),
        # ChromaDB vector layer (Phase-3 S8). Default-OFF; chromadb lazy-imported.
        vector_store_enabled=_env_bool("SAMUS_VECTOR_STORE_ENABLED", False),
        vector_store_collection=_env(
            "SAMUS_VECTOR_STORE_COLLECTION",
            "samus_knowledge",
        )
        or "samus_knowledge",
        vector_store_path=_env(
            "SAMUS_VECTOR_STORE_PATH",
            "/opt/samus/data/vectorstore",
        )
        or "/opt/samus/data/vectorstore",
        vector_store_host=_env("SAMUS_VECTOR_STORE_HOST", "") or "",
        vector_store_port=_env_int("SAMUS_VECTOR_STORE_PORT", 0),
        google_places_api_key=_env("GOOGLE_PLACES_API_KEY", "") or "",
        google_cse_api_key=_env("GOOGLE_CSE_API_KEY", "") or "",
        google_cse_id=_env("GOOGLE_CSE_ID", "") or "",
        presence_deep_verify_enabled=_env_bool("SAMUS_PRESENCE_DEEP_VERIFY_ENABLED", True),
        anthropic_api_key=_env("ANTHROPIC_API_KEY", "")
        or "",  # Unused — LM Studio backend needs no auth
        anthropic_admin_api_key=_env("ANTHROPIC_ADMIN_API_KEY", "") or "",
        samus_efh_semantic_enabled=_env_bool("SAMUS_EFH_SEMANTIC", True),
        lm_studio_base_url=_env("LM_STUDIO_BASE_URL", "") or "",
        stripe_api_key=_env("STRIPE_API_KEY", "") or "",
        stripe_webhook_secret=_env("STRIPE_WEBHOOK_SECRET", "") or "",
        # Revenue-stream partition (separate income sources). Empty -> pooled
        # under stripe_api_key / the default entity (back-compatible).
        stripe_api_key_products=_env("STRIPE_API_KEY_PRODUCTS", "") or "",
        stripe_api_key_tiktok=_env("STRIPE_API_KEY_TIKTOK", "") or "",
        stripe_webhook_secret_products=_env("STRIPE_WEBHOOK_SECRET_PRODUCTS", "") or "",
        stripe_webhook_secret_tiktok=_env("STRIPE_WEBHOOK_SECRET_TIKTOK", "") or "",
        revenue_entity_default=_env("REVENUE_ENTITY_DEFAULT", "HustleForge LLC")
        or "HustleForge LLC",
        revenue_entity_products=_env("REVENUE_ENTITY_PRODUCTS", "") or "",
        revenue_entity_tiktok=_env("REVENUE_ENTITY_TIKTOK", "") or "",
        vapi_api_key=_env("VAPI_API_KEY", "") or "",
        vapi_webhook_secret=_env("VAPI_WEBHOOK_SECRET", "") or "",
        vapi_assistant_id=_env("VAPI_ASSISTANT_ID", "") or "",
        vapi_phone_number_id=_env("VAPI_PHONE_NUMBER_ID", "") or "",
        samus_voice_rep_name=_env("SAMUS_VOICE_REP_NAME", "Morgan") or "Morgan",
        samus_voice_callback_number=(
            _env("SAMUS_VOICE_CALLBACK_NUMBER", "(530) 555-0105") or "(530) 555-0105"
        ),
        # Prior-call context injection (default OFF). Arms the dialer's merge of
        # build_prospect_context() keys into Vapi variableValues.
        samus_voice_profile_context_enabled=_env_bool(
            "SAMUS_VOICE_PROFILE_CONTEXT",
            False,
        ),
        outreach_voice_send_enabled=_env_bool("SAMUS_OUTREACH_VOICE_SEND", False),
        vapi_inbound_assistant_id=_env("VAPI_INBOUND_ASSISTANT_ID", "") or "",
        vapi_inbound_phone_number_id=_env("VAPI_INBOUND_PHONE_NUMBER_ID", "") or "",
        twilio_account_sid=_env("TWILIO_ACCOUNT_SID", "") or "",
        twilio_auth_token=_env("TWILIO_AUTH_TOKEN", "") or "",
        twilio_api_key=_env("TWILIO_API_KEY", "") or "",
        twilio_api_secret=_env("TWILIO_API_SECRET", "") or "",
        ngrok_authtoken=_env("NGROK_AUTHTOKEN", "") or "",
        ngrok_reserved_domain=_env("NGROK_RESERVED_DOMAIN", "") or "",
        email_backend=_env("EMAIL_BACKEND", "sendgrid") or "sendgrid",
        sendgrid_api_key=_env("SENDGRID_API_KEY", "") or "",
        sendgrid_from_email=_env("SENDGRID_FROM_EMAIL", "") or "",
        sendgrid_from_name=_env("SENDGRID_FROM_NAME", "HustleForge") or "HustleForge",
        sendgrid_reply_to=_env("SENDGRID_REPLY_TO", "") or "",
        sendgrid_base_url=_env("SENDGRID_BASE_URL", "https://api.sendgrid.com")
        or "https://api.sendgrid.com",
        auto_fulfill_offer_codes=[
            x.strip() for x in (_env("SAMUS_AUTO_FULFILL_OFFERS", "") or "").split(",") if x.strip()
        ],
        intake_allowed_origins=[
            x.strip()
            for x in (
                _env("SAMUS_INTAKE_ALLOWED_ORIGINS", "https://hustleforge.tech")
                or "https://hustleforge.tech"
            ).split(",")
            if x.strip()
        ],
        intake_dedup_window_seconds=_env_int("SAMUS_INTAKE_DEDUP_WINDOW_S", 86400),
        intake_operator_email=_env("SAMUS_INTAKE_OPERATOR_EMAIL", "") or "",
        gmail_inbox_email=_env("SAMUS_GMAIL_INBOX_EMAIL", "") or "",
        gmail_oauth_client_id=_env("SAMUS_GMAIL_OAUTH_CLIENT_ID", "") or "",
        gmail_oauth_client_secret=_env("SAMUS_GMAIL_OAUTH_CLIENT_SECRET", "") or "",
        gmail_oauth_token_path=_env(
            "SAMUS_GMAIL_OAUTH_TOKEN_PATH",
            "/opt/samus/data/intake/gmail_oauth_token.json",
        )
        or "/opt/samus/data/intake/gmail_oauth_token.json",
        gmail_inbox_max_per_pass=_env_int("SAMUS_GMAIL_INBOX_MAX_PER_PASS", 25),
        gmail_inbox_ledger_path=_env(
            "SAMUS_GMAIL_INBOX_LEDGER",
            "/opt/samus/data/intake/inbound_email.jsonl",
        )
        or "/opt/samus/data/intake/inbound_email.jsonl",
        llm_workcell_base_token_budget=_env_int("SAMUS_LLM_BASE_TOKENS", 100_000),
        llm_budget_ema_alpha=_env_float("SAMUS_LLM_BUDGET_EMA_ALPHA", 0.05),
        llm_budget_floor_pct=_env_float("SAMUS_LLM_BUDGET_FLOOR_PCT", 0.10),
        # LLM cost-control layer (token-cost-hardening 2026-05-18, port from
        # 29ba2df). $1/day cap is the operator-chosen production posture per
        # memory:project_samus_llm_token_policy (Lever 1.1).
        llm_global_daily_dollar_cap=_env_float("SAMUS_LLM_GLOBAL_DAILY_DOLLAR_CAP", 1.0),
        llm_reinvest_pct=_env_float("SAMUS_LLM_REINVEST_PCT", 0.05),
        llm_daily_floor_usd=_env_float("SAMUS_LLM_DAILY_FLOOR_USD", 0.50),
        llm_daily_ceiling_usd=_env_float("SAMUS_LLM_DAILY_CEILING_USD", 25.0),
        llm_cheap_model_pattern=_env(
            "SAMUS_LLM_CHEAP_MODEL_PATTERN",
            r"^claude-haiku-",
        )
        or r"^claude-haiku-",
        llm_circuit_breaker_threshold=_env_int(
            "SAMUS_LLM_CIRCUIT_BREAKER_THRESHOLD",
            10,
        ),
        llm_circuit_breaker_cooldown_sec=_env_int(
            "SAMUS_LLM_CIRCUIT_BREAKER_COOLDOWN_SEC",
            300,
        ),
        # Portfolio trigger layer (Lever 3.2; backend/strategy/triggers.py).
        portfolio_workcell_dollar_cap=_env_float("SAMUS_PORTFOLIO_DOLLAR_CAP", 0.20),
        portfolio_min_signal_change=_env_float("SAMUS_PORTFOLIO_MIN_SIGNAL_CHANGE", 0.15),
        portfolio_tick_interval_sec=_env_int("SAMUS_PORTFOLIO_TICK_INTERVAL_SEC", 900),
        # SEO passive security audit toggle (2026-05-20). Default True.
        seo_security_audit_enabled=_env_bool(
            "SAMUS_SEO_SECURITY_AUDIT_ENABLED",
            True,
        ),
        # SEO durable input_hash result cache (WIRED-DORMANT). Default FALSE —
        # off = pre-feature behaviour; operator flips SAMUS_AUDIT_CACHE_ENABLED.
        seo_audit_cache_enabled=_env_bool(
            "SAMUS_AUDIT_CACHE_ENABLED",
            False,
        ),
        # ==================================================================
        # intake-hardening section (feat/samus-intake-hardening) -----------
        # ==================================================================
        # Per-IP rate limiting on POST /intake/onboarding. Enabled by
        # default for real deployments; the pytest process leaves
        # SAMUS_INTAKE_RATE_LIMIT_ENABLED unset and conftest does not set
        # it, so dedicated rate-limit tests opt in explicitly.
        intake_rate_limit_enabled=_env_bool("SAMUS_INTAKE_RATE_LIMIT_ENABLED", True),
        intake_rate_limit_per_minute=_env_int("SAMUS_INTAKE_RATE_LIMIT_PER_MINUTE", 5),
        intake_rate_limit_per_hour=_env_int("SAMUS_INTAKE_RATE_LIMIT_PER_HOUR", 30),
        intake_rate_limit_global_per_hour=_env_int(
            "SAMUS_INTAKE_RATE_LIMIT_GLOBAL_PER_HOUR",
            600,
        ),
        intake_rate_limit_ttl_grace_seconds=_env_int(
            "SAMUS_INTAKE_RATE_LIMIT_TTL_GRACE_SECONDS",
            120,
        ),
        # Optional Cloudflare Turnstile CAPTCHA. Empty secret -> skipped.
        intake_captcha_secret=_env("SAMUS_INTAKE_CAPTCHA_SECRET", "") or "",
        intake_captcha_verify_url=_env(
            "SAMUS_INTAKE_CAPTCHA_VERIFY_URL",
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        )
        or "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        # Trusted-proxy hop count for X-Forwarded-For parsing.
        intake_trusted_proxy_hops=_env_int("SAMUS_INTAKE_TRUSTED_PROXY_HOPS", 1),
        # Stripe webhook production live-mode gate.
        stripe_reject_test_mode_in_production=_env_bool(
            "SAMUS_STRIPE_REJECT_TEST_MODE",
            True,
        ),
        # ==================================================================
        # v0.5/v0.6 Directed Capability Protocol layer ---------------------
        # ==================================================================
        samus_isv_consumer_enabled=_env_bool(
            "SAMUS_ISV_CONSUMER_ENABLED",
            True,
        ),
        samus_dual_channel_enabled=_env_bool(
            "SAMUS_DUAL_CHANNEL_ENABLED",
            True,
        ),
        samus_dual_channel_audit_timeout_sec=_env_int(
            "SAMUS_DUAL_CHANNEL_AUDIT_TIMEOUT_SEC",
            180,
        ),
        samus_template_registry_enabled=_env_bool(
            "SAMUS_TEMPLATE_REGISTRY_ENABLED",
            True,
        ),
        samus_major_public_key_path=_env("SAMUS_MAJOR_PUBLIC_KEY_PATH", "") or "",
        samus_signing_key_path=_env("SAMUS_SIGNING_KEY_PATH", "") or "",
        samus_optimus_dispatch_url=_env("SAMUS_OPTIMUS_DISPATCH_URL", "") or "",
        samus_major_rbl_status_url=_env("SAMUS_MAJOR_RBL_STATUS_URL", "") or "",
        # meta-governance L1 broker client (B.4) — fail-closed reservation
        # gate that meters every anthropic_messages() call. The default
        # base URL targets Anita's operator_console port on loopback;
        # disable-in-dev-when-no-key keeps pytest green out of the box.
        samus_broker_enabled=_env_bool("SAMUS_BROKER_ENABLED", True),
        samus_broker_base_url=_env(
            "SAMUS_BROKER_BASE_URL",
            "https://127.0.0.1:8420",
        )
        or "https://127.0.0.1:8420",
        samus_broker_reserve_timeout_sec=_env_float(
            "SAMUS_BROKER_RESERVE_TIMEOUT_SEC",
            1.5,
        ),
        samus_broker_release_timeout_sec=_env_float(
            "SAMUS_BROKER_RELEASE_TIMEOUT_SEC",
            1.5,
        ),
        samus_broker_disable_in_dev_when_no_key=_env_bool(
            "SAMUS_BROKER_DISABLE_IN_DEV_WHEN_NO_KEY",
            True,
        ),
        samus_broker_priority_json=_env("SAMUS_BROKER_PRIORITY_JSON", "") or "",
        samus_agora_relief_forward_enabled=_env_bool(
            "SAMUS_AGORA_RELIEF_FORWARD_ENABLED",
            False,
        ),
        samus_agora_relief_forward_staleness_sec=_env_int(
            "SAMUS_AGORA_RELIEF_FORWARD_STALENESS_SEC",
            21600,
        ),
        samus_agora_relief_forward_max_per_run=_env_int(
            "SAMUS_AGORA_RELIEF_FORWARD_MAX_PER_RUN",
            5,
        ),
        samus_agora_relief_forward_interval_sec=_env_int(
            "SAMUS_AGORA_RELIEF_FORWARD_INTERVAL_SEC",
            1800,
        ),
        # design-taste deliverable-quality subsystem (backend/taste). All
        # default-OFF; the audit is pure-stdlib + zero-LLM when armed.
        taste_audit_enabled=_env_bool("SAMUS_TASTE_AUDIT_ENABLED", False),
        taste_pdc_gate_enabled=_env_bool("SAMUS_TASTE_PDC_GATE_ENABLED", False),
        taste_audit_block_on_fail=_env_bool("SAMUS_TASTE_AUDIT_BLOCK_ON_FAIL", False),
        taste_review_floor=_env_float("SAMUS_TASTE_REVIEW_FLOOR", 0.6),
    )


def require_env(*names: str) -> None:
    """Top-level convenience: re-bootstrap and assert fields are populated."""
    bootstrap_settings().require_env(*names)


def reload_settings() -> Settings:
    """Drop the cached Settings and re-bootstrap. Re-exported from ``config``."""
    from .config import reload_settings as _reload

    return _reload()


def get_settings() -> Settings:
    """Re-export from ``config`` for callers using the legacy import path."""
    from .config import get_settings as _gs

    return _gs()
