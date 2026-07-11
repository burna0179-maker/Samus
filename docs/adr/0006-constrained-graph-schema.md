# ADR-0006: Constrained Graph Schema

## Status
Accepted

## Context
Arbitrary Cypher and runtime label creation would make graph state difficult to authorize and audit.

## Decision
Use explicit node labels, relationships, schemas, and query allowlists. Treat new ontology concepts as reviewed schema changes or insight reports.

## Consequences
Positive: safer, more reviewable graph behavior.
Negative: less runtime flexibility and slower schema evolution.
