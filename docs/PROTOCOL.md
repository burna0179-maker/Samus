# Protocol Contract Guide

## Purpose

Explain how to review and evolve `protocol_contract.yaml`.

## Contract Responsibilities

The YAML contract should declare:

- stable agent identifier;
- protocol version;
- supported capabilities;
- request and response expectations;
- audit requirements;
- governance and approval behavior;
- compatibility and deprecation policy.

## Change Rules

1. Parse the YAML in CI.
2. Version breaking changes.
3. Add tests for adapters that consume changed fields.
4. Update `Architecture_Samus.md`.
5. Record lasting decisions in an ADR.
6. Do not place secrets, host-specific endpoints, or customer data in the contract.

## Review Questions

- Is every declared capability implemented and wired?
- Does the contract distinguish optional capabilities?
- Can an older peer reject unsupported versions safely?
- Are failure and escalation semantics explicit?
- Are audit fields sufficient to reconstruct a cross-agent decision?
