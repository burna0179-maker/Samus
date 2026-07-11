-- SAMUS Ops Schema — SQLite control plane
-- Source: ChatGPT recovery chat 20 (phone-access ops gateway design)
--
-- Canonical relationship:
--   [NEW] operator control plane sitting in front of canonical workers
--   [EXPANDS §6 application] mobile-accessible ops surface (Tailscale Serve)
--   [EXPANDS §6 data] job + run + approval + artifact registry
--
-- Job lifecycle states:
--   queued → dispatched → running → {succeeded | failed | halted | timed_out}
--                       └→ awaiting_approval → {approved | rejected}
--
-- Job vs Run distinction:
--   job_id = operator/business request (1:N runs over time — retries, re-dispatch)
--   run_id = actual execution instance (heartbeat-tracked)

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,           -- job_id
    job_type            TEXT NOT NULL,              -- leadgen | scaffold | fulfillment | prospect | seo
    params_json         TEXT NOT NULL,              -- JSON-encoded params
    status              TEXT NOT NULL,              -- queued | dispatched | running | awaiting_approval
                                                    --   | approved | rejected | succeeded | failed
                                                    --   | halted | timed_out
    require_approval    INTEGER NOT NULL DEFAULT 0, -- bool
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);

CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,           -- run_id
    job_id              TEXT NOT NULL REFERENCES jobs(id),
    worker_name         TEXT NOT NULL,              -- leadgen | scaffold | fulfillment | ...
    status              TEXT NOT NULL,              -- (same enum as job status)
    started_at          TEXT,
    finished_at         TEXT,
    last_heartbeat_at   TEXT,                       -- for timeout detection
    summary             TEXT,
    error_json          TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_job_id ON runs(job_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_heartbeat ON runs(last_heartbeat_at);

CREATE TABLE IF NOT EXISTS approvals (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL REFERENCES jobs(id),
    run_id              TEXT REFERENCES runs(id),
    approval_type       TEXT NOT NULL,              -- publish_website_change | execute_stripe_payload
                                                    --   | deliver_client_handoff | promote_product_status
    payload_hash        TEXT NOT NULL,              -- SHA256 of approval-scoped payload (tamper detect)
    requested_by        TEXT NOT NULL,
    reason              TEXT,
    expires_at          TEXT,                       -- explicit expiry — prevents stale approvals
    allowed_executions  INTEGER NOT NULL DEFAULT 1, -- consume-on-approve semantics
    executions_used     INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,              -- pending | approved | rejected | expired | consumed
    decided_at          TEXT,
    decided_by          TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_job ON approvals(job_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(id),
    kind                TEXT NOT NULL,              -- leads_jsonl | product_handoff | seo_audit | invoice
    path                TEXT NOT NULL,              -- relative path under /data/...
    metadata_json       TEXT,
    sha256              TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT REFERENCES runs(id),
    job_id              TEXT REFERENCES jobs(id),
    level               TEXT NOT NULL,              -- debug | info | warn | error | audit
    event_type          TEXT NOT NULL,              -- run_started | run_completed | approval_required
                                                    --   | heartbeat_stale | autonomy_downgraded
    message             TEXT,
    metadata_json       TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

-- Required worker response contract:
-- {
--   "status": "succeeded" | "failed" | "awaiting_approval" | "halted",
--   "summary": str,
--   "artifacts": [{"kind": str, "path": str, "metadata": dict}],
--   "metrics": dict,
--   "approval_request": dict | None,
--   "error": dict | None
-- }
