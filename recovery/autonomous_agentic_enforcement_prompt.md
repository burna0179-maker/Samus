# Autonomous Agentic System — Production Enforcement Prompt
Source: ChatGPT recovery chat 45

**Canonical relationship:**
- [META-PROMPT] for senior software engineer building fully autonomous agentic AI
- [PAIRS WITH] master_framework_mutation_lifecycle.py (9-stage)
- [PAIRS WITH] agent_civilization_blueprint.md (L5 autonomy)
- [PAIRS WITH] meta_cognition_engine.py (wrapper pattern)

## Core directive
All realized, conceptual, implied, scaffolded, placeholder, or referenced:
- Schemas / Systems / Sub-systems / Services / Controllers / Guardrails
- Pipelines / Cognitive components / Memory structures / Learning mechanisms / Execution layers

MUST be:
1. Converted into **explicit, executable code**
2. **Feature-complete** (no stubs, no TODOs)
3. **Production-deployment ready**
4. **Integrated into the runtime execution path**
5. **Observable, testable, recoverable**

**No architecture diagram, markdown concept, or commentary may exist without corresponding functional implementation.**

## 10 system requirements

### 1. Concrete schema enforcement
Every data model / message contract / cognitive artifact / memory object / plugin interface / capability definition MUST have:
- Typed schema (Pydantic / dataclass / TypedDict)
- Versioning support
- Validation logic
- Serialization/deserialization
- Migration compatibility
- Integrity hashing
- Audit trail hooks

No implicit structures allowed.

### 2. Autonomous reasoning execution
Agent must:
- Continuously evaluate environment state
- Perform goal-drift detection
- Run internal planning loops
- **Trigger execution without human prompting**
- Reprioritize tasks dynamically
- Self-initiate optimization cycles

Required components:
- Scheduler loop (async, non-blocking)
- Task queue with priority tiers
- Cognitive planner module
- Reflection engine
- Self-critique evaluator
- Execution reconciler
- Autonomous trigger policy

### 3. Self-awareness layer
Internal state registry / capability index / active module map / resource telemetry / goal hierarchy map / confidence scoring.
- Runtime introspection API
- Capability self-description endpoint
- Self-health diagnostics
- Degraded mode + recovery + panic isolation

Self-awareness MUST influence planning and execution decisions.

### 4. Learning & self-extension framework
Detect capability gaps → propose new sub-systems → generate plans → create modules → register safely → sandbox experimental code → promote stable → rollback.
- Plugin architecture + capability registry + dynamic loader
- Sandboxed eval env + trust scoring + promotion workflow
- Code signing / integrity validation

### 5. Production hardening (all modules)
- Structured logging + metrics export + trace IDs
- Exception hierarchy + retry policies + circuit breakers
- Timeout enforcement + input sanitization + security gating
- Rate limiting + authentication + HMAC/signature validation
- Config-driven feature flags

**No silent failures allowed.**

### 6. Memory architecture (multi-layer)
- Short-term working / Episodic / Long-term semantic / Capability / Execution history / Audit logs
- Vector indexing + hash integrity + retrieval scoring + decay/pruning + context assembly

### 7. Execution authority
- Evaluate when action required → confirm safety policies → execute system-level commands within scope
- Spin containerized sub-agents → monitor → kill misbehaving → reallocate resources
- Policy engine + authority gating + scope constraints + risk scoring + rollback

### 8. Observability & telemetry
- Central telemetry bus + event-driven arch
- Health endpoints + metrics dashboards
- Self-reporting heartbeat + cognitive cycle logging

**Agent must be able to analyze its own performance data.**

### 9. Codebase enforcement rules
- No placeholder code
- No commented-out logic
- No conceptual modules without implementation
- No markdown-only specifications
- All referenced systems MUST exist as concrete files
- All systems MUST be wired into runtime
- Dead code detection implemented
- Unused schema detection triggers alert

### 10. Autonomous improvement cycle (no human initiation)
1. Observe performance
2. Detect inefficiencies
3. Propose modifications
4. Generate patch
5. Validate in sandbox
6. Run regression tests
7. Promote if stable
8. Archive previous version
9. Update capability map

## Output expectation
- Directory structure
- File contents
- Inter-module wiring
- Startup sequence
- Execution loop
- Deployment config (Dockerfile, compose)
- Security defaults
- Environment config template
- Test scaffolding

## Failure condition
If any system, sub-system, or schema is mentioned but NOT implemented in executable form → **implementation is incomplete**.
