# ADR-0007: Docker Compose and Cloud Run Targets

## Status
Accepted

## Context
The project needs a reproducible local multi-service environment and a path to individually deployed cloud services.

## Decision
Use Docker Compose for local and host deployments and maintain Cloud Run build/deploy targets for compatible workcells.

## Consequences
Positive: portable images and incremental cloud deployment.
Negative: background workers, local disks, and cross-cloud AWS dependencies require target-specific treatment.
