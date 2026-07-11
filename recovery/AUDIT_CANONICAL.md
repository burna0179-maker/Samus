# Recovery → Canonical Audit & Fill-In Plan

**Target:** `Canon/Architecture_HustleAgent_v1.md` (was `Samus/Architecture_SamusCanonical_v1.md` at v1.3.0; now v1.5.0, moved to `Canon/` and renamed 2026-05-13) + reference impl at `C:\Users\PC\Downloads\samus_v1.0\samus_v1\`
**Source:** 60 recovery files in `D:\Hustleforge\Samus\recovery\`
**Rename map applied:** v3 cause-and-effect addendum (~210 renames)

---

## 1. Reference impl scaffold survey

The canonical reference impl is **almost entirely scaffold** — only 4 files contain actual content; every other module is `__init__.py` only.

### Filled (4 files)
| Path | Purpose |
|---|---|
| `backend/core/configuration/settings/base.py` | Pydantic `Settings` w/ all `sn_*` flags |
| `backend/core/mutation/scope/decorator.py` | `@mutation_scope` + `MutationType`/`Risk` enums + `ScopeRegistry` |
| `backend/core/mutation/scope/static_analyzer.py` | Boot-time AST scan |
| `backend/standard/context/tiers/long_term.py` | `LongTermTier` JSONL-backed facts store |

### Filled (header files)
| Path | Purpose |
|---|---|
| `backend/core/protocols/__init__.py` | All 11 Protocol classes (Application/Orchestration/Agent/Context/Data/ModelExtended/Observability/SecurityExtended/SelfHealExtended/InterAgent/Autonomy) |
| `backend/core/null_objects/__init__.py` | All 11 Null* implementations + import-time type assertions |
| `backend/packs/*/pack.json` | 5 pack manifests (business/content/research/security_tools/population) |
| `profiles/*.json` | 5 profiles (minimal/standard/research/business/full) |

### Empty scaffold (everything else)
**CORE empty scaffolds (12 directories):** `bootstrap`, `configuration/flags`, `governance/constitution`, `governance/policy`, `identity`, `infrastructure/filesystem`, `infrastructure/process`, `manifest`, `model/backends`, `mutation/interception`, `mutation/ledger`, `mutation/permit`, `mutation/pipeline`, `null_objects` (file present but no content beyond header), `security/authority`, `security/posture`, `self_heal`

**STANDARD empty scaffolds (28 directories across 12 planes):**
- `agents/base`, `agents/cognitive`, `agents/cognitive/stages`, `agents/parliament`, `agents/reasoning`
- `application/api`, `application/middleware`, `application/schemas`
- `autonomy/autonomic_loop`, `autonomy/evolution`
- `context/bus`, `context/tiers` (only `long_term.py` filled)
- `data/backends`, `data/ledgers`
- `inter_agent/discovery`, `inter_agent/identity`, `inter_agent/negotiation`, `inter_agent/trust`
- `model_extended/backends`
- `observability/health`, `observability/logging`, `observability/metrics`
- `orchestration/colony_tick`, `orchestration/patterns`
- `persona_overlays/affect`, `persona_overlays/context`, `persona_overlays/roles`, `persona_overlays/voice`
- `security_extended/anomaly`, `security_extended/baselines`, `security_extended/defense_in_depth`
- `self_heal_extended/federation`, `self_heal_extended/supervisor`, `self_heal_extended/tick_healer`

**PACKS empty scaffolds (all 5):** business/api, business/integrations, business/pods, content/api, content/pods, research/api, security_tools/api, security_tools/pods, population (entire pack)

**Total: ~50 scaffolded directories awaiting fill-in.**

---

## 2. Canonical adapter pattern

Before mapping recovery → canonical, every recovery file needs adaptation to canonical's conventions. The recovery files originate from three different iterations with different patterns; canonical uses a strict single pattern.

### Pattern checklist (apply to every fill-in)

| Recovery convention | Canonical convention | Action |
|---|---|---|
| `class X(Component, component_id="...")` (F:\\Samus) | Plain `class X:` | **Strip `Component` inheritance** |
| `class X(Component): @classmethod build(deps)` | Module-level `get_x()` singleton + `_instance` global | **Replace DI with module singleton** |
| `getattr(cfg, "hf_persona_*")` | `get_settings().sn_persona_*` | **Rename flags `hf_*` → `sn_*`** |
| `def cycle(self, ...)` (sync) | `async def cycle(self, ...) -> ...` | **Make async where Protocol mandates** |
| Returns plain dict | Returns `HealthReport` / typed dataclass | **Add typed returns** |
| No `__plane__` marker | `__plane__ = "<plane>"` at module top | **Add plane marker** |
| Raw file I/O | `from backend.core.infrastructure.filesystem import get_paths` | **Route through filesystem facade** |
| `import os; os.environ.get(...)` | `from backend.core.configuration.settings import get_settings` | **No direct env access** |
| Custom mutation tracking | `@mutation_scope(state_paths=..., mutation_types={...})` | **Decorate with canonical scope** |
| `logging.getLogger("...")` (allowed) | Same — keep as-is | OK |

### Canonical module template

```python
"""<one-line module purpose>.

<longer description>
"""
from __future__ import annotations

import <stdlib>
from typing import Any

from backend.core.configuration.settings import get_settings
from backend.core.infrastructure.filesystem import get_paths   # if disk I/O
from backend.core.mutation.scope import mutation_scope, MutationType   # if mutates state
from backend.core.protocols import HealthReport, HealthStatus

__plane__ = "<plane_name>"


@mutation_scope(
    state_paths=("data/<plane>/<sub>/**",),
    mutation_types={MutationType.STATE},
)
class XXX:
    plane_name = "<plane>:<sub>"

    def __init__(self, ...) -> None:
        ...

    async def <protocol_method>(self, ...) -> ...:
        ...

    def health(self) -> HealthReport:
        return HealthReport(status=HealthStatus.OK, detail="...", metrics={...})


_instance: XXX | None = None


def get_xxx() -> XXX:
    global _instance
    if _instance is None:
        _instance = XXX()
    return _instance
```

### Settings additions needed
Many recovery files reference settings that don't exist in canonical's `base.py`. The audit will list each one for inclusion in the canonical settings extension.

---

## 3. Recovery → canonical mapping table (v3 renames applied)

### CORE tier fills

| Recovery file | Canonical home | v3 rename note |
|---|---|---|
| `security_layer_restructure.py` | **`backend/core/security/`** new submodules: `signing.py`, `envelope.py`, `key_registry.py`, `canonical_json.py`, `errors.py` | None (additive) |
| `governance_drift_detection.py` | **`backend/core/governance/drift_detection.py`** | `csa/` → `governance/` already applied |
| `master_framework_mutation_lifecycle.py` (9-stage pipeline) | **`backend/core/mutation/pipeline/execute.py`** — adapt 9-stage to canonical 8-stage OR document the 9th (`scaling_adjust`) as separate observability concern | Pipeline canonical: scope→authority→snapshot→simulate→approve→apply→verify→commit. Master Framework adds Stage 8 feedback + Stage 9 scaling. |
| `governance_parliament.py` | **`backend/standard/agents/parliament/quorum_voter.py`** | **RENAME**: `parliament` directory keeps name; file is `quorum_voter.py` per v3 |
| `three_plane_authority_model.md` (concept) | Inform **`backend/core/security/authority/`** filling + **`backend/core/governance/constitution/`** | Foundational principle — informs design, not direct port |
| `autonomy_tier_model.md` (concept) | Inform **`backend/core/security/authority/tiers.py`** + **`backend/standard/autonomy/autonomic_loop/`** | Aligns with canonical's `AuthorityTier` enum |

### STANDARD tier fills (12 planes)

#### Plane: `application` (was at canonical §6 — application middleware stack)
| Recovery file | Canonical home |
|---|---|
| `stripe_webhook_receiver.py` (per-route gating pattern) | **`backend/standard/application/middleware/replay_protection.py`** + **`backend/standard/application/middleware/webhook_gate.py`** + **canonical Annex_RouteInventory** for `/webhooks/*` |
| `samus_ops_gateway.py` + `samus_ops_schema.sql` | Phone-ops surface — does NOT belong in canonical core. **Move to packs/business/** OR keep as an external operator tool, NOT in canonical. |
| `hf_smms_dashboard.php` | External WordPress plugin — NOT part of canonical at all. **Set aside.** |

#### Plane: `agents` (cognitive Stage 1-9 loop, base pods, parliament, reasoning)
| Recovery file | Canonical home |
|---|---|
| `persona_system_v2_frontier.py` | **`backend/standard/agents/cognitive/stages/persona_frame.py`** — Stage 1 of canonical 9-stage CognitiveLoop |
| `social_adapter_v2.py` | **`backend/standard/agents/cognitive/stages/social_adapter.py`** — supports Stage 6 ACT (rhetorical posture) |
| `style_adapter_v2.py` | **`backend/standard/agents/cognitive/stages/style_adapter.py`** — Stage 7 REFLECT → Stage 8 PERSIST formatting |
| `meta_cognition_engine.py` (wrapper pattern) | Inform **`backend/standard/agents/cognitive/loop.py`** as the 9-stage orchestrator that internally uses the wrapper concept (perception → core → reflection) |
| `governance_parliament.py` | **`backend/standard/agents/parliament/quorum_voter.py`** (v3 rename) — Stage 5 DECIDE multi-analyst review |
| `autonomous_closer.py` | **`backend/packs/business/pods/sales/closer_fsm.py`** — domain-specific, belongs in business pack |
| `realtime_adaptive_agent.py` | **`backend/packs/business/pods/sales/adaptive_tone.py`** — domain-specific |
| `deal_scoring_agent.py` | **`backend/packs/business/pods/lead_scorer.py`** (canonical declares `lead_scorer` pod in business/pack.json) |
| `sales_pipeline_nodes.py` | **`backend/packs/business/pods/sales/conversation_graph.py`** |
| `callsheet_product_registry.py` | **`backend/packs/business/pods/sales/product_registry.py`** |
| `crm_feedback_engine.py` | **`backend/packs/business/pods/sales/feedback_metrics.py`** — but flag persistence-deferred |
| `vapi_sales_agent_config.md` | **`backend/packs/business/pods/sales/vapi_agent.md`** + integration in `email_outreach` pod |
| `ai_receptionist_pod.py` | **`backend/packs/business/pods/receptionist.py`** — NEW pod (extend business/pack.json `provides_pods` list) |
| `alfred_document_agent.py` | **`backend/packs/business/pods/document_generator.py`** — extend business/pack.json |

#### Plane: `autonomy` (autonomic_loop + evolution)
| Recovery file | Canonical home |
|---|---|
| `strategy_engine.py` | **`backend/standard/autonomy/autonomic_loop/decisions.py`** — verdict-style routing fits canonical's `mapek_tier1/2/3` Protocol |
| `campaign_optimizer.py` | **`backend/packs/business/pods/portfolio_optimizer.py`** — domain, not generic autonomy |
| `llm_portfolio_manager.py` | **`backend/packs/business/pods/llm_strategist.py`** — domain |
| `campaign_bandit.py` | **`backend/standard/autonomy/evolution/bandit.py`** — UCB1 is generic enough to live in canonical evolution plane |
| `automation_factory_integration.py` | **`backend/standard/self_heal_extended/supervisor/health_assessment.py`** — cross-module health is supervisor responsibility |
| `persona_memory_v2.py` | **`backend/standard/persona_overlays/<sub>/persistence.py`** OR **`backend/standard/context/tiers/persona_tier.py`** — persona memory has dual home; canonical leans toward context.tiers because §6 says persona_overlays are dimensions (affect/context/roles/voice) not memory stores |

#### Plane: `context` (bus + tiers)
| Recovery file | Canonical home |
|---|---|
| `seed_knowledge_v2.py` + `knowledge_ingest_pod.py` | **`backend/standard/context/tiers/semantic.py`** (semantic memory tier) + **`backend/standard/context/bus/ingest.py`** — fills the `[EXPANDS §17 roadmap]` vector-backed semantic memory |
| `smms_chatgpt_ingestion.py` | **`backend/packs/content/pods/scraper/chatgpt_export.py`** — content pack already declares scraper pod |
| `archive_reasoning_pipeline.md` (concept) | Inform **`backend/standard/context/bus/archive_scanner.py`** + **`backend/standard/autonomy/evolution/proposal_generator.py`** |

#### Plane: `data` (backends + ledgers)
| Recovery file | Canonical home |
|---|---|
| `forensic_ledger_chain_model.md` | **`backend/standard/data/ledgers/integrity.py`** + **`backend/standard/data/ledgers/egress.py`** + **`backend/standard/data/ledgers/incident.py`** + **`backend/standard/data/ledgers/_chain.py`** (shared HMAC chain primitives) |
| `prospect_schema.py` (7-table CRM) | **`backend/packs/business/data/crm_tables.py`** — pack-level data schema |

#### Plane: `inter_agent` (discovery + identity + negotiation + trust)
| Recovery file | Canonical home |
|---|---|
| `auth0_hybrid_identity_strategy.md` (concept) | Inform **`backend/standard/inter_agent/identity/edge_auth.py`** (Auth0 edge) + canonical Annex_SecurityInvariants extension |
| `proto_versioning_strategy.md` | **`backend/standard/inter_agent/negotiation/proto_versions.py`** — version-routing primitive |
| `security_layer_restructure.py` envelope module | **`backend/standard/inter_agent/identity/envelope.py`** — CloudEvents + Ed25519 signing |

#### Plane: `model_extended`
| Recovery file | Canonical home |
|---|---|
| `offline_voice_hud_stack.md` (Whisper+Ollama+Piper) | **`backend/standard/model_extended/backends/ollama.py`** + **`backend/standard/model_extended/backends/whisper_stt.py`** + **`backend/standard/model_extended/backends/piper_tts.py`** |

#### Plane: `observability`
| Recovery file | Canonical home |
|---|---|
| `alloy_loki_observability.md` (concept) | Inform **`backend/standard/observability/logging/alloy_shipper.py`** + Loki label discipline in **`backend/standard/observability/logging/json_formatter.py`** |
| `samus_ops_schema.sql` (events table) | Audit event shape informs **`backend/standard/observability/health/events.py`** |
| `system_maturity_metrics.md` (10-domain SMI) | **`backend/standard/observability/metrics/maturity_index.py`** — composite scoring module |

#### Plane: `orchestration` (colony_tick + patterns)
| Recovery file | Canonical home |
|---|---|
| `fulfillment_worker_v2.py` (DAG dispatch) | **`backend/standard/orchestration/patterns/dag_dispatcher.py`** (generic) + **`backend/packs/business/pods/fulfillment_engine.py`** (business-specific 5-action surface) |
| `queue_app_dlq_resolver.py` | **`backend/standard/orchestration/patterns/dlq_resolver.py`** — RedrivePolicy-truth resolution |
| `fixed_scope_template_pipeline.py` | **`backend/standard/orchestration/patterns/template_pipeline.py`** (generic 5-stage) + **`backend/packs/content/pods/template_intelligence.py`** (learning engine, domain) |
| **NOTE**: `orchestration/colony_tick/` directory is the v3-RENAME-TARGET → **`orchestration/ensemble_tick/`**. The directory hasn't been renamed in the reference impl yet; new code should land in `ensemble_tick/` after rename, or in `colony_tick/` with shim during transition. |

#### Plane: `persona_overlays` (affect + context + roles + voice)
| Recovery file | Canonical home |
|---|---|
| `persona_system_v2_frontier.py` (`PersonaFrame` + `OperatingMode` enum) | **Split**: `OperatingMode` enum → `backend/standard/persona_overlays/context/operating_mode.py` ; `PersonaFrame` → `backend/standard/agents/cognitive/stages/persona_frame.py` |
| `social_adapter_v2.py` | **`backend/standard/persona_overlays/voice/social_adapter.py`** — voice/style dimension |
| `style_adapter_v2.py` | **`backend/standard/persona_overlays/voice/style_adapter.py`** |
| `nova_hart_persona_spec.md` | **`backend/standard/persona_overlays/roles/nova_hart.json`** — role overlay configuration |

#### Plane: `security_extended` (DiD + anomaly + baselines)
| Recovery file | Canonical home |
|---|---|
| `system_invariant_defense_scenarios.md` | **`backend/standard/security_extended/anomaly/invariant_scenarios.py`** — SYS_COMPONENT_DESYNC / SYS_DEGRADED_COMPOUND / SYS_MULTISIGNAL_CONFLICT scenarios |
| `drift_simulation_harness.py` | **`backend/standard/security_extended/baselines/drift_harness.py`** — adversarial injection sandbox |
| `governance_drift_detection.py` | **CORE-level** (already mapped above to `backend/core/governance/drift_detection.py`) but its scoring math `behavioral_drift`/`heuristic_drift`/`schema_drift` functions are reusable in **`backend/standard/security_extended/baselines/drift_scoring.py`** |
| `autonomous_agentic_enforcement_prompt.md` | Set aside — meta-prompt, not code. Inform **`docs/enforcement_policy.md`** |

#### Plane: `self_heal_extended` (federation + supervisor + tick_healer)
| Recovery file | Canonical home |
|---|---|
| `automation_factory_integration.py` | **`backend/standard/self_heal_extended/supervisor/cross_module_health.py`** (already noted under autonomy) |
| `preflight_checks.py` | **`backend/standard/self_heal_extended/supervisor/preflight.py`** — but kept also as operator CLI script |

### PACKS tier fills

#### Pack: `business` (extend pack.json `provides_pods`)
Canonical declares: `["sales", "revenue", "lead_scorer", "email_outreach"]`. Add via recovery:
- `receptionist` (from `ai_receptionist_pod.py`)
- `document_generator` (from `alfred_document_agent.py`)
- `prospect` (from `prospect_worker.py`)
- `fulfillment_engine` (from `fulfillment_worker_v2.py` DAG)
- `portfolio_optimizer` + `llm_strategist` (from chat 02)

#### Pack: `content`
Canonical declares: `["rag", "scraper", "content_gen"]`. Map:
- `content_gen` ← `nova_hart_persona_spec.md` + `comfyui_influencer_workflow.md` as configuration
- `scraper` ← `prospect_worker.py` crawl-guardrails + `smms_chatgpt_ingestion.py` parser
- `rag` ← `seed_knowledge_v2.py` + `knowledge_ingest_pod.py` semantic-store integration

#### Pack: `population` (v3 rename of `civilization`)
- `agent_civilization_blueprint.md` — full design doc → **`backend/packs/population/blueprint.md`** + pod skeletons under `backend/packs/population/pods/`:
  - `sovereign.py`, `economy.py`, `marketplace.py`, `research_cluster.py`, `simulation.py`

#### Pack: `security_tools`
Canonical declares: `["pentest", "vuln_scanner", "network_probe"]`. Map:
- Recovery `system_invariant_defense_scenarios.md` adversarial-scenario library is shared between `pentest` pod and `standard/security_extended/anomaly`

---

## 4. Recovery surplus (no canonical home — set aside or external)

| Recovery file | Disposition |
|---|---|
| `hf_smms_dashboard.php` | External WP plugin — keep outside canonical |
| `samus_hud_overlay.html` | External OBS overlay — keep outside canonical |
| `offline_voice_hud_stack.md` (HUD parts) | Operator UI — keep external; only Ollama/Whisper/Piper backends become canonical `model_extended/backends/` |
| `seo_module_system_prompt.md` | Pack-level system prompt; lives in `backend/packs/business/prompts/seo.md` |
| `comfyui_influencer_workflow.md` | Creative pipeline doc; lives in `backend/packs/content/docs/` |
| `smms_global_automation_project.md` | Umbrella vision doc; lives outside canonical (operator-facing roadmap) |
| `production_agent_stack_blueprint.md` | Reference architecture doc; lives in `docs/reference_stack.md` |
| `docker_workcell_strategy.md` | Deployment doc; lives in `docs/deployment.md` |
| `organizational_planes.md` | Operator-org doc; lives in `docs/operator_orientation.md` |
| `samus_deploy26_inventory.md` | Snapshot of competing impl; archive in `recovery/` only |
| `autonomous_agentic_enforcement_prompt.md` | Meta-prompt; lives in `docs/enforcement_policy.md` |
| `onboarding_form_schema.py` | hustleforge.tech site config — outside canonical |
| `run_seo_client.py` | Operator CLI script — lives in `scripts/run_seo_client.py` (matches reference impl `scripts/` dir) |

---

## 5. Gaps: canonical scaffolds with no recovery candidate

The following canonical scaffolds **have no recovery file to draw from**. They need fresh implementation following canonical's documented design:

### CORE (8 gaps)
- `core/bootstrap/__init__.py` — 7-phase boot orchestrator (per canonical §3)
- `core/configuration/flags/` — runtime feature-flag system (settings holds typed env; flags is a separate concern per canonical §5)
- `core/governance/constitution/` — Tier-1 hard rules (regex-based per canonical §5; not LLM)
- `core/governance/policy/` — PolicyDecisionPoint (PDP) — Layer 3 of DiD per canonical §7
- `core/identity/` — Tier-1 immutable persona loader (`data/identity/values.json` per canonical §5)
- `core/infrastructure/filesystem/` — `get_paths()` facade + atomic I/O (referenced by every other module — high-priority gap)
- `core/infrastructure/process/` — process lifecycle primitives (start/signal/reap)
- `core/manifest/` — profile + pack resolver w/ Kahn topological sort (per canonical §4 — this is what makes the manifest system work)
- `core/model/backends/local_echo.py` — deterministic fallback model (CORE-required per canonical §5)
- `core/mutation/interception/` — `sys.audit` runtime hook (per canonical §8 stage 4 — observation/block mode)
- `core/mutation/ledger/` — hash-chained JSONL ledger w/ rotating HMAC keys (per canonical §6 data plane spec)
- `core/mutation/permit/` — 7-state `PermitState` lifecycle issue/verify/sweep (per canonical §8)
- `core/mutation/pipeline/execute.py` — 8-stage pipeline orchestrator (recovery's `master_framework_mutation_lifecycle.py` is the 9-stage competitor — needs adaptation back to 8 stages OR explicit 9-stage extension)
- `core/security/authority/` — `AuthorityTier` enum + `SecurityContext` + `TrustTier` (per canonical §10)
- `core/security/posture/` — `declare_posture()` registry (per canonical §5)
- `core/self_heal/` — `SelfHealRegistry` (per canonical §9 — coverage gate)

### STANDARD (multiple gaps)
- `standard/agents/base/` — abstract pod classes + mixins (per canonical §6)
- `standard/agents/cognitive/loop.py` — 9-stage CognitiveLoop orchestrator (per canonical §6 plane operational details)
- `standard/agents/cognitive/stages/` — individual stage implementations beyond what recovery provides
- `standard/agents/reasoning/` — non-LLM logic/verification pods
- `standard/application/api/` — `/health` `/ready` `/identity` `/sanity` `/api/chat` `/api/admin/*` (per canonical §14)
- `standard/application/middleware/` — middleware stack ordering per canonical §6 (SecurityHeaders → APIVersion → ReplayProtection → TraceMetrics → CORS)
- `standard/application/schemas/` — Pydantic request/response models
- `standard/autonomy/autonomic_loop/` — MAPE-K Tier 1/2/3 tick implementations (per canonical AutonomyPlane protocol)
- `standard/autonomy/evolution/` — proposal generator (was Darwin's CSA scope; lives as STANDARD plane in canonical reference per canonical §6 sub-modules)
- `standard/context/bus/` — internal event bus for context propagation (Sapphire's EventBus pattern per canonical §6)
- `standard/context/tiers/working.py`, `episodic.py`, `semantic.py` — only `long_term.py` exists
- `standard/data/backends/` — pluggable storage (file/JSON/future SQLite-vec)
- `standard/data/ledgers/` — multiple ledger types sharing rotating-HMAC chain (recovery `forensic_ledger_chain_model.md` informs this)
- `standard/inter_agent/discovery/` — peer discovery scaffold (canonical §17 corrections: "scaffolded, not deferred")
- `standard/inter_agent/identity/` — Ed25519 keypairs + thumbprints
- `standard/inter_agent/negotiation/` — capability negotiation protocol
- `standard/inter_agent/trust/` — trust-score storage + decay (per canonical §6 trust math: `τ_t = clamp(0.04·S − 0.15·V − 0.08·F + 0.02·P, 0, 1)`)
- `standard/model_extended/backends/` — pluggable Ollama/external (LM Studio is canonical primary per memory)
- `standard/observability/health/` — `/health`/`/ready`/`/sanity` endpoint logic
- `standard/observability/logging/` — structured logging w/ trace_id contextvar
- `standard/observability/metrics/` — Prometheus counters + histograms (canonical §6 references specific metrics: `<agent>_cycle_stage_duration_seconds`, etc.)
- `standard/orchestration/colony_tick/` (→ `ensemble_tick/` v3) — periodic multi-pod parallel execution
- `standard/orchestration/patterns/` — fan-out / gather / retry / circuit-broken-call shapes
- `standard/persona_overlays/affect/` — emotional modeling (recovery has `persona_memory_v2.py` which is part of this)
- `standard/persona_overlays/context/` — persona-context coupling (mood-aware memory weighting)
- `standard/persona_overlays/roles/` — role overlays (operator-on-loop/supervisor/observer)
- `standard/persona_overlays/voice/` — speech/style/diction (recovery has `style_adapter_v2.py` + `social_adapter_v2.py`)
- `standard/security_extended/defense_in_depth/` — 6 layers L1-L6 per canonical §7
- `standard/security_extended/anomaly/` — runtime anomaly detection (recovery `system_invariant_defense_scenarios.md`)
- `standard/security_extended/baselines/` — known-good behavioral baselines + drift detection (recovery `drift_simulation_harness.py`)
- `standard/self_heal_extended/tick_healer/` — periodic local recovery sweep
- `standard/self_heal_extended/federation/` — peer-recovery state machine (IDLE→QUARANTINED→RECOVERY_SENT→VERIFYING→REINTEGRATED per canonical §17)
- `standard/self_heal_extended/supervisor/` — supervisor-role logic (matches Major's responsibilities; canonical-level per §6)

### PACKS (all 5 mostly empty)
- All pods need creation per pack manifests
- Per-pack `__init__.py` needs to export `get_pods()` and `get_router()` per canonical §11

**Bottom line gap count**: ~50 directories scaffolded, recovery files cover roughly half. The other half requires fresh canonical-compliant implementation.

---

## 6. Prioritized fill-in plan

Ordered by dependency depth — must-fill-first first.

### Phase 1: CORE foundations (blocking everything else)
1. **`core/infrastructure/filesystem/__init__.py`** — `get_paths()` returns `Paths(data, logs, config, run, ...)`. Almost every downstream module imports this. **Status: 0% — write fresh.**
2. **`core/manifest/__init__.py`** — profile + pack resolver. Without this, no pack/profile loading works. **Status: 0% — write fresh per canonical §4 spec.**
3. **`core/bootstrap/__init__.py`** — 7-phase boot orchestrator. **Status: 0% — write fresh per canonical §3.**
4. **`core/identity/__init__.py`** — load `data/identity/values.json`, hash-record at boot. **Status: 0% — write fresh per canonical §5 schema.**
5. **`core/model/backends/local_echo.py`** — deterministic fallback. **Status: 0%.**
6. **`core/security/posture/`** — `declare_posture()` + registry. **Status: 0%.**
7. **`core/security/authority/`** — AuthorityTier + SecurityContext + TrustTier. **Status: 0% — informed by `three_plane_authority_model.md` + `autonomy_tier_model.md`.**
8. **`core/governance/constitution/`** — 5 immutable hard rules (regex). **Status: 0% — canonical §5 specifies regex pattern.**
9. **`core/governance/policy/`** — PolicyDecisionPoint. **Status: 0%.**
10. **`core/self_heal/`** — registry + coverage gate. **Status: 0%.**

### Phase 2: CORE mutation plane (canonical §8)
11. **`core/mutation/permit/`** — 7-state lifecycle. **Status: 0%.**
12. **`core/mutation/ledger/`** — hash-chained JSONL w/ rotating HMAC. **Status: 0% — recovery `forensic_ledger_chain_model.md` provides chain primitives.**
13. **`core/mutation/interception/`** — `sys.audit` hook. **Status: 0%.**
14. **`core/mutation/pipeline/execute.py`** — 8-stage executor. **Status: 0% — recovery `master_framework_mutation_lifecycle.py` provides 9-stage variant; adapt to 8 + document 9th as scaling concern.**

### Phase 3: STANDARD planes — data + observability (needed for everything else to log/persist)
15. **`standard/data/ledgers/_chain.py` + `integrity.py` + `egress.py` + `incident.py`** — recovery `forensic_ledger_chain_model.md` is direct fill.
16. **`standard/data/backends/file.py` + `json_kv.py`** — fresh write.
17. **`standard/observability/logging/`** — structured logging. **Status: 0%.**
18. **`standard/observability/metrics/`** — Prometheus bridge. **Recovery `system_maturity_metrics.md` provides SMI composite.**
19. **`standard/observability/health/`** — `/health` `/ready` `/sanity` handlers. **Status: 0%.**

### Phase 4: STANDARD planes — context + autonomy + inter_agent (cognitive prerequisites)
20. **`standard/context/tiers/{working,episodic,semantic}.py`** — fill 3 tier siblings of `long_term.py`. Recovery `seed_knowledge_v2.py` + `knowledge_ingest_pod.py` provides semantic.
21. **`standard/context/bus/`** — event bus. **Status: 0%.**
22. **`standard/inter_agent/identity/envelope.py`** — recovery `security_layer_restructure.py` envelope module is direct fill.
23. **`standard/inter_agent/trust/`** — recovery `governance_parliament.py` reputation-feedback informs trust-score storage.
24. **`standard/inter_agent/discovery/`** + `negotiation/` — fresh write.
25. **`standard/autonomy/autonomic_loop/`** — recovery `strategy_engine.py` provides verdict logic.
26. **`standard/autonomy/evolution/bandit.py`** — recovery `campaign_bandit.py` is direct fill.

### Phase 5: STANDARD planes — agents + persona_overlays + application
27. **`standard/agents/base/`** — abstract pod class. **Status: 0%.**
28. **`standard/agents/cognitive/loop.py`** — 9-stage CognitiveLoop. Recovery `meta_cognition_engine.py` informs the wrapper pattern.
29. **`standard/agents/cognitive/stages/persona_frame.py`** — recovery `persona_system_v2_frontier.py` direct fill.
30. **`standard/agents/parliament/quorum_voter.py`** — recovery `governance_parliament.py` direct fill (apply v3 rename).
31. **`standard/agents/reasoning/`** — fresh write.
32. **`standard/persona_overlays/affect/`** — recovery `persona_memory_v2.py` provides affect history.
33. **`standard/persona_overlays/voice/{style,social}_adapter.py`** — recovery direct fills.
34. **`standard/persona_overlays/context/operating_mode.py`** — recovery `persona_system_v2_frontier.py` `OperatingMode` enum.
35. **`standard/persona_overlays/roles/`** — fresh write; load configurations from `data/persona/roles/*.json`.
36. **`standard/application/api/`** — endpoint handlers per canonical §14.
37. **`standard/application/middleware/`** — canonical §6 middleware stack (SecurityHeaders → APIVersion → ReplayProtection → TraceMetrics → CORS). Recovery `stripe_webhook_receiver.py` informs ReplayProtection.

### Phase 6: STANDARD planes — security_extended + self_heal_extended + orchestration + model_extended
38. **`standard/security_extended/defense_in_depth/`** — 6 layers L1-L6.
39. **`standard/security_extended/anomaly/`** — recovery `system_invariant_defense_scenarios.md` direct fill.
40. **`standard/security_extended/baselines/`** — recovery `drift_simulation_harness.py` + `governance_drift_detection.py` scoring math.
41. **`standard/self_heal_extended/supervisor/`** — recovery `automation_factory_integration.py` informs cross-module health.
42. **`standard/self_heal_extended/federation/`** — fresh write per canonical §17.
43. **`standard/self_heal_extended/tick_healer/`** — fresh write.
44. **`standard/orchestration/colony_tick/`** (or `ensemble_tick/` after v3 rename) — fresh write.
45. **`standard/orchestration/patterns/dlq_resolver.py`** — recovery `queue_app_dlq_resolver.py` direct fill.
46. **`standard/orchestration/patterns/dag_dispatcher.py`** — recovery `fulfillment_worker_v2.py` informs.
47. **`standard/model_extended/backends/ollama.py`** + `whisper_stt.py` + `piper_tts.py` — recovery `offline_voice_hud_stack.md` informs.

### Phase 7: PACKS
48. **`packs/business/pods/`** — multiple recovery fills (receptionist, lead_scorer, sales/*, fulfillment_engine, document_generator, prospect, portfolio_optimizer, llm_strategist)
49. **`packs/content/pods/`** — rag/scraper/content_gen fills from recovery
50. **`packs/population/`** — entire pack from `agent_civilization_blueprint.md`
51. **`packs/security_tools/pods/`** — fresh writes
52. **`packs/research/pods/`** — fresh writes (canonical declares `research`, `log_intel`)

### Phase 8: settings + config + rename application
53. Extend `core/configuration/settings/base.py` with all new `sn_*` flags listed in this audit
54. Apply v3 renames to canonical paths still on old names (`colony_tick` → `ensemble_tick`, etc.)
55. Add CI check `scripts/check_naming.py` (per feedback_naming_convention)
56. Update canonical doc to reflect filled state

---

## 7. Settings to add to `core/configuration/settings/base.py`

Recovery files reference settings not currently in `base.py`. Add these:

```python
# ── Persona overlay extensions (chat 15, 17, 18) ──
sn_persona_history_size: int = 32
sn_persona_audit_enabled: bool = True
sn_persona_emergency_drift_recovery_ceiling: float = 0.9
sn_persona_mode_min_hold_seconds: float = 5.0

sn_style_adapter_enabled: bool = True
sn_social_adapter_enabled: bool = True
sn_social_drift_window_size: int = 12

sn_persona_memory_max_events: int = 5000
sn_persona_memory_wal_enabled: bool = True
sn_persona_memory_async_flush: bool = True

# ── Drift detection (chat 45) ──
sn_drift_baseline_required: bool = True
sn_drift_severity_ewma_lambda: float = 0.3
sn_drift_behavioral_weight: float = 0.25
sn_drift_heuristic_weight: float = 0.20
sn_drift_schema_weight: float = 0.25
sn_drift_policy_weight: float = 0.20
sn_drift_resource_weight: float = 0.10

# ── Parliament / quorum_voter (chat 40) ──
sn_parliament_quorum: float = 0.5
sn_parliament_base_approval: float = 0.67
sn_parliament_reputation_gain: float = 0.02
sn_parliament_reputation_loss: float = 0.05

# ── Autonomy tier model (chat 39) ──
sn_autonomy_tier_promotion_dwell_sec: int = 300
sn_autonomy_tier_demotion_hysteresis: float = 0.08
sn_autonomy_level_5_requires_parliament: bool = True
sn_autonomy_level_5_requires_simulation: bool = True

# ── Forensic ledger (chats 26, 30) ──
sn_ledger_hmac_key_rotation_sec: int = 86400
sn_ledger_hmac_overlap_sec: int = 300
sn_ledger_verify_at_boot: bool = True
sn_ledger_verify_max_entries: int = 100

# ── Template pipeline (chat 43) ──
sn_template_max_workflow_steps: int = 5
sn_template_max_external_tools: int = 3
sn_template_max_templates: int = 3
sn_template_drift_threshold: float = 0.80
sn_template_promotion_min_successful_runs: int = 10
sn_template_promotion_min_validation_rate: float = 0.95

# ── Knowledge ingestion (chat 01) ──
sn_knowledge_chunk_size: int = 800
sn_knowledge_chunk_overlap: int = 100
sn_knowledge_batch_size: int = 64
sn_knowledge_default_collection: str = "samus_knowledge"
```

---

## 8. Acceptance gates (per canonical §15)

After each phase, the build must pass:
1. `python tests/smoke/test_boot_minimal.py` — `OK: minimal profile boots to Phase 7`
2. `python tests/smoke/test_boot_standard.py` — 4 OK lines
3. `python tests/smoke/test_boot_research.py` — 4 OK lines
4. `logs/boot/mutation_scope_report.json` — `percent_zero_mutation >= 0.90`, `violations == 0`
5. `logs/boot/boot_report.json` — `phase_completed == 7`, `self_heal_coverage >= 0.90`
6. `/api/admin/scopes` — every pack pod has non-empty scope
7. `/api/admin/postures` — every loaded module has declared posture

Sample fill-ins (next section) include the `@mutation_scope` decorator and posture declaration required to pass gates 6+7.

---

## 9. Sample concrete fill-ins

Three sample concrete fill-ins follow as separate files to demonstrate the adaptation pattern in canonical-compliant form:

1. **`fillin_sample_quorum_voter.py`** — adapts `governance_parliament.py` → `backend/standard/agents/parliament/quorum_voter.py` (v3 rename, async, settings, mutation_scope)
2. **`fillin_sample_persona_frame.py`** — adapts `persona_system_v2_frontier.py` → `backend/standard/agents/cognitive/stages/persona_frame.py` (strips `Component`, async-ready, infrastructure.filesystem)
3. **`fillin_sample_integrity_ledger.py`** — adapts `forensic_ledger_chain_model.md` → `backend/standard/data/ledgers/integrity.py` (canonical ledger schema + chain primitives)

These three demonstrate the three most common adaptation paths: (a) module pure-rename + class API, (b) strip-Component + integrate-with-canonical, (c) doc-to-code primary fill.
