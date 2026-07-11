# ADR-0008: Immutable Runtime Baseline

## Status
Accepted

## Context
Changes to governance and identity code can silently alter security assumptions.

## Decision
Hash and sign a protected-file manifest and verify it during production boot. Updating protected files requires an operator-controlled baseline re-sign.

## Consequences
Positive: detects unauthorized drift in critical files.
Negative: release operations become more complex and signing-key protection is critical.
