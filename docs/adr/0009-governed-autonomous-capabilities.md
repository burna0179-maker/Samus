# ADR-0009: Governed Autonomous Capabilities with Fence Attestation

## Status
Accepted

## Context
Unconditional blocking rules (e.g. VR-G5 banning all voice_dial) prevent the agent from self-initiating legitimate actions even when all safety preconditions are met. Codex ADR-016/017/018/019 established a governed dial policy as the first conditional blocking rule.

## Decision
A Codex blocking rule may define a conditional allowance path gated by a mandatory fence set. The validator checks every fence literally (all must be True); a missing or False fence preserves the unconditional block. The policy defaults OFF and requires explicit operator arming. Daily pre-shift attestation supplies the consent basis — no attestation, no consent, no action.

## Consequences
Positive: the agent can self-initiate governed actions within operator-defined bounds without per-action approval.
Negative: fence attestation logic in the validator grows per capability; each new governed capability needs its own ADR chain and fence set.
