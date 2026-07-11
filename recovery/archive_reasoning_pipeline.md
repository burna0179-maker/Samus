# Continuous Archive Reasoning Pipeline + 3-Layer Snapshot
Source: ChatGPT recovery chat 46

**Canonical relationship:**
- [NEW pack] archive_reasoning — periodic continuous architectural-extraction loop
- [PAIRS WITH] alfred_document_agent.py (template registry pattern)
- [PAIRS WITH] smms_chatgpt_ingestion.py (knowledge base ingestion)
- [NEW] **3-Layer Architecture Snapshot** as system integrity model (structural / behavioral / capability)
- [PAIRS WITH] three_plane_authority_model.md (snapshot is owned by Plane 0; Parliament evaluates against it)

## Periodic Archive Reasoning Pipeline

### Trigger
A periodic process `periodic_archive_scan` runs on a scheduled interval, calling:
```python
analyze_archives.run_pipeline()
```

### Pipeline stages
1. **INGEST** — scan `incoming/` directory (archived documents, notes, schemas, specs, prompts, partial designs)
2. **PROCESS** — for each file:
   - Parse + extract structured knowledge
   - Identify: concepts / architectures / algorithms / system features / implicit capabilities / partially-described mechanisms
   - Determine: introduces new concept | extends subsystem | exposes missing implementation | implies non-existent feature
3. **REASONING EXPANSION** — for each extracted concept:
   - Evaluate whether system lacks: functions / classes / modules / subsystems / utilities / orchestration / validation / monitoring
   - If gaps exist → construct NEW implementation components (Python modules, scaffolds, pipelines, reasoning engines, validators, adapters, automation routines)
   - **Allowed to infer and design functionality that was implied but not explicitly written**
4. **SYSTEM COMPLETION** — expand capability, connect subsystems, fill architectural gaps
5. **OUTPUT GENERATION** — fully functional `.py` files into `generated/` with: clear purpose, structured classes/functions, docstrings, integration points
6. **ARCHIVE MANAGEMENT** — move source to `processed/` or `rejected/`
7. **CONTINUOUS IMPROVEMENT** — extend previously generated modules; refactor when new context arrives

### Core principle
> **Old material is not static history. It is raw intelligence that can produce new architecture.**
> Continuously transform archival information into executable system capability.

### Output requirements
- Return: file path + complete `.py` + brief reasoning explaining what gap the file fills
- NO placeholder code
- Only create modules that represent meaningful architectural capability

### Optional fourth stage: Architectural Gap Detection
Maintain a live system map; check archives against it → turns pipeline into self-extending architecture engine.

---

## 3-Layer Architecture Snapshot (governance baseline)

Snapshot represents **governance-approved reality**. Combined structural + behavioral + capability layers form the **system integrity model**.

### Layer 1 — Structural Snapshot
Physical architecture: subsystems / modules / dependencies / interfaces.
Detects: unauthorized modules, dependency injection, subsystem boundary violations.

### Layer 2 — Behavioral Snapshot
Runtime permissions: filesystem read/write paths, network policy, runtime restrictions (dynamic code, shell, self-modifying).
Detects: privilege escalation, malicious runtime behavior, new attack surfaces.

### Layer 3 — Capability Snapshot
What system can do: capabilities map → modules → subsystems.
Detects: capability duplication, covert feature insertion, logic escalation.

### Compact unified snapshot
```json
{
  "snapshot_version": "1.0",
  "timestamp": "ISO8601",
  "structure": {
    "subsystems": {
      "archive": ["archive_ingest", "archive_router"],
      "knowledge": ["concept_extractor", "concept_graph"],
      "governance": ["parliament_engine", "proposal_router"]
    },
    "modules": {
      "archive_ingest": {
        "path": "services/archive_ingest.py",
        "deps": ["filesystem", "document_parser"],
        "interfaces": ["scan_incoming", "route_archive"]
      }
    },
    "dependency_edges": [["archive_ingest", "document_parser"]]
  },
  "behavior": {
    "fs_read": ["archives/incoming", "knowledge/"],
    "fs_write": ["archives/processed", "proposals/"],
    "network": {"enabled": false, "allowed_hosts": []},
    "execution": {"shell": false, "dynamic_code": false, "self_modify": false}
  },
  "capabilities": {
    "archive_ingestion": "archive_ingest",
    "concept_extraction": "concept_extractor"
  },
  "integrity": {
    "structure_hash": "sha256...",
    "behavior_hash": "sha256...",
    "capability_hash": "sha256..."
  }
}
```

### Governance evaluation logic (Parliament checks)
```python
# 1. Structural diff
if module_name not in snapshot["structure"]["modules"]:
    new_module_detected()
# 2. Dependency validation
for dep in proposed_deps:
    if dep not in approved_dependencies:
        flag_dependency_violation()
# 3. Behavior validation
if requested_permission not in snapshot["behavior"]:
    flag_privilege_escalation()
# 4. Capability validation
if capability in snapshot["capabilities"]:
    flag_duplicate_capability()
```

### Adversarial detection patterns (auto-flag)
- Hidden network access
- Shell command execution
- Dynamic code evaluation
- New filesystem paths
- Cross-subsystem dependency chains
- Duplicate capability names

### Snapshot lifecycle (CRITICAL)
Snapshots updated ONLY after:
1. Parliament approves a proposal
2. Module generation completes
3. Integration verification passes

**Never update snapshots automatically during ingestion.** Snapshots = governance-approved reality, not runtime state.

### Why compact format wins
- Small enough for fast Parliament reasoning (milliseconds)
- Instant structural diffing via hashes
- Exposes dependency anomalies immediately
- Deterministic proposal evaluation
- Acts as **structural checksum** of the architecture

### Optional 4th layer (chat 46 next-tier)
**Execution Surface Map** — tracks exactly what code paths are allowed to run.
Dramatically improves adversarial detection in agentic architectures.

### Risk-scored governance (chat 46 upgrade)
Each proposal scored by:
- `permission_changes` count
- `dependency_depth`
- `subsystem_boundary_crossings`
- `new_capabilities` count

Parliament votes based on risk thresholds (integrates with `governance_parliament.py` from chat 40).

---

## Docker disk pressure auto-alleviation (chat 46 operational tail)

For long-running agentic systems, combine three safeguards:

1. **Log rotation** (`/etc/docker/daemon.json` — `max-size: 10m, max-file: 3`)
2. **Scheduled prune** (`docker system prune -af --volumes` daily at 3 AM via cron)
3. **Build cache cleanup** (`docker builder prune -af` periodically)

### Disk watchdog logic (for autonomous systems)
```python
if disk_usage > 80%:
    prune_images()
if disk_usage > 90%:
    prune_volumes()
if disk_usage > 95%:
    pause_noncritical_containers()
```

Prevents runtime failures from disk exhaustion. Self-maintenance container option avoids cron dependency.
