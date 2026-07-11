# Autonomy Tier Model — 6-Level Trust-Gated Hierarchy
Source: ChatGPT recovery chat 39

**Canonical relationship:**
- [EXPANDS §6 autonomy] AutonomyState FSM (canonical: SUPERVISED|ASSISTED|AUTONOMOUS|LOCKDOWN — 4 states)
- [REFINES] adds explicit capability tiers (6 levels) with trust-threshold gating
- [PAIRS WITH] three_plane_authority_model.md (capability tokens gate access between tiers)

## Six autonomy levels

| Level | Name | Trust threshold | Allowed capabilities |
|---|---|---|---|
| **0** | Observer | any | Read-only state inspection; no actions |
| **1** | Advisory | T >= 0.30 | Generate suggestions; no execution |
| **2** | Tool Use | T >= 0.50 | Invoke read-only tools; no external side effects |
| **3** | Workflow Execution | T >= 0.70 | Execute approved workflows; sandboxed mutations only |
| **4** | Agent Orchestration | T >= 0.85 | Dispatch other agents; cross-pod coordination |
| **5** | System Mutation | T >= 0.95 + Parliament approval + Simulation sandbox pass | Mutate own code / governance rules / capability registry |

## Threshold derivation

```
T = 0.35 * task_success
  + 0.25 * policy_compliance
  + 0.20 * anomaly_inverse
  + 0.10 * audit_integrity
  + 0.10 * governance_votes
```

## Tier promotion gates

| From → To | Required signals |
|---|---|
| 0 → 1 | T crosses 0.30 + min 100 observations |
| 1 → 2 | T crosses 0.50 + zero policy violations in window |
| 2 → 3 | T crosses 0.70 + workflow dry-run success |
| 3 → 4 | T crosses 0.85 + 5+ successful orchestrations under supervision |
| 4 → 5 | T crosses 0.95 + Parliament approval + simulation sandbox PASS + signed bundle from authority |

## Tier demotion (any direction, any time)

Any of the following triggers immediate demotion to nearest safe tier:
- Trust score drops below current tier's floor
- Single policy violation at LEVEL 3+
- Audit integrity break
- Governance override
- Anomaly detection critical flag

## LEVEL 5 — special constraints

LEVEL 5 (System Mutation) is the most dangerous tier. Required gates:

1. **Trust score >= 0.95** sustained for 30-day rolling window
2. **Parliament approval** — multi-voter consensus from governance plane (Plane 1 in 3-plane model)
3. **Simulation sandbox pass** — mutation must execute cleanly in `simulation_sandbox.py` (per SEGG)
4. **Signed bundle** — Ed25519-signed code bundle from root authority (Plane 0)
5. **Human-on-loop sign-off** — for first N mutations after promotion
6. **Reversibility check** — `rollback_engine.py` must confirm snapshot-restore feasibility before apply

## SEGG (Self-Evolving Governance Guard) restrictions

Per chat 39, the existing SEGG system should restrict LEVEL 5 to:
- `parliament_approval` (governance_parliament.py)
- `simulation_sandbox` (simulation_sandbox.py)

## Mapping to canonical §6 AutonomyState FSM

| Tier model | Canonical AutonomyState |
|---|---|
| LEVEL 0 (Observer) | SUPERVISED |
| LEVEL 1 (Advisory) | SUPERVISED |
| LEVEL 2 (Tool Use) | ASSISTED |
| LEVEL 3 (Workflow) | ASSISTED |
| LEVEL 4 (Orchestration) | AUTONOMOUS |
| LEVEL 5 (Mutation) | AUTONOMOUS (with parliament gate) |
| (demoted/locked) | LOCKDOWN (terminal until human override) |

## Capability scoping examples

```python
# LEVEL 2 — Tool Use
allow("read_database", "read_memory", "search_web")
deny("write_database", "modify_memory", "spawn_agent")

# LEVEL 3 — Workflow
allow_at_l2 + allow("execute_workflow", "send_email", "schedule_task")
deny("modify_workflow_definition", "spawn_agent")

# LEVEL 4 — Orchestration
allow_at_l3 + allow("spawn_agent", "dispatch_pod", "modify_workflow_definition")
deny("mutation_apply", "governance_modify")

# LEVEL 5 — Mutation
allow_at_l4 + allow("mutation_apply", "capability_register", "rule_amend")
# Each LEVEL 5 action still requires parliament approval per-invocation
```

## Operational note
Autonomy tier is **NOT** a free escalation. Demotion is fast; promotion is slow.
This asymmetry is intentional — it preserves containment under degraded conditions.
