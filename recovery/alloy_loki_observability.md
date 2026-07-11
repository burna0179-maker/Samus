# Alloy + Loki Observability Spine
Source: ChatGPT recovery chat 27

**Canonical relationship:**
- [EXPANDS §6 observability plane] centralized log substrate
- [NEW] Grafana LGTM stack alignment (Loki + Grafana + Tempo later)

## Mental model
```
Agent / PowerShell / app logs → Alloy → Loki → Grafana
                                                  ↓
                                          dashboards / explore / alerts
```

## Roles
| Component | Function | Per-node |
|---|---|---|
| **Alloy** | Telemetry collector — tails files, reads Windows Event Logs, ships to Loki | 1 per node |
| **Loki** | Centralized log store — indexes labels (not full text) | 1 centralized |
| **Grafana** | Query UI, dashboards, alerts | 1 centralized |

## Deployment shape (Windows-first)
- **Per node:** 1× Alloy service. Reads agent log files + selected Windows Event Logs (Application, System, Windows PowerShell, Microsoft-Windows-PowerShell/Operational)
- **Central ops node:** 1× Loki monolithic (good up to ~20 GB/day) + 1× Grafana
- **Later:** Tempo for traces, OTLP receiver, metrics backend

## Critical: label strategy
Loki indexes labels — small, stable, low-cardinality only.

**Good labels:**
- `agent`, `node`, `subsystem`, `env`, `severity`, `job`, `level`

**BAD labels (high cardinality — degrade Loki):**
- request IDs as labels
- user text
- dynamic file paths
- unique prompt text
- workflow IDs
- correlation IDs

Put high-cardinality context in the **log body** as structured fields, or use Loki's `structured_metadata` (unindexed).

## Canonical log format (JSON one-line)
```json
{
  "ts": "2026-03-20T18:14:11.238Z",
  "level": "INFO",
  "node": "HF-OPS-01",
  "agent": "samus_orchestrator",
  "subsystem": "memory_bus",
  "workflow_id": "wf_20260320_1814_a1",
  "correlation_id": "c_8f2d1b",
  "event_type": "neo4j_query_started",
  "dependency": "neo4j",
  "message": "starting memory lookup",
  "extra": {"query_class": "critical_read", "timeout_ms": 120}
}
```

Searchable fields in body; safe labels at ingest.

## Alloy pipeline pattern
```
discover files → loki.source.file → loki.process (parse/relabel) → loki.write → Loki
                  loki.source.windowsevent (per channel) ↗
```

## First dashboards (only four)
1. **Fleet health** — logs/sec by node, error rate, missing/silent nodes
2. **Dependency health** — Neo4j log volume, timeouts, degraded-mode activations
3. **Workflow health** — replay jobs, DLQ events, failed runs by subsystem
4. **Security & governance** — signature failures, ledger verification failures, policy blocks

## Rollout
- **Phase 1**: Loki + Grafana on one ops host; Alloy on ONE pilot node only
- **Phase 2**: expand node-by-node, add PowerShell channels, structured JSON parsing
- **Phase 3**: OTLP logs/traces, Tempo, Grafana links wired into HUD

## Operational guardrails
- one canonical log root per node
- one log format standard for all new agents
- no tombstone folders in active collection paths
- separate active logs from archive logs
- rotate files predictably
- NEVER label by anything unbounded
- collect only active branches (dead stubs excluded during cleanup)
