# Major Design Decisions

The table below summarizes the ten decisions that most shaped the architecture. Each links to deeper reasoning — ADRs record the durable form; `docs/DESIGN.md` and `ENGINEERING_DECISIONS.md` cover the reasoning in full.

| Decision | What I chose | What I rejected | Why it matters |
|---|---|---|---|
| Workcell decomposition | 21 independent FastAPI workcells with shared runtime | Single application or coarser three-tier split | Establishes domain ownership, independent failure boundaries, and a reusable platform abstraction |
| HTTP + SQS dispatch | Gateway routes to either path; service layer is path-agnostic | HTTP-only or queue-only | Allows local development without queue provisioning; adds durability for long-running work without duplicating business logic |
| Central shared runtime | `backend/common/` mandatory for all workcells | Per-workcell implementations | Prevents security, observability, and retry behavior from drifting across 21 services |
| Deterministic-before-LLM | LLMs used only for synthesis; governance, scoring, routing stay deterministic | LLM-first reasoning throughout | Controls cost, latency, and nondeterminism in paths that must be reliable |
| Layered LLM budget chain | Global cap → model floor → circuit breaker → per-workcell quota → 1 call/job max | Single top-level cap | A single check fails silently when individual workcells overrun their share; layering gives granular observability and enforcement |
| Fail-CLOSED default (one fail-open exception) | All 11 guardrails fail-closed; G9 (LLM budget) fails open | Uniform fail-open or uniform fail-closed | Uncontrolled outreach is not recoverable; bounded overspend on inference is. Asymmetric risk requires asymmetric policy. |
| Multiple persistence models | DynamoDB + SQS + JSONL + Neo4j | Single universal database | Queue semantics, keyed state, append-only audit, and graph traversal have different operational requirements; one store forces compromises on all four |
| Constrained graph schema | Explicit labels and bounded Cypher | Arbitrary ontology and open-ended query surface | Arbitrary mutation makes graph behavior unauditable and complicates authorization |
| Immutable baseline | Hash-verified protected files at boot (identity + governance code) | Trust that governance files remain unchanged | Silent modification of the baseline is the highest-impact low-visibility attack surface; verify at startup |
| Recovery directory separation | `recovery/` explicitly excluded from active architecture | Git history as the only record of superseded designs | Preserves the context of why something was replaced, without creating confusion about what is running |

See [`docs/adr/`](../docs/adr/) for individual decision records, and [`ENGINEERING_DECISIONS.md`](../ENGINEERING_DECISIONS.md) for first-person reasoning behind each choice.
