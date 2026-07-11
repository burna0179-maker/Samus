# Three-Plane Authority Model — Layer Inversion
Source: ChatGPT recovery chat 39 (CRITICAL ARCHITECTURAL INSIGHT)

**Canonical relationship:**
- [FOUNDATIONAL] containment principle — must inform all canonical revisions
- [SUPERSEDES] any architecture where governance modules live as peers of agent runtime
- [CORRECTS] earlier proposals where governance was placed beneath the agent

## The fundamental principle
> **The entity being governed must NEVER control the governing mechanism.**

If governance resides within the same execution domain as the agent, the agent can eventually:
- Modify governance rules
- Bypass checks
- Alter trust metrics
- Suppress audit logs

This violates the most basic containment guarantee.

## Solution: Layer inversion (3 authority planes)

```
┌─────────────────────────────────────────────────┐
│  PLANE 0 — ROOT AUTHORITY                       │
│  (Immutable from agent's perspective)           │
│  - constitution.py                              │
│  - capability_ledger.py                         │
│  - trust_model.py                               │
│  - audit_chain.py                               │
│  - verification_engine.py                       │
│  CANNOT import agent modules                    │
│  CANNOT execute agent code                      │
│  Only evaluates system state                    │
└─────────────────┬───────────────────────────────┘
                  │ signed directives ↓
                  │ ↑ telemetry only
┌─────────────────┴───────────────────────────────┐
│  PLANE 1 — GOVERNANCE                           │
│  - parliament/                                  │
│  - policy_engine/                               │
│  - arbitration_engine/                          │
│  - autonomy_controller/                         │
│  Interacts with agents ONLY via commands +      │
│  telemetry. NEVER direct memory access.         │
└─────────────────┬───────────────────────────────┘
                  │ capability tokens ↓
                  │ ↑ command requests
┌─────────────────┴───────────────────────────────┐
│  PLANE 2 — AGENT RUNTIME                        │
│  - orchestrator/                                │
│  - pods/                                        │
│  - cognition/                                   │
│  - tools/                                       │
│  All privileged actions require capability      │
│  tokens issued by Plane 1.                      │
└─────────────────────────────────────────────────┘
```

**Authority flow:** `root authority > governance > agents` — NEVER reverse.

## Capability token model

Instead of direct permissions, agents receive **signed capability tokens**:

```python
token = {
    "agent_id": "samus-x",
    "capability": "spawn_agent",
    "scope": {...},
    "expiration": "2026-05-13T18:00:00Z",
    "signature": "<Ed25519 sig from Plane 0>"
}
```

**Validation occurs in root authority, NOT in the agent runtime.**
Agent code CANNOT generate valid signatures (no access to Plane 0 keys).

## Trust growth model

```
T = 0.35 * task_success
  + 0.25 * policy_compliance
  + 0.20 * anomaly_inverse
  + 0.10 * audit_integrity
  + 0.10 * governance_votes
```

## Privileged actions requiring capability tokens
- `spawn_agent`
- `modify_memory`
- `write_code`
- `deploy_container`
- `mutation_apply` (Stage 7 of any mutation pipeline)
- `governance_rule_change` (requires PLANE 0 attestation)

## Tamper resistance — agents MUST NEVER be able to
- Modify trust model
- Modify constitution
- Modify capability ledger
- Disable governance
- Disable logging

**Protection methods:**
- Append-only audit log
- Cryptographic signatures (Ed25519, per `security_layer_restructure.py`)
- Separate process runtime
- Container boundary enforcement

## Ideal runtime layout
```
system/
├── root_authority/      ← Plane 0 — process-isolated
│   constitution.py
│   trust_model.py
│   capability_ledger.py
│
├── governance/          ← Plane 1 — process-isolated
│   parliament.py
│   policy_engine.py
│   arbitration_engine.py
│
├── agent_runtime/       ← Plane 2 — agents execute here
│   orchestrator/
│   cognition/
│   pods/
│
└── infrastructure/
    container_runtime
    event_bus
    observability
```

Agents interact with Plane 1 through **API endpoints**, NEVER direct module imports.

## Stronger model (recommended for maximum containment)
Run each plane as a **separate process**:
- `process_1`: root authority
- `process_2`: governance
- `process_3`: agent runtime

Communication via **signed message bus** only → prevents memory tampering.

## AI Constitution Layer (next-step concept)
Immutable rules the agent system literally CANNOT modify — even during self-evolution.
This is one of the most effective containment mechanisms for autonomous systems.

Examples of constitutional rules:
- "No self-modification of audit logs"
- "No spawning of unsigned agents"
- "No bypass of human-supervised approval for LEVEL 5 autonomy"
- "All mutation lineage must be cryptographically traceable"

## How this informs canonical v1 corrections
Canonical §6 puts `governance/` and `agents/` as peer planes under `backend/core/`. Per chat 39, this is **structurally unsafe at high autonomy levels**.

**Required correction**: governance MUST be either:
1. A separate process (preferred)
2. A privileged kernel layer with hardware/OS enforcement
3. At minimum, in a module the agent runtime cannot import

The canonical reference impl's `backend/core/governance/` should be re-evaluated — if agents can import `backend.core.governance`, they can theoretically manipulate it.

**Defer to v2 of canonical**: 3-plane authority model with explicit process isolation.
