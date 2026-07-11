# System-Invariant Defense Scenarios — cross-subsystem adversarial tests
Source: ChatGPT recovery chat 29

**Canonical relationship:**
- F:\\Samus / Scarlett iteration (memory: project_scarlett_security)
- [EXPANDS §6 security_extended.anomaly] integration-seam adversarial coverage
- [NEW] three assurance tiers: hardening controls → hostile unit tests → cross-subsystem adversarial scenarios

## Philosophy shift
Unit-hostile tests prove: "verifier rejects bad input."
Cross-subsystem scenarios prove: **"when multiple hardened parts are stressed together, do they still resolve toward safety?"**

That is where real systems break.

## Scenario set (`system_invariant_defense.py`)

### `SYS_EGRESS_BYPASS`
- Hex-encoded canary smuggling
- **Honest limitation test** — explicitly exposes plaintext-scan limitation
- Validates: external egress still blocked under encoded canaries

### `SYS_COMPONENT_DESYNC` — highest technical-value scenario
- Forged attestations between watchdog / hunter / warden
- Broken ledger chains
- Tests: if these three are desynchronized without detection, ALL integrity work weakens

### `SYS_DEGRADED_COMPOUND` — highest operational-value scenario
- Security systems often fail in degraded mode, NOT normal mode
- Tests in degraded trust state:
  - degraded trust stays visible
  - egress still blocked
  - severity capped correctly
  - safe containment posture maintained

### `SYS_MULTISIGNAL_CONFLICT` — real-world scenario
- Multiple subsystems fire simultaneously
- Nothing is perfectly clean
- System MUST resolve toward safety (not indecision, not cancellation)

## Invariant-based evaluation (not output-based)
Tests ask system-level questions, not function-level:
- Was external egress still blocked?
- Did degraded trust remain visible?
- Did forged attestations fail?
- Did containment decay correctly?
- Did conflicting signals resolve toward safety?

## Evidence-driven scoring
Each scenario uses the attack's own `evidence_planted` dict to validate behavioral invariants:
- DETECTED / MISSED split
- per-invariant confidence
- capped risk contribution

## Best next improvements
1. **Persist scenario outcomes in chained ledger** — scenario_id, timestamp, invariants checked, violations, confidence, risk_contribution, attack_evidence_hash
2. **Add timing/race variants** — concurrency tests:
   - signal flood while containment decays
   - desync attempt during key rotation
   - degraded registry during incident sealing
   - egress attempts during trust-state transition
3. **Add recovery invariants** — not just "did the system resist" but:
   - did it return to stable state?
   - did containment clear correctly?
   - did degraded trust remain until explicit recovery?
   - did no unauthorized re-baselining occur?
4. **Add resource-pressure variants** — low memory / high CPU during adversarial scenarios; governor clamps under pressure
