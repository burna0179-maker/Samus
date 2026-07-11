# ADR-0002: Per-Service HMAC Identity and Caller Grants

## Status
Accepted

## Context
A single shared key authenticates membership but does not establish caller identity or least privilege.

## Decision
Use per-service HMAC keys, signed caller identity, and caller-to-callee grants, retaining a shared-key fallback only for compatibility.

## Consequences
Positive: explicit service identity and deny-by-default authorization.
Negative: more secret distribution and rotation complexity; fallback weakens isolation.
