# Organizational Planes — role-first, lifecycle-first orientation
Source: ChatGPT recovery chat 28

**Canonical relationship:**
- F:\\Samus iteration organizational model
- [EXPANDS canonical's tier model] adds lifecycle dimension orthogonal to plane dimension
- **Operator constraint (chat 28):** path-specific recommendations are AVOIDED in this iteration
  — guidance stays at plane/role/lifecycle level only

## Six planes (role dimension)

| Plane | Contains |
|---|---|
| **Control** | orchestration, policy/governance, operator interface, command routing, health/state summarization |
| **Data** | active agent outputs, workflow artifacts, transient execution state, queues / replay surfaces, dependency interaction results |
| **Observability** | logs, metrics, traces, alert definitions, dashboard provisioning, incident summaries |
| **Security** | signatures, attestations, trust material, verification results, audit evidence, fail-closed policy records |
| **Recovery** | snapshots, rollback manifests, DLQ payloads, replay outcomes, last-known-good state, incident restoration notes |
| **Archive / history** | old runs, exported evidence, retired modules, migration notes, prior schema versions |

## Five lifecycle states (orthogonal to plane)

| Lifecycle | Meaning |
|---|---|
| **active** | currently-executed system elements |
| **staging** | candidate updates, validation, promotion |
| **quarantine** | failed, suspicious, or policy-blocked material |
| **archive** | retired but retained history |
| **scratch** | temporary operator or build workspace |

## Folder/sub-folder principle
Structure separates by **lifecycle first**, then by **function (plane)**:

```
active/
  control/
  observability/
  recovery/
  security/
  agents/
  workflows/
  integrations/
staging/
  ... (same sub-shape)
quarantine/
  ...
archive/
  ...
scratch/
  ...
```

## Observability-specific organizational questions
NOT "where should Alloy read from", but:
- what is **active telemetry**
- what is **historical telemetry**
- what is **operator-facing summary**
- what is **forensic evidence**
- what is **noise** that should never be ingested

Leading to splits:
- live telemetry
- processed telemetry
- alerting definitions
- dashboard definitions
- forensic exports
- retired logs

## Recommendation order rule (chat 28)
For every architectural recommendation, follow this order:
1. plane
2. role
3. lifecycle state
4. folder/sub-folder orientation
5. concrete path ONLY if explicitly requested
