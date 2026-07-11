# ADR-0001: HTTP and SQS Execution Paths

## Status
Accepted

## Context
Local development benefits from direct HTTP calls, while durable production work benefits from queue buffering and retry semantics.

## Decision
The gateway uses SQS for a target when a queue URL is configured and signed HTTP otherwise. Both paths call the same service-layer logic.

## Consequences
Positive: incremental adoption, local simplicity, durable async work.
Negative: parity tests, idempotency, tracing, and eventual consistency are required.
