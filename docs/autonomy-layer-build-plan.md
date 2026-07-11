# Samus Full Autonomy Layer — Build Spec

> Operator-approved 2026-06-24. Natively absorb `recovery/meta_cognition_engine.py` by building the missing `CognitiveLoop` core + the 6 `backend.autonomy.*` subsystems it wraps, with persona (Stage-1) feeding in. **Wire-not-arm: whole layer ships default OFF, propose-only, existing governance gates remain authoritative.**

## Guardrails (apply to every phase)
- **Preservation:** never edit `recovery/*` originals (port/absorb into `backend/`). Do NOT touch the two existing autonomy substrates — `backend/common/autonomy.py` (MAPE-K planner behind `/autonomy/plan`) and `backend/identity/autonomy_layer.py` (`_shared.autonomy` wiring). The new `CognitiveLoop` is a distinct THIRD construct.
- **Authoritative gates (autonomy PROPOSES, gates DISPOSE — never bypass):** `cash_engine/gate.py::evaluate_gate`, `common/codex/validator.py::check_action`, `governance/efh_evaluator.py::EthicalFailureHandler.evaluate`, `common/compliance_guard.py::evaluate`, `governance/karma/engine.py::check`, `cash_engine/stages.py::_outreach_stage` (live-send double-gate).
- **Dormancy (house pattern):** every new capability gated by a `*_enabled: bool = False` field in `backend/common/config.py` + mirrored `_env_bool("SAMUS_*_ENABLED", False)` in `backend/common/settings.py` (`Settings` is `extra="forbid"` → add to BOTH). Template module: `backend/attribution/`. Persist via `backend/common/state_paths.py::state_path(...)`.
- No commit/push. Run only the phase's targeted tests with `D:\Hustleforge\Samus\.venv\Scripts\python.exe -m pytest <files> -q` (skip full suite; ~32 known finance failures unrelated).

## Contract 1 — `CognitiveLoop` (what MetaCognitionEngine wraps)
`async loop.cycle(inp) -> result`.
- `inp` (mutable): `system_prompt:str` (engine does `inp.system_prompt += bias`), `user_text:str`, `channel:str`, `plan_token:str`, `metadata:dict`.
- `result`: `elapsed_ms:float`, `errors:list[str]`, `compliance_blocked:bool`, `plan_token:str`, plus `reply:str`, `ok:bool`, `stages:list[StageResult]`, `persona_frame:dict|None`, `reflect_score:float`.

## Contract 2 — the 6 `backend/autonomy/*` subsystems (constructed no-arg, fail-safe)
| Module | Class | Methods called by the wrapper |
|---|---|---|
| `situation_index.py` | `SituationIndex` | `classify(text=,channel=,plan_token=) -> dict{risk,type}` |
| `heuristic_registry.py` | `HeuristicRegistry` | `evaluate_pre(text=,situation=) -> str` (bias text; "" = none) |
| `reinforcement_heuristics.py` | `ReinforcementHeuristicEngine` | `get_weight(name)->float`, `reinforce(name,reward)->None`, `snapshot()` (PORT from recovery lines 168-227; weight∈[0.1,5.0], reward∈[-1,1], decay 0.995) |
| `autotuner.py` | `Autotuner` | `adjust(latency=,errors=,compliance_blocked=) -> None` |
| `upgrade_engine.py` | `UpgradeEngine` | `evaluate(result=,situation=)->bool`, `execute()->None` (execute writes a PROPOSAL record only — NEVER self-modifies code) |
| `architecture_persistence.py` | `ArchitecturePersistence` | `snapshot(plan_token=,compliance_blocked=,error_count=) -> None` |

## Reference to MIRROR (do not invent a divergent design)
- Primary: `D:\Hustleforge\Anita\backend\core\workflows\cognitive_loop.py` (class `CognitiveLoop:168`, `run():651`, `STAGE_NAMES:155` = PERCEIVE→CLASSIFY→RETRIEVE→REASON→DECIDE→ACT→REFLECT→PERSIST→EMIT) + `cycle_models.py` (`CycleInput:24`, `CycleResult:96`, `StageResult:40`, `StageStatus:18`). Every optional dep is constructor-injected and degrades to no-op when `None` (bare-loop test pattern); classify short-circuit sets the `compliance_blocked` analogue and skips to EMIT.
- Altitude: `D:\Hustleforge\Optimus\backend\standard\cognition\loop.py` — minimal, injected substrate, fail-closed to HOLD, SENSE/ACT greenfield, `register_posture` declares zero caps. Build Samus at THIS altitude (ACT proposes-only).

## Stage-1 persona (already ported, dormant) + a bug to fix
- `backend/cognitive/persona_frame.py::PersonaSystem.transform(stimulus)->dict` (+`_persona_frame`), `.notify_cycle_outcome(...)`, `.snapshot()`. Flag `persona_frame_enabled` (OFF).
- `backend/memory/persona_self_model.py::PersonaMemory` (WAL+JSON), `.snapshot()`, `.apply_introspection(...)`. Flag `persona_self_model_enabled` (OFF).
- **BUG (fix in Phase C):** `persona_frame.py:186-193` reads `snap["emotion"]["novelty"]`, `snap["emotion"]["drift"]`/`memory_confidence`, `snap["baseline_confidence"]`, `snap["counters"][...]` — but `PersonaMemory.snapshot()` (`persona_self_model.py:202-209`) returns `{schema,emotion:{valence,confidence,novelty},heuristics:{...},event_count,ts}` (no `counters`/`baseline_confidence`; `drift` is under `heuristics.avg_drift`). So drift currently reads 0. Fix additively by making `snapshot()` a SUPERSET exposing the keys persona_frame reads (preserve existing keys).

## Phase plan (build A–E now; F–G are operator-GATED)
- **Phase A [SAFE]** — `backend/cognitive/cycle_models.py` (CycleInput/CycleResult/StageResult/StageStatus per Contract 1) + `backend/cognitive/cognitive_loop.py` (`CognitiveLoop.cycle`, all deps injected/optional, stages PERCEIVE→…→EMIT with REASON/ACT as no-op stubs this phase, fail-closed). Tests: bare loop runs, returns CycleResult exposing the contract fields, `system_prompt` mutable, never raises.
- **Phase B [SAFE]** — `backend/autonomy/__init__.py` + the 6 modules (Contract 2). Port `reinforcement_heuristics` from recovery + add `state_path("autonomy","heuristic_weights.json")`. Add the per-subsystem `*_enabled=False` flags. Tests: each constructs no-arg, satisfies its signature, RL clamps/decay, persistence round-trips, each inert when its flag False.
- **Phase C [SAFE]** — fix the persona snapshot superset (above); implement REASON (`backend/common/llm_client.py` workcell `"cognition"`, budget-gated, errors→`result.errors`, fail-closed HOLD) + CLASSIFY (EFH → `compliance_blocked`, short-circuit to EMIT; optional Codex). Tests: non-zero drift after reflections; REASON degrades on LlmCallError/budget without raising; CLASSIFY blocks on a seeded EFH breach.
- **Phase D [SAFE]** — `backend/cognitive/meta_cognition_engine.py` (port recovery near-verbatim) + flag `autonomy_meta_enabled` (OFF → pure passthrough to `loop.cycle`). Wire PersonaSystem into META-PERCEPTION bias + `notify_cycle_outcome` into META-REFLECTION. Tests: bias mutates system_prompt; reward math matches recovery; disabled == byte-identical to direct `loop.cycle`; runs with subsystems `None`.
- **Phase E [SAFE]** — implement PERCEIVE from read-only domain surfaces (CRM `list_follow_ups_due`/`list_opportunities_pending_stake`/`list_operator_tasks`; `cash_engine/decay.py::compute_decay_risk`; `finance/service.py::get_runway`/`get_actions_summary`). ACT stays propose-only → writes `state_path("cognition","proposals.jsonl")`. Tests: PERCEIVE tolerates unavailable backends; ACT writes a proposal and invokes NO effector.
- **Phase F [GATE]** — first live caller (route or control-tick hook) behind `cognitive_loop_enabled` (OFF). Needs operator decision.
- **Phase G [GATE — highest risk]** — promote proposals into `review_opportunity`/`ProposedAction`; first path to live revenue influence (still through every gate). Needs operator decision.

Flags to add across phases (all default False, in config.py + settings.py): `autonomy_meta_enabled`, `cognitive_loop_enabled`, `autonomy_reinforcement_enabled`, `autonomy_autotuner_enabled`, `autonomy_upgrade_enabled` (+ reuse existing `persona_frame_enabled`/`persona_self_model_enabled`).
