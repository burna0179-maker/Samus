"""Runs INSIDE samus-outreach (has the SendGrid key + send_email + the
host_artifacts bind). Sweeps the client check-in queue and sends any draft
whose 24h hold has elapsed and that the operator hasn't stopped.

Policy (operator-chosen 2026-06-30): draft -> 24h grace -> auto-send unless
stopped. STOP = set the entry's "status" to "stopped" in the queue, OR delete
the draft .md file (this sweep treats a missing draft file as stopped).

Sends as message_kind="transactional" (relationship mail to an existing
customer — exempt from the CAN-SPAM marketing footer, correct for a personal
note). Idempotent: only status=="pending" past send_after is sent; then marked.
Fail-soft per entry. Prints a JSON summary.

Queue: /opt/samus/data/artifacts/seo_clients/checkin_queue.jsonl
Env: CHECKIN_DRY_RUN=1 to simulate (no send).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

ART = "/opt/samus/data/artifacts"
QUEUE = f"{ART}/seo_clients/checkin_queue.jsonl"
DRY = os.environ.get("CHECKIN_DRY_RUN", "").strip() in ("1", "true", "yes")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_draft(full_path: str) -> tuple[str, str]:
    """Read subject + body from the LIVE draft .md at send time (so operator
    edits up to the last moment are honored). Subject = text after
    '**Subject:**'; body = everything after the first '---' divider."""
    text = open(full_path, encoding="utf-8-sig").read()
    subject = ""
    for line in text.splitlines():
        if line.strip().lower().startswith("**subject:**"):
            subject = line.split("**", 2)[-1].replace("Subject:", "").strip()
            break
    body = ""
    if "\n---\n" in text:
        body = text.split("\n---\n", 1)[1].strip()
    elif "---" in text:
        body = text.split("---", 1)[1].strip()
    return subject, body


CLIENTS_PATH = f"{ART}/seo_clients/clients.json"
MAX_PER_SWEEP = int(os.environ.get("SAMUS_CHECKIN_MAX_PER_SWEEP", "5") or 5)
SENDS_DISABLED = os.environ.get("SAMUS_SENDS_DISABLED", "").strip().lower() in ("1", "true", "yes")


def _load_allowlist() -> dict:
    """customer_id -> registered email (lowercased). Check-ins may ONLY go to a
    REGISTERED client at their on-file address (red-team HIGH: arbitrary-recipient
    injection via the mutable queue). Fail-closed: if the registry can't be read,
    the allowlist is empty and every send is blocked."""
    try:
        reg = json.load(open(CLIENTS_PATH, encoding="utf-8-sig"))
        return {
            str(c.get("customer_id")): str(c.get("email", "")).strip().lower()
            for c in reg.get("clients", []) if c.get("customer_id") and c.get("email")
        }
    except Exception:
        return {}


def _safe_draft_path(dp: str):
    """Resolve draft_path under ART and confine it (reject absolute path / '..'
    escape). Returns the real path if inside ART, else None (red-team HIGH: path
    traversal -> arbitrary in-container file read, exfiltrated as the email body)."""
    if not dp:
        return None
    art_real = os.path.realpath(ART)
    full = os.path.realpath(os.path.join(art_real, dp))
    if full == art_real or full.startswith(art_real + os.sep):
        return full
    return None


def main() -> int:
    if SENDS_DISABLED:
        print(json.dumps({"sent": 0, "disabled": True, "reason": "SAMUS_SENDS_DISABLED"}))
        return 0
    if not os.path.exists(QUEUE):
        print(json.dumps({"queue": "missing", "sent": 0}))
        return 0
    rows = []
    for line in open(QUEUE, encoding="utf-8-sig"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue

    now = _now()
    allow = _load_allowlist()
    sent = 0
    stopped = 0
    skipped = 0
    blocked = 0
    details = []
    changed = False

    for r in rows:
        if r.get("status") != "pending":
            skipped += 1
            continue
        sa = _parse(r.get("send_after", ""))
        if sa is None or now < sa:
            skipped += 1
            continue
        # RECIPIENT ALLOWLIST (red-team HIGH): only a registered client at their
        # on-file address. Neutralizes arbitrary-recipient injection via the queue.
        to = str(r.get("to") or "").strip().lower()
        want = allow.get(str(r.get("customer_id") or ""))
        if not want or to != want:
            r["status"] = "blocked_recipient"
            blocked += 1
            changed = True
            details.append({"to": r.get("to"), "result": "BLOCKED: recipient not allowlisted for customer_id"})
            continue
        # PATH CONFINEMENT (red-team HIGH): draft_path must resolve inside ART.
        full = _safe_draft_path(r.get("draft_path", ""))
        if full is None:
            r["status"] = "blocked_path"
            blocked += 1
            changed = True
            details.append({"to": r.get("to"), "result": "BLOCKED: draft_path escapes artifacts dir"})
            continue
        # RATE CAP (blue-team): never send more than MAX_PER_SWEEP per sweep — a
        # queue-flood spike guard. Excess defers to the next sweep.
        if sent >= MAX_PER_SWEEP:
            skipped += 1
            details.append({"to": r.get("to"), "result": f"deferred: per-sweep cap {MAX_PER_SWEEP} reached"})
            continue
        # STOP-by-delete: if the draft file was removed, treat as cancelled.
        if not os.path.exists(full):
            r["status"] = "stopped_deleted"
            stopped += 1
            changed = True
            details.append({"to": r.get("to"), "result": "stopped (draft deleted)"})
            continue
        # Read the LIVE draft at send time (honors operator edits until now).
        subject, body = _read_draft(full)
        if not body:
            skipped += 1
            details.append({"to": r.get("to"), "result": "skipped (empty draft body)"})
            continue
        if DRY:
            details.append({"to": r.get("to"), "result": "DRY_RUN would send", "subject": subject})
            continue
        try:
            from backend.common.email_backend import send_email
            res = send_email(
                to=r["to"],
                subject=subject,
                body=body,
                from_name=r.get("from_name") or "HustleForge",
                reply_to=r.get("reply_to") or None,
                message_kind="transactional",
            )
            r["status"] = "sent"
            r["sent_at"] = _now().isoformat()
            r["message_id"] = res.get("message_id", "")
            sent += 1
            changed = True
            details.append({"to": r.get("to"), "result": "sent", "message_id": res.get("message_id", "")})
        except Exception as exc:  # noqa: BLE001
            r["status"] = "send_failed"
            r["error"] = f"{type(exc).__name__}: {exc}"[:200]
            changed = True
            details.append({"to": r.get("to"), "result": f"FAILED: {type(exc).__name__}"})

    if changed and not DRY:
        tmp = QUEUE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, QUEUE)

    # Spike/abuse signal: blocked recipients/paths or hitting the per-sweep cap
    # is anomalous — surface it loudly for the operator (blue-team gap #1: no
    # real-time alerting). A queue-flood or poisoned entry lands here.
    alert = blocked > 0 or sent >= MAX_PER_SWEEP
    out = {"sent": sent, "stopped": stopped, "skipped": skipped, "blocked": blocked,
           "dry_run": DRY, "details": details}
    if alert:
        out["ALERT"] = (f"check-in send anomaly: blocked={blocked} "
                        f"sent={sent}/cap={MAX_PER_SWEEP} — review checkin_queue.jsonl")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
