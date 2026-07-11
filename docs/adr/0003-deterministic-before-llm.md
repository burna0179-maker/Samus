# ADR-0003: Deterministic Execution Before LLM Calls

## Status
Accepted

## Context
LLM calls add cost, latency, nondeterminism, and audit complexity.

## Decision
Use deterministic logic for policy, state, billing, scoring, routing, and fallback. Permit centralized, budgeted LLM calls only where flexible synthesis materially improves results.

## Consequences
Positive: predictable behavior and bounded cost.
Negative: more templates and explicit rules; less open-ended adaptation.
