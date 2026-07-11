# ADR-0004: Workload-Specific Persistence

## Status
Accepted

## Context
Queues, keyed state, append-only evidence, and relationship traversal have different operational requirements.

## Decision
Use SQS for queued work, DynamoDB for keyed state, JSONL for local append-only evidence, Neo4j for constrained graph relationships, and selected Firestore paths for cloud-native ledgers.

## Consequences
Positive: each workload uses an appropriate model.
Negative: reconciliation, backup, security, and operations span multiple systems.
