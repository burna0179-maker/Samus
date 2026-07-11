# Proto Versioning Strategy
Source: ChatGPT recovery chat 23

**Canonical relationship:**
- [EXPANDS §10 inter_agent] versioned message contracts for cross-agent envelopes
- [NEW] domain-scoped namespacing: `hustleforge.<domain>.v1`

## Rules
1. **Major version in package name** — `package hustleforge.mesh.v1` (NOT field suffix `user_id_v2`)
2. **Field reservation on removal** — `reserved 4, 7; reserved "old_field_name";`
3. **Parallel version coexistence** — v1 + v2 run side-by-side, blue/green rollout
4. **Domain-scoped namespaces aligned to planes:**
   - `hustleforge.security.v1`
   - `hustleforge.execution.v1`
   - `hustleforge.control.v1`
   - `hustleforge.recovery.v1`
   - `hustleforge.observability.v1`

## Backward-compatible (no version bump)
- Add new fields
- Add new messages
- Add new enums (at end)

## Breaking (requires new v2 package)
- Remove fields without reserve
- Change field types
- Rename fields
- Change semantics

## Envelope example
```protobuf
syntax = "proto3";
package hustleforge.security.v1;
option java_package = "com.hustleforge.security.v1";

message SignedEnvelope {
  string sender = 1;
  string action = 2;
  bytes payload = 3;
  bytes signature = 4;
  int64 timestamp = 5;
}
```

## Version routing
Orchestrator routes by:
- `proto: "hustleforge.security.v1.SignedEnvelope"` OR
- `version: "v1"` field in envelope

## Optional schema_version field
```protobuf
int32 schema_version = 99;
```
For debugging / observability / audit only — NEVER for compatibility (package version is authoritative).

## Start point
Even on iteration 1, start at `v1` (not unversioned) to avoid retrofit pain.
