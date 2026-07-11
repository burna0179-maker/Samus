# Samus_Deploy26 — Production Stack Inventory
Source: ChatGPT recovery chat 39

**Canonical relationship:**
- [CURRENT STATE] — user's latest declared production stack
- [SUPERSEDES] earlier F:\\Samus iterations (memory: project_samus_plane_iteration)
- [ALIGNS WITH] canonical v1.3 in many places; expands several beyond canonical scope

## Top-level layout
```
Samus_Deploy26/
├── main.py                          # venv bootstrap + health + uvicorn
├── Dockerfile                       # 2-stage: sanitize → lean Python 3.13 runtime
├── docker-compose.yml               # 3 services: orchestrator + neo4j + security forwarder
├── .env                             # ~675 lines of feature flags & config
├── requirements.txt
├── entrypoint.sh
│
├── backend/                         # ALL APPLICATION CODE
├── scripts/                         # 24 operational scripts
├── Workflows/                       # one_task_ops + workflow_helpers
├── agentic_harness/                 # adversarial test harness
├── tests/                           # 60+ test files
├── data/                            # 12 runtime data domains
└── logs/
```

## backend/ subsystem map (NEW domains beyond canonical §6)

### CORE engine (200+ modules) — `core/`
- `samus_orchestrator.py` — main orchestrator
- `cycle_pipeline.py` — **21-stage typed CycleStage pipeline** (vs canonical 9-stage CognitiveLoop)
- `message_bus.py` + `advanced_event_bus.py` — event-driven pub/sub
- `bootstrap_registry.py` — boot stage tracking singleton
- `shutdown_coordinator.py` — phased graceful shutdown
- `subsystem_manifest.py` — **12 canonical subsystem definitions**
- `schema_registry.py` — **8 canonical enums + schema versioning**
- `capability_registry.py` — runtime capability source of truth
- `capability_scaffolder.py` — stub implementations for gaps
- `autonomic_reflex.py` — reflexive nervous system
- `circuit_breaker_mesh.py` — centralized circuit state
- `config_hot_reload.py` — runtime settings reload

### Subsystem clusters
| Subsystem | Module count | Purpose |
|---|---|---|
| `orchestration/` | 16+ | base/samus orchestrator + facade + reward + reasoning + episode + diagnostics |
| `agents/` | 30 | meta_cognition + pods (domain/content/scraper/lead/email/analytics/rag/living_shield/camera/face) |
| `csa/` (Cognitive Stack Arch) | 15 | constitutional_core + governance_parliament + arbitration + stratified_memory + economic_optimization |
| `evolution/` (Self-Evolution) | **49** | Phase 4-25 — mutation_gate + mutation_mode + signal_correlation + workload_rebalancer + policy_conflict + behavioral_anomaly + meta_observability + capability_maturity + outcome_virtualizer + cognitive_colony + meta_governance + meta_learning + self_referential + emergent_goal + predictive_self_model + cross_domain_transfer + capability_synthesizer + governance_drift_detector + autonomy_audit_cycle |
| `security/` | 35+ | process_baseline + forensic_harvester + baseline_poisoning_defense + anomaly_scoring + bayesian_anomaly + baseline_integrity + adversarial_simulator + evidence_ledger + policy_engine + identity + authority + attestations + input_sanitizer + simulation_gate + actuation_bridge + container_threat_monitor + escape_detection + autonomous_response + redblue/ (23) + redteam/ (7) |
| `governance_guard/` (SEGG) | 8 | segg_core + mutation_interceptor + risk_scoring + drift_monitor + rollback_engine + policy_engine + simulation_sandbox |
| `cognition/` | 9 | episodic_memory + semantic_memory + affect_engine + social_planner + meta_reasoner + six_sigma_spmc + feedback_fusion + drift_adapter |
| `memory/` | 10+ | adaptive_memory_routing + semantic_memory_controller + semantic_vector_store + progressive_disclosure + stack_audit + stack_enrichment + idempotency_ledger |
| `civilization/` | 6 | agent_economy + capability_marketplace + agent_lifecycle + research_cluster + simulation_sandbox |
| `agentic/` | 7 | environment_event_bus + perception_layer + trust_autonomy_model + hierarchical_planner + reward_learning_engine + human_supervision + failure_recovery |
| `services/` | 12 | DI container + bootstrap_registry + shutdown_coordinator + manifest + schema + message_bus + advanced_event_bus + advanced_stack_state_sync + experiment_manager + db |
| `inference/` | 3 | router + backends (Ollama/OpenAI/Anthropic) + types |
| `tools/` | 2 | thermal_guard + trisafe_layer |
| `workflows/` | 8 | lead_generation + batch_ingest_queue + cycle_models + cycle_batch + cycle_reflect + contagious_repair + tick_health + goal_dag |

### Sibling backend domains
- `observability/` (20 modules) — Prometheus bridge w/ 93 methods + resilient_metrics + EWMA circuit breaker + hash-chain event_ledger + log_retention (hot/warm/cold + zstd) + disk_pressure_governor + adaptive_log_controller + log_condenser + token_usage_tracker + otel
- `memory/` (6) — controller + vector_memory + embedding_cache + consolidator + autonomic_memory_tuner + Long_memory
- `persona/` — persona_system + persona_memory + style_adapter + social/adapter (matches recovery chats 15-18)
- `compliance/engine.py`
- `gear/` — gear_schema + registry (GearMaster pattern)
- `integrations/` — ollama_chat_interface (primary) + anthropic_client + ses_client + web_scraper

## Architectural assessment (chat 39)

### Score: 8.5 / 10

### Strengths
- Extremely advanced agent framework
- Strong adversarial security architecture (red/blue + redteam continuous sentinel)
- Hierarchical reasoning capability
- Autonomous evolution architecture (49 modules, Phases 4-25)

### Weaknesses identified
1. **System duplication**: `samus_orchestrator.py`, `circuit_breaker_mesh.py`, `shutdown_coordinator.py`, `bootstrap_registry.py`, `policy_feedback_loop.py`, `capability_registry.py`, `autonomic_reflex.py` exist in MULTIPLE locations (`core/`, `core/orchestration/`, `core/services/`, `core/evolution/`)
2. **God-subsystems**: `core/` has 20+ critical runtime modules; `evolution/` has 49 modules spanning capability registry + perf tuning + autonomy governance + heuristic reinforcement
3. **Security surface fragmentation**: security primitives distributed across `core/security`, `core/governance_guard`, `core/tools/trisafe_layer`, `core/agentic/trust_autonomy_model` → policy fragmentation

### Recommended 5-domain root refactor
```
backend/
├── runtime/          — core execution kernel (orchestrator, event_system, execution, lifecycle)
├── intelligence/     — cognition + planning + learning + evolution
├── agents/           — base + functional + perception + security pods
├── governance/       — parliament + autonomy + safety
├── security/         — identity + anomaly_detection + container_security + redteam
└── infrastructure/   — config + database + inference + observability + utils
```

**One structural improvement that would dramatically improve the system:** introduce a `backend/kernel/` layer owning event bus / scheduler / authority engine / trust model / orchestrator. Everything else becomes plugins → reduces attack surface ~70%.
