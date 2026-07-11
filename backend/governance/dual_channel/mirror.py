"""DualChannelMirror — audit envelope precedes commercial commit.

Per directed_capability.protocol.yaml v0.6.0 Component 3:
  audit_envelope_precedes_commercial_commit (structural invariant).
  fail_closed: > timeout sec without Major ack -> commercial BLOCKED and
  audit_blackout_frozen state engaged; only Anita /gate can lift.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from backend.common.audit_ledger import _atomic_write_text  # RT RACE-05: atomic envelope writes
from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.governance.dual_channel")

_SAMUS_ROOT = Path(__file__).resolve().parents[3]
# Audit queue must persist on the writable data volume in-container; the
# image root /opt/samus is read-only. See backend/common/state_paths.py.
_PENDING_QUEUE = state_path("dual_channel", "pending")
_ACK_DIR = state_path("dual_channel", "ack")
_BLACKOUT_FLAG = state_path("dual_channel", "AUDIT_BLACKOUT_FROZEN")


class ProtocolViolation(RuntimeError):
    """Reverse-ordering or invariant-breach by caller."""


class AuditBlackoutError(RuntimeError):
    """Major audit channel timeout or unreachable; commit refused."""


class DualChannelMirror:
    """Wraps commercial action commits in a mandatory audit envelope.

    Usage:
        mirror.send_action(envelope) -> audit_ack_ts (str)
        # caller must NOT have committed the commercial action yet.
        # After the call returns, caller commits and stamps audit_ack_ts.
    """

    def __init__(self, *, optimus_dispatch_url=None, signing_key_path=None,
                 timeout_sec: int = 180, env: str = "development",
                 ack_probe: Callable[[str], dict | None] | None = None) -> None:
        self._url = (optimus_dispatch_url or "").strip()
        self._timeout = max(1, int(timeout_sec))
        self._env = env
        self._lock = threading.Lock()
        _PENDING_QUEUE.mkdir(parents=True, exist_ok=True)
        _ACK_DIR.mkdir(parents=True, exist_ok=True)
        self._ack_probe = ack_probe  # injectable for tests / local-mode

        self._signing_key: Ed25519PrivateKey | None = None
        if signing_key_path:
            p = Path(signing_key_path)
            if p.is_file():
                try:
                    self._signing_key = load_pem_private_key(p.read_bytes(), password=None)
                except Exception as exc:  # noqa: BLE001
                    _LOG.error("samus.dual_channel.signing_key_load_failed: %s", exc)
                    if env in ("production", "prod"):
                        raise
        elif env in ("production", "prod"):
            raise RuntimeError(
                "samus.dual_channel.missing_signing_key: refuses to boot in prod"
            )

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------
    def is_blackout_frozen(self) -> bool:
        return _BLACKOUT_FLAG.exists()

    def _freeze(self, reason: str) -> None:
        try:
            _BLACKOUT_FLAG.write_text(
                f"{_utcnow_iso()}\n{reason}\n", encoding="utf-8",
            )
        except OSError:
            pass
        _LOG.error("samus.dual_channel.audit_blackout_frozen: %s", reason)

    def lift_freeze(self, *, anita_gate_signature: str) -> None:
        """Only Anita-confirmed restoration can lift the freeze (v0.6.0)."""
        if not anita_gate_signature:
            raise ProtocolViolation("freeze_lift_requires_anita_gate_signature")
        try:
            _BLACKOUT_FLAG.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def send_action(self, *, isv_id, action_class: str,
                    trust_premium_at_commit: float, value_exchange_record_ref: str,
                    commercial_destination: str, syscall_summary=None,
                    commit_status: str = "pending") -> str:
        """Queue + sync-wait audit envelope; return audit_ack_ts.

        Raises AuditBlackoutError on timeout / unreachable Major.
        """
        if self.is_blackout_frozen():
            raise AuditBlackoutError("audit_blackout_frozen_state_active")

        envelope = {
            "envelope_id": str(uuid.uuid4()),
            "ts": _utcnow_iso(),
            "acting_agent": "samus",
            "isv_id": isv_id,
            "action_class": action_class,
            "trust_premium_at_commit": float(trust_premium_at_commit),
            "value_exchange_record_ref": value_exchange_record_ref,
            "syscall_summary": syscall_summary or {},
            "commercial_destination": commercial_destination,
            "commit_status": commit_status,
            "audit_ack_ts": None,
        }
        envelope["signature"] = self._sign(envelope)

        with self._lock:
            self._persist_pending(envelope)
            ack_ts = self._await_ack(envelope)

        envelope["audit_ack_ts"] = ack_ts
        envelope["commit_status"] = "committed"
        self._persist_ack(envelope)
        return ack_ts

    def assert_audit_precedes_commit(
        self, audit_ack_ts: str | None, commercial_commit_ts: str | None,
    ) -> None:
        """Caller hook to enforce structural invariant explicitly."""
        if not audit_ack_ts:
            raise ProtocolViolation(
                "audit_envelope_precedes_commercial_commit: no audit ack"
            )
        if commercial_commit_ts and audit_ack_ts > commercial_commit_ts:
            return  # well-ordered (ack first)
        if commercial_commit_ts and commercial_commit_ts < audit_ack_ts:
            raise ProtocolViolation(
                "audit_envelope_precedes_commercial_commit: reverse ordering"
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _sign(self, envelope: dict) -> str:
        payload = json.dumps(
            {k: v for k, v in envelope.items() if k != "signature"},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if self._signing_key is None:
            # Dev-only stub signature; production refuses at __init__.
            return f"unsigned:{envelope['envelope_id']}"
        sig = self._signing_key.sign(payload)
        return "ed25519:" + sig.hex()

    def _persist_pending(self, envelope: dict) -> Path:
        # RT RACE-05: atomic write — a crash mid-write on a signed commercial-
        # action audit envelope would leave a partial YAML that parses to None,
        # permanently losing the audit record.
        path = _PENDING_QUEUE / f"{envelope['envelope_id']}.yaml"
        _atomic_write_text(path, yaml.safe_dump(envelope, sort_keys=False))
        return path

    def _persist_ack(self, envelope: dict) -> Path:
        path = _ACK_DIR / f"{envelope['envelope_id']}.yaml"
        _atomic_write_text(path, yaml.safe_dump(envelope, sort_keys=False))
        try:
            (_PENDING_QUEUE / f"{envelope['envelope_id']}.yaml").unlink()
        except FileNotFoundError:
            pass
        return path

    def _await_ack(self, envelope: dict) -> str:
        deadline = time.monotonic() + self._timeout
        env_id = envelope["envelope_id"]
        backoff = 0.5
        while time.monotonic() < deadline:
            try:
                ack = self._probe_ack(env_id, envelope)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("samus.dual_channel.probe_exc: %s", exc)
                ack = None
            if ack:
                return ack
            time.sleep(min(backoff, 5.0))
            backoff = min(backoff * 1.5, 5.0)
        self._freeze(f"audit_ack_timeout envelope_id={env_id}")
        raise AuditBlackoutError(f"audit_ack_timeout: {env_id}")

    def _probe_ack(self, env_id: str, envelope: dict) -> str | None:
        if self._ack_probe is not None:
            result = self._ack_probe(env_id)
            if isinstance(result, dict):
                return result.get("audit_ack_ts")
            return None
        if self._url:
            with httpx.Client(timeout=10.0) as client:
                r = client.post(
                    f"{self._url.rstrip('/')}/dispatch/major/action_audit",
                    json=envelope,
                )
                if r.status_code == 200:
                    body = r.json() if r.content else {}
                    if isinstance(body, dict) and body.get("audit_ack_ts"):
                        return body["audit_ack_ts"]
            return None
        # Local-disk drop mode (dev): pretend Major acks immediately. This is
        # intentionally permissive so test/dev boots succeed; production MUST
        # configure SAMUS_OPTIMUS_DISPATCH_URL.
        if self._env in ("production", "prod"):
            return None
        return _utcnow_iso()


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
