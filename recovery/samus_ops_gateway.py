#!/usr/bin/env python3
"""
SAMUS Ops Gateway — phone-accessible control plane
Source: ChatGPT recovery chat 20 (full design + tightening feedback)

Canonical relationship:
- [NEW] operator-facing control plane on dedicated edge port (8100)
- [EXPANDS §6 application] mobile-mediated job submission + approval
- [EXPANDS §6 inter_agent] internal-only gateway→worker mesh (Tailscale Serve outside)
- Pairs with samus_ops_schema.sql for state storage

Architecture:
  Phone (Tailscale tailnet only)
    ↓ HTTPS
  Tailscale Serve (private to tailnet)
    ↓ http://127.0.0.1:8100
  samus-gateway (FastAPI + SQLite + UI)
    ↓ samus-internal Docker network (NO host port exposure)
  worker-leadgen / worker-scaffold / worker-fulfillment / worker-prospect

Hard rules:
  - Phone NEVER touches workers directly (gateway-mediated only)
  - Phone NEVER gets raw Docker daemon access
  - Workers have NO internet access, NO public ports
  - Every gateway→worker call HMAC-signed (Layer 2 hardening)

Required job-state machine (per chat 20 tightening):
  queued → dispatched → running → {succeeded | failed | halted | timed_out}
                     └→ awaiting_approval → {approved | rejected}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, List, Optional


JOB_STATUSES = ("queued", "dispatched", "running", "awaiting_approval",
                "approved", "rejected", "succeeded", "failed", "halted", "timed_out")

APPROVAL_TYPES = ("publish_website_change", "execute_stripe_payload",
                  "deliver_client_handoff", "promote_product_status")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _payload_hash(payload: Dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _sign_internal_request(body: bytes, secret: str) -> Dict[str, str]:
    """HMAC-SHA256 signing for gateway→worker calls (Layer 2 hardening)."""
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    mac = hmac.new(secret.encode(), (ts + nonce).encode() + body, hashlib.sha256).hexdigest()
    return {"X-Samus-Timestamp": ts, "X-Samus-Nonce": nonce, "X-Samus-Signature": mac}


class OpsGateway:
    """Skeleton — full FastAPI integration in target deployment env."""

    def __init__(self, db_path: str, internal_secret: str, worker_routes: Dict[str, str]):
        self.db_path = db_path
        self.internal_secret = internal_secret
        self.worker_routes = worker_routes      # {"leadgen": "http://worker-leadgen:8200", ...}
        self._init_db()

    def _init_db(self) -> None:
        schema_path = os.path.join(os.path.dirname(__file__), "samus_ops_schema.sql")
        if not os.path.exists(schema_path):
            return
        conn = sqlite3.connect(self.db_path)
        try:
            with open(schema_path) as f:
                conn.executescript(f.read())
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    # ----- public endpoints (FastAPI routes wire these) -----
    def submit_job(self, job_type: str, params: Dict[str, Any], require_approval: bool = False) -> Dict[str, Any]:
        if job_type not in self.worker_routes:
            raise ValueError(f"Unknown job_type: {job_type}")
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs(id, job_type, params_json, status, require_approval, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (job_id, job_type, json.dumps(params), "queued", int(require_approval), now, now),
            )
        if not require_approval:
            self._dispatch_run(job_id, job_type, params)
        return {"job_id": job_id, "status": "queued"}

    def _dispatch_run(self, job_id: str, job_type: str, params: Dict[str, Any]) -> str:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs(id, job_id, worker_name, status, started_at, last_heartbeat_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (run_id, job_id, job_type, "dispatched", now, now),
            )
            c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", ("dispatched", now, job_id))
        # Actual HTTP POST to worker (with HMAC sig) happens in deployment-env code.
        return run_id

    def list_jobs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as c:
            if status:
                rows = c.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def dashboard(self) -> Dict[str, Any]:
        with self._conn() as c:
            active = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN('queued','dispatched','running')").fetchone()[0]
            halted = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN('halted','timed_out')").fetchone()[0]
            pending_approvals = c.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
            stale_threshold = time.time() - 300        # 5 min heartbeat staleness
            stale = c.execute(
                "SELECT COUNT(*) FROM runs WHERE status='running' AND last_heartbeat_at < datetime(?, 'unixepoch')",
                (stale_threshold,),
            ).fetchone()[0]
        return {
            "active_jobs": active,
            "halted_jobs": halted,
            "pending_approvals": pending_approvals,
            "stale_runs": stale,
        }

    def request_approval(self, job_id: str, approval_type: str, payload: Dict[str, Any],
                         requested_by: str, expires_in_sec: int = 3600) -> Dict[str, Any]:
        if approval_type not in APPROVAL_TYPES:
            raise ValueError(f"Unknown approval_type: {approval_type}")
        approval_id = f"app-{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in_sec))
        with self._conn() as c:
            c.execute(
                "INSERT INTO approvals(id, job_id, approval_type, payload_hash, requested_by, "
                "expires_at, status, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, job_id, approval_type, _payload_hash(payload),
                 requested_by, expires_at, "pending", now),
            )
            c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", ("awaiting_approval", now, job_id))
        return {"approval_id": approval_id, "status": "pending", "expires_at": expires_at}

    def list_pending_approvals(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def decide_approval(self, approval_id: str, decision: str, decided_by: str) -> Dict[str, Any]:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved|rejected")
        now = _now_iso()
        with self._conn() as c:
            row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row:
                raise LookupError("approval not found")
            if row["status"] != "pending":
                raise ValueError(f"approval already {row['status']}")
            c.execute("UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=?",
                      (decision, now, decided_by, approval_id))
            c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (decision, now, row["job_id"]))
        return {"approval_id": approval_id, "status": decision}


# Suggested FastAPI app skeleton (target deployment):
def create_app(db_path: str, internal_secret: str, auth_secret: str, worker_routes: Dict[str, str]):
    try:
        from fastapi import FastAPI, Header, HTTPException
    except ImportError:
        return None
    gw = OpsGateway(db_path, internal_secret, worker_routes)
    app = FastAPI(title="samus-ops-gateway")

    def _require_auth(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing token")
        if authorization[7:] != auth_secret:
            raise HTTPException(401, "invalid token")

    @app.get("/health")
    def health(): return {"status": "ok"}

    @app.get("/dashboard")
    def dashboard(authorization: str = Header(None)):
        _require_auth(authorization)
        return gw.dashboard()

    @app.post("/jobs/submit")
    def submit(body: Dict[str, Any], authorization: str = Header(None)):
        _require_auth(authorization)
        return gw.submit_job(body["job_type"], body.get("params", {}),
                             bool(body.get("require_approval", False)))

    @app.get("/approvals/pending")
    def pending(authorization: str = Header(None)):
        _require_auth(authorization)
        return gw.list_pending_approvals()

    @app.post("/approvals/{approval_id}")
    def decide(approval_id: str, body: Dict[str, Any], authorization: str = Header(None)):
        _require_auth(authorization)
        return gw.decide_approval(approval_id, body["decision"], body.get("decided_by", "operator"))

    return app
