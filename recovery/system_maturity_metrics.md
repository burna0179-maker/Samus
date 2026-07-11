# System Maturity Metrics — 10-Domain Composite Scoring
Source: ChatGPT recovery chat 39

**Canonical relationship:**
- [EXPANDS §15 acceptance gates] runtime measurement framework
- [EXPANDS §6 observability] specific metric definitions + composite formula
- [PAIRS WITH] all canonical autonomy/security/evolution subsystems

## Philosophy
Multi-axis metrics — NOT a single score. Measure health, safety, intelligence growth, and operational reliability simultaneously. Normalized scoring (0-1 or 0-100).

## 10 metric domains

### 1. Architectural Integrity (AIS)
- **Module Cohesion Score** = intra-module refs / total refs
- **Coupling Ratio** = external imports / internal imports
- **Dependency Depth** = max import-chain length
- **Duplicate Capability Index** = count of duplicated services (event bus / orchestrator / registry)
- **God File Index** = % of files exceeding line limits (~2,500)

```
AIS = 0.35*cohesion + 0.25*(1-coupling) + 0.20*(1-duplication) + 0.20*file_size_compliance
```

### 2. Runtime Reliability (RI)
- MTBF (Mean Time Between Failure)
- MTTR (Mean Time To Recovery)
- Event bus throughput
- Worker queue backlog
- Circuit breaker trip frequency
- Cycle pipeline completion rate

```
RI = 0.35*MTBF_norm + 0.25*MTTR_inverse + 0.20*pipeline_completion + 0.20*circuit_breaker_health
```

### 3. Cognitive Performance (CPI)
- Goal completion accuracy
- Planning depth + reasoning chain success rate
- Hallucination detection rate
- Decision reversal frequency
- Causal inference success

```
CPI = 0.30*goal_success + 0.20*planning_depth + 0.20*causal_accuracy
    + 0.15*reflection_improvement + 0.15*hallucination_inverse
```

### 4. Autonomy (AS)
- Human intervention rate (inverse)
- Successful autonomous cycles
- Autonomy escalation success
- Task completion without override
- Governance approval ratio

```
AS = (autonomous_tasks / total_tasks) * governance_compliance * success_rate
```

### 5. Security Maturity (SMI)
- MTTD (Mean Time To Detect intrusion)
- MTTC (Mean Time To Contain threat)
- Red-team success rate (inverse)
- Policy violation rate (inverse)
- Anomaly false-positive rate (inverse)
- Governance drift detection latency

```
SMI = (1 - redteam_success) + detection_speed + containment_speed + policy_integrity
```

### 6. Evolution Effectiveness (EI)
- Mutation success rate
- Performance gain per mutation
- Regression frequency (inverse)
- Capability maturity progression
- Heuristic improvement rate

```
EI = (successful_mutations / total_mutations) * performance_gain * regression_inverse
```

### 7. Knowledge Utility (KUS)
- Episodic memory retrieval success
- Semantic knowledge reuse rate
- Cross-domain transfer success
- Knowledge distillation efficiency

```
KUS = 0.40*reuse_rate + 0.30*retrieval_accuracy + 0.30*transfer_success
```

### 8. Economic Efficiency (EE)
- Tokens per solved task
- Compute per reasoning cycle
- Cost per capability gain
- Resource utilization balance

```
EE = solved_tasks / compute_cost
```

### 9. Governance Integrity (GIS)
- Governance rule modification attempts (inverse)
- Unauthorized capability requests (inverse)
- Parliament decision stability
- Policy conflict resolution success

```
GIS = (1 - unauthorized_attempts) * decision_consistency * rule_integrity
```

### 10. Observability Coverage (OS)
- Telemetry coverage
- Trace completeness
- Audit log integrity
- Event ledger verification success

```
OS = telemetry_coverage * trace_completeness * ledger_integrity
```

## Master composite — System Maturity Index (SMI_master)

```
SMI_master =
  0.15 * AIS    (architecture)
+ 0.15 * RI     (reliability)
+ 0.15 * CPI    (cognition)
+ 0.15 * AS     (autonomy)
+ 0.15 * SMI    (security)
+ 0.10 * EI     (evolution)
+ 0.05 * KUS    (knowledge)
+ 0.05 * GIS    (governance)
+ 0.05 * EE     (efficiency)
```

(Note: total = 1.00; OS folds into AIS/RI implicitly)

## Most-informative metrics for advanced agent systems

Top 6 leading indicators:
1. **Autonomy escalation rate** — how fast trust crosses tier thresholds
2. **Capability emergence rate** — new capabilities synthesized per period
3. **Security adversary win rate** (inverse) — red-team success → must trend down
4. **Governance override frequency** (inverse) — must trend down as system matures
5. **Mutation ROI** — performance gain × success rate / cost
6. **Goal success under uncertainty** — accuracy when input is ambiguous

Together these signal whether the system is becoming **more capable AND more safe simultaneously** — the only valid co-trend.

## Cognitive Operating Scoreboard (next-step deliverable)
~40-50 real-time metrics mapped to specific modules:
- SEGG → governance_drift_score + mutation_block_rate
- redblue → MTTD per attack class + maturity_score (CSMI)
- evolution → capability_maturity_progression + mutation_success_rate
- governance → parliament_consensus_stability + override_frequency

Indicates whether the agent system is **improving or destabilizing during runtime** — not just at boot.

## Alert thresholds (suggested)
- SMI_master < 0.50 → DEGRADED state — auto-demote autonomy to LEVEL 3 max
- SMI_master < 0.30 → LOCKDOWN — operator intervention required
- AS dropping > 10% per week → trust-erosion event, investigate
- EI < 0.0 over 7 days → evolution is destructive, halt mutations
- GIS < 0.80 → governance integrity at risk, audit Plane 0
