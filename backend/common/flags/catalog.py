"""Samus flag catalog — the single source of truth for operator-toggleable
behaviour gates (Hustleforge agent §5 / E5 D13).

Samus's feature gates have historically lived as fields on the pydantic
``Settings`` model (``backend/common/config.py``), each bound from a
``SAMUS_*`` env var by ``bootstrap_settings()`` (``backend/common/settings.py``).
That remains the BOOT-TIME wiring source. This catalog ABSORBS the
ecosystem flag-catalog pattern (Anita/Darwin ``core/flags/catalog.py``) and
layers a runtime-mutable registry ON TOP of it WITHOUT changing existing
behaviour:

  * Every flag declared here mirrors a ``Settings`` field. Its
    ``default_value`` is taken FROM a default ``Settings()`` instance, so
    the catalog default can never silently diverge from the field default
    (the parity test asserts this). Its ``env_var`` records the binding so
    operators see exactly which ``SAMUS_*`` var drives the boot value.
  * Resolution order (``runtime.effective_value`` / ``is_enabled``) is:
    runtime flag override (if persisted) -> the eager settings fallback.
    Boot wiring still reads ``Settings`` directly; the catalog only adds a
    runtime flip surface, so an unset flag store changes nothing.

Categories group the flags for the operator dashboard:

* ``governance``   — caller-authz + console auth posture
* ``revenue``      — autonomous revenue / cash-engine gates
* ``website``      — website-builder workcell + content/SEO/deploy gates
* ``media``        — paid AI media (Gemini image / Veo video) gates
* ``social``       — social reel generation
* ``commerce``     — Medusa + TikTok commerce streams
* ``workflow``     — n8n workflow-automation deliverable
* ``memory``       — knowledge-graph / vector-store layers
* ``governance_dcp`` — Directed Capability Protocol (ISV/dual-channel/templates)
* ``inter_agent``  — quorum + relief forwarder
* ``taste``        — design-taste deliverable-quality subsystem
* ``observability``— shadow foresight + SEO security audit
* ``security``     — EFH semantic + voice/Twilio send gates

NOTE: this catalog declares Samus's OWN flags only — it does not import
Anita's ecosystem-wide ``sn_*`` registry. Samus's gates use the agent's
existing field names so live code that reads ``settings.<field>`` keeps
working untouched; the catalog name == the settings field name.
"""

from __future__ import annotations

from functools import lru_cache

from .store import FlagSpec, register_flag

__all__ = ["register_flag", "build_specs", "register_all"]


@lru_cache(maxsize=1)
def _defaults():
    """A default ``Settings`` instance (no env applied) = canonical defaults.

    Built lazily + cached so importing the catalog never forces a settings
    construction at import time. Constructed WITHOUT ``bootstrap_settings``
    (which reads env / .env files) so the values are the pure model
    defaults that the catalog mirrors.
    """
    from ..config import Settings

    return Settings()


def build_specs() -> list[FlagSpec]:
    """Return every Samus flag spec, defaults sourced from ``Settings()``.

    Pure (no registration side effect) so the parity test can introspect
    the specs without touching the process-global store.
    """
    s = _defaults()

    def b(name: str, env_var: str, category: str, description: str) -> FlagSpec:
        return FlagSpec(
            name=name,
            description=description,
            default_value=bool(getattr(s, name)),
            kind="bool",
            category=category,
            env_var=env_var,
        )

    specs: list[FlagSpec] = [
        # --- governance / auth posture ---------------------------------
        FlagSpec(
            name="bearer_required",
            description=(
                "Operator-console pack auth gate. When True, /api/console/* "
                "routes require Authorization: Bearer <api_token>; when False "
                "the pack's _token_dep short-circuits (loopback trust model)."
            ),
            default_value=bool(s.bearer_required),
            kind="bool",
            category="governance",
            env_var="SN_BEARER_REQUIRED",
        ),
        FlagSpec(
            name="authz_mode",
            description=(
                "Caller->callee authorization enforcement mode "
                "(off|audit|enforce). DEFAULT off = the caller-authz matrix "
                "is dormant and behaviour is pre-R-1."
            ),
            default_value=str(s.authz_mode),
            kind="str",
            category="governance",
            env_var="SAMUS_AUTHZ_MODE",
        ),

        # --- revenue / cash engine -------------------------------------
        b("cash_engine_enabled", "SAMUS_CASH_ENGINE_ENABLED", "revenue",
          "Top-level enable for the autonomous revenue pipeline (Cash "
          "Engine). It HALTS at the codex-gated review front door and never "
          "executes on its own authority."),
        b("cash_engine_live_send_enabled", "SAMUS_CASH_ENGINE_LIVE_SEND", "revenue",
          "When True the outreach stage performs a LIVE SES send rather than "
          "only composing a reviewable draft. Default OFF (+ requires "
          "ses_from_email). Fenced outward step."),
        b("prospecting_in_stack_cadence_enabled",
          "SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED", "revenue",
          "ADR-014 (ACCEPTED): run the daily prospecting discovery + CRM "
          "persist on an in-stack asyncio cadence inside the samus-prospecting "
          "container (geo-ring -> /work discover with persist_prospects=true), "
          "mirroring the gateway control-tick loop. Default OFF (dormant) — "
          "the loop does not start and the host scheduled task stays the live "
          "producer until an operator arms it."),
        b("self_supply_enabled", "SAMUS_SELF_SUPPLY_ENABLED", "revenue",
          "Starvation-aware self-supply: when the idle-drive portfolio "
          "initiates zero campaigns on a missing input (today's call list), "
          "fire the owning workcell's producer (prospecting daily_supply) "
          "from the tick itself instead of relying on a host scheduled task. "
          "Bounded 3 attempts/input/day then operator alert. Default OFF; "
          "diagnosis still reports when disarmed."),
        b("auto_stake_enabled", "SAMUS_AUTO_STAKE_ENABLED", "revenue",
          "Auto-stake sweep: control_tick promotes qualified prospects "
          "(lead_score >= threshold, owner_email present, no existing "
          "opportunity, not recently contacted) to staked opportunities "
          "with a generated stake sentence, then enqueues cash_engine_step. "
          "Default OFF — the operator is the only stake-gate until armed. "
          "Knobs: SAMUS_AUTO_STAKE_{MIN_LEAD_SCORE,MAX_PER_SWEEP,RECENT_DAYS,"
          "SCAN_LIMIT} read directly from env."),
        b("samus_reengagement_enabled", "SAMUS_REENGAGEMENT_ENABLED", "revenue",
          "Soft-no re-engagement sweep — rescans samus_call-State for "
          "prospects whose last_outcome was 'not_interested' past the "
          "cooldown (SAMUS_REENGAGEMENT_COOLDOWN_DAYS, default 30) and "
          "fires a cash_engine review with trigger_source='reengagement', "
          "routing the outreach stage through a promotional template. "
          "Default OFF (dormant) — the sweep is a no-op until armed. "
          "Live-send still gated by cash_engine_live_send_enabled."),

        # --- website builder workcell ----------------------------------
        b("website_builder_enabled", "SAMUS_WEBSITE_BUILDER_ENABLED", "website",
          "Master enable for the website-builder fulfillment workcell. "
          "Machinery-on; outward/credentialed steps stay separately fenced."),
        b("website_autonomous_enabled", "SAMUS_WEBSITE_AUTONOMOUS_ENABLED", "website",
          "Lifts the per-stage operator-approval gate so a proven sequence "
          "runs unattended. Default OFF = supervised walk-through."),
        b("website_live_publish_enabled", "SAMUS_WEBSITE_LIVE_PUBLISH", "website",
          "Second key the irreversible publish/deliver stages need. Default "
          "OFF = build runs end-to-end but never goes live or contacts the "
          "customer."),
        b("website_content_generation_enabled",
          "SAMUS_WEBSITE_CONTENT_GENERATION_ENABLED", "website",
          "Taste-governed content generation in the `generate` stage. Default "
          "ON; degrades to deterministic on-brand copy with no API key / on "
          "budget-cap / on failure (taste-audited fail-closed before use)."),
        b("website_headless_enabled", "SAMUS_WEBSITE_HEADLESS_ENABLED", "website",
          "Self-contained taste-governed static-site generator. Dormant by "
          "default; the artifact is generated on demand."),
        b("website_design_intelligence_enabled",
          "SAMUS_WEBSITE_DESIGN_INTELLIGENCE_ENABLED", "website",
          "Industry design-intelligence palette/font/style mapping applied by "
          "the headless generator. Pure local data, $0; default ON."),
        b("website_legal_pages_enabled", "SAMUS_WEBSITE_LEGAL_PAGES_ENABLED", "website",
          "Jurisdiction-aware legal sub-pages (Privacy/Terms). Default ON; "
          "templates are not legal advice."),
        b("website_multipage_enabled", "SAMUS_WEBSITE_MULTIPAGE_ENABLED", "website",
          "Marketing sub-pages (Services/Gallery/FAQ/Reviews/area) for an "
          "agency-grade multi-page site. Default ON."),
        b("website_deploy_enabled", "SAMUS_WEBSITE_DEPLOY_ENABLED", "website",
          "Deploys the generated headless site (Cloudflare Pages). Dormant + "
          "keyless-safe: nothing deploys without a token/account/project."),
        b("website_demo_sites_enabled", "SAMUS_WEBSITE_DEMO_SITES_ENABLED", "website",
          "Operator-prompted cold-call demo-site builder (one pre-built demo "
          "site per no-website prospect on the morning call list, hard $ "
          "ceiling per run). Default OFF — operator fires it explicitly via "
          "scripts/Build-DemoSites.ps1; never scheduled."),
        b("website_asset_isolation_strict", "SAMUS_WEBSITE_ASSET_ISOLATION_STRICT", "website",
          "STRICT mode for the per-client brand-asset isolation gate: strip "
          "remote refs outside the fonts/Tailwind allowlist + the client's "
          "own domain. The no-cross-client-reuse gate always runs; default "
          "OFF keeps unknown remotes as warnings."),
        b("website_seo_enrich_enabled", "SAMUS_WEBSITE_SEO_ENRICH_ENABLED", "website",
          "Build the structured-data + per-page meta SEO package. Default ON."),
        FlagSpec(
            name="website_seo_gate",
            description=(
                "Deliver-stage SEO posture (off|audit|enforce). enforce parks "
                "a sub-par site until it passes."
            ),
            default_value=str(s.website_seo_gate),
            kind="str",
            category="website",
            env_var="SAMUS_WEBSITE_SEO_GATE",
        ),
        FlagSpec(
            name="website_security_gate",
            description=(
                "Deliver-stage passive-security posture (off|audit|enforce). "
                "On Wix subdomains use audit (findings are Wix-platform headers)."
            ),
            default_value=str(s.website_security_gate),
            kind="str",
            category="website",
            env_var="SAMUS_WEBSITE_SECURITY_GATE",
        ),

        # --- paid AI media ---------------------------------------------
        b("website_media_gen_enabled", "SAMUS_WEBSITE_MEDIA_GEN_ENABLED", "media",
          "Headless taste-governed Gemini image generation for the website "
          "deliverable. PAID; metered by media_budget (separate from the "
          "Anthropic $1/day cap). Dormant + keyless-safe by default."),
        b("website_media_video_enabled", "SAMUS_WEBSITE_MEDIA_VIDEO_ENABLED", "media",
          "Full-motion hero video (Veo). Extra-gated: needs this flag AND, "
          "per video, operator approval. The expensive part — default OFF."),
        b("media_video_requires_approval", "SAMUS_MEDIA_VIDEO_REQUIRES_APPROVAL", "media",
          "Require per-video operator approval before any Veo spend. Default "
          "ON (belt-and-braces over the daily $ cap)."),

        # --- social reel ----------------------------------------------
        b("social_reel_enabled", "SAMUS_SOCIAL_REEL_ENABLED", "social",
          "Narrated/subtitled vertical social-reel pipeline. Dormant + "
          "fail-closed: spends nothing and degrades cleanly when heavy deps "
          "are absent."),
        b("social_reel_video_enabled", "SAMUS_SOCIAL_REEL_VIDEO_ENABLED", "social",
          "Per-shot Veo motion in reels. Extra-gated (flag + per-reel "
          "approval); a multi-shot Veo reel can exceed the cap, so default OFF."),

        # --- commerce streams ------------------------------------------
        b("commerce_medusa_enabled", "SAMUS_COMMERCE_MEDUSA_ENABLED", "commerce",
          "Operator-run Medusa commerce client (products income stream). "
          "Dormant + keyless-safe (no-op without base_url/token)."),
        b("tiktok_content_posting_enabled", "SAMUS_TIKTOK_CONTENT_POSTING_ENABLED",
          "commerce",
          "TikTok content posting. Dormant + dry-run by default; needs an "
          "operator-approved app."),
        b("tiktok_shop_enabled", "SAMUS_TIKTOK_SHOP_ENABLED", "commerce",
          "TikTok Shop Seller API (selling). Dormant by default; needs an "
          "approved seller app."),
        b("tiktok_dry_run", "SAMUS_TIKTOK_DRY_RUN", "commerce",
          "Belt-and-braces: never posts/sells live while True (default ON)."),

        # --- workflow automation deliverable ---------------------------
        b("workflow_n8n_deliverable_enabled", "SAMUS_WORKFLOW_N8N_DELIVERABLE_ENABLED",
          "workflow",
          "Compile the TaskPlan into deployable n8n workflow JSON + runbook. "
          "Pure/offline/$0; default ON."),
        b("workflow_n8n_llm_enrich", "SAMUS_WORKFLOW_N8N_LLM_ENRICH", "workflow",
          "Budget-gated LLM parameter-enrichment pass over the workflow. "
          "Dormant by default."),
        b("workflow_n8n_deploy_enabled", "SAMUS_WORKFLOW_N8N_DEPLOY_ENABLED", "workflow",
          "Live POST of the generated workflow to an n8n instance. Dormant by "
          "default."),
        b("workflow_n8n_dry_run", "SAMUS_WORKFLOW_N8N_DRY_RUN", "workflow",
          "Deploy dry-run gate (belt-and-braces). Default ON."),

        # --- memory layers ---------------------------------------------
        FlagSpec(
            name="kg_tier_mode",
            description=(
                "Knowledge-graph private/hivemind tier representation "
                "(off|label). off = no tier property written (local stack "
                "unchanged); label = Cloud Run two-tier split."
            ),
            default_value=str(s.kg_tier_mode),
            kind="str",
            category="memory",
            env_var="SAMUS_KG_TIER_MODE",
        ),
        b("crm_hivemind_projection_enabled", "SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED",
          "memory",
          "Project the CRM prospect->contact->opportunity sub-graph into "
          "Neo4j on opportunity writes. Default OFF; no-ops cleanly when "
          "Neo4j is unreachable."),
        b("vector_store_enabled", "SAMUS_VECTOR_STORE_ENABLED", "memory",
          "Bounded ChromaDB semantic-retrieval layer. Default OFF (no-op, "
          "never imports chromadb); fails closed when the backend is absent."),

        # --- Directed Capability Protocol (governance plane) -----------
        b("samus_isv_consumer_enabled", "SAMUS_ISV_CONSUMER_ENABLED", "governance_dcp",
          "Consume Major-signed InitialStateVectors and gate commercial "
          "actions to ISV scope. Default ON."),
        b("samus_dual_channel_enabled", "SAMUS_DUAL_CHANNEL_ENABLED", "governance_dcp",
          "Require a synchronously-acked ActionTakenEnvelope to Major before "
          "every commercial commit lands. Default ON."),
        b("samus_template_registry_enabled", "SAMUS_TEMPLATE_REGISTRY_ENABLED",
          "governance_dcp",
          "Pre-approved action templates that bypass per-instance EFH "
          "evaluation for Samus's three signed templates. Default ON."),

        # --- inter-agent: quorum + relief ------------------------------
        b("samus_quorum_publish_enabled", "SAMUS_QUORUM_PUBLISH_ENABLED", "inter_agent",
          "Publish EFH vetoes + PDC BLOCK/ESCALATE findings to the shared "
          "quorum hub (advisory, never blocking). Dormant by default."),
        b("samus_quorum_voting_enabled", "SAMUS_QUORUM_VOTING_ENABLED", "inter_agent",
          "Enable the POST /quorum/vote VOTER endpoint. Default OFF — the "
          "route returns 503 before doing any work while unset."),
        b("samus_pdc_observe_enabled", "SAMUS_PDC_OBSERVE_ENABLED", "inter_agent",
          "Run the PDC composite as a parallel observer after EFH passes. "
          "Informational; never refuses the commit. Default OFF."),
        b("samus_agora_relief_forward_enabled", "SAMUS_AGORA_RELIEF_FORWARD_ENABLED",
          "inter_agent",
          "Mirror Samus's own stale operator-pending deals to Anita's relief "
          "intake for tribal deliberation while the operator is away. "
          "Resolves nothing; dormant by default."),
        b("samus_broker_enabled", "SAMUS_BROKER_ENABLED", "inter_agent",
          "Route every Anthropic call through Anita's L1 resource broker "
          "(fail-closed reservation gate). Default ON; disabled at call time "
          "in dev when no inter-agent HMAC key is configured."),

        # --- design-taste subsystem ------------------------------------
        b("taste_audit_enabled", "SAMUS_TASTE_AUDIT_ENABLED", "taste",
          "Run the deterministic zero-LLM taste Pre-Flight audit over "
          "deliverables. Default OFF."),
        b("taste_pdc_gate_enabled", "SAMUS_TASTE_PDC_GATE_ENABLED", "taste",
          "Fold the taste verdict into the PDC composite as a soft (REVIEW) "
          "signal. Never BLOCKs on taste alone. Default OFF."),
        b("taste_audit_block_on_fail", "SAMUS_TASTE_AUDIT_BLOCK_ON_FAIL", "taste",
          "Allow a hard-fail taste violation to mark the deliverable verdict "
          "as failing so the producer can refuse to ship. Default OFF "
          "(warn-only)."),

        # --- observability (shadow) ------------------------------------
        b("foresight_enabled", "SAMUS_FORESIGHT_ENABLED", "observability",
          "ECO-ADR-0003 §7 revenue foresight 'Cone of Plausibility' shadow "
          "report on the finance snapshot. Observation-only — never changes "
          "any finance figure or decision. ARMED (default ON)."),
        b("seo_security_audit_enabled", "SAMUS_SEO_SECURITY_AUDIT_ENABLED",
          "observability",
          "Passive security & trust-posture audit appended to the SEO "
          "customer report (benign GETs + one TLS handshake, zero-LLM). "
          "Default ON."),

        # --- security gates --------------------------------------------
        b("samus_efh_semantic_enabled", "SAMUS_EFH_SEMANTIC", "security",
          "Augment the EFH deterministic keyword heuristic with an LLM "
          "semantic classifier that can only ADD axiom breaches. Default ON "
          "(armed HOTL Tranche 1, 2026-07-05); fail-open so the heuristic "
          "floor stays load-bearing."),
        b("outreach_voice_send_enabled", "SAMUS_OUTREACH_VOICE_SEND", "security",
          "Gate the outreach call/voicemail send path through the real Vapi "
          "provider. Default OFF — an outbound dial returns a degraded "
          "receipt until armed."),
        b("stripe_reject_test_mode_in_production", "SAMUS_STRIPE_REJECT_TEST_MODE",
          "security",
          "Reject Stripe events with livemode=false in a production env so a "
          "test-mode event cannot advance a customer to paid. Default ON."),
        b("intake_rate_limit_enabled", "SAMUS_INTAKE_RATE_LIMIT_ENABLED", "security",
          "Per-source-IP fixed-window limiter on POST /intake/onboarding "
          "(DynamoDB-backed, fails OPEN). bootstrap default ON for real "
          "deployments."),

        # --- CORE key/secret management (E5 D15) -----------------------
        b("samus_secret_rotation_enabled", "SAMUS_SECRET_ROTATION_ENABLED",
          "security",
          "Arm the CORE key-vault rotation cycle (backend/common/keys). When "
          "on, SecretRotationManager.tick bumps the HKDF vault generation on "
          "the configured interval + records a fingerprint backup; it never "
          "rewrites the operator's DPAPI .cred store. Default OFF (dormant)."),

        # --- network isolation kill-switch ----------------------------
        b("outbound_calls_enabled", "SAMUS_OUTBOUND_CALLS_ENABLED", "security",
          "Master kill-switch for ALL outbound HTTP calls to external services "
          "(Stripe, Gemini, Wix, SEO/prospecting fetches, etc.). Default ON. "
          "Set False to block all external egress without stopping Samus. "
          "Inter-agent calls to localhost are NOT affected."),
    ]
    return specs


def register_all() -> None:
    """Register every Samus flag spec on the process-global store.

    Called at boot AFTER ``init_default_store``. Idempotent (the store's
    write-once-on-shape guard makes repeated imports/boots safe).
    """
    for spec in build_specs():
        register_flag(spec)
