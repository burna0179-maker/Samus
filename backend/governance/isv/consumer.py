"""IsvConsumer — Major-signed ISV intake + in-scope predicate.

Per directed_capability.protocol.yaml v0.6.0:
  - Only Major may sign ISVs (Ed25519, verified against Major's pubkey).
  - At most one ISV active at a time; new ISV deprecates the previous.
  - Max lifetime 24h; expired ISV -> no_active_isv refusal.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.governance.isv")

_SAMUS_ROOT = Path(__file__).resolve().parents[3]
# ISV active-record + dispatch inbox persist on the writable data volume
# in-container (the image root /opt/samus is read-only). See state_paths.py.
_ACTIVE_ISV_DIR = state_path("active_isv")
_DISPATCH_INBOX = state_path("dispatch_inbox")


class ProtocolViolation(RuntimeError):
    pass


class NoActiveIsvError(RuntimeError):
    pass


class IsvConsumer:
    def __init__(self, *, major_pubkey_path=None, env: str = "development") -> None:
        self._env = env
        self._active_dir = _ACTIVE_ISV_DIR
        self._inbox = _DISPATCH_INBOX
        self._active_dir.mkdir(parents=True, exist_ok=True)
        self._major_pubkey: Ed25519PublicKey | None = None
        if major_pubkey_path:
            p = Path(major_pubkey_path)
            if p.is_file():
                try:
                    self._major_pubkey = load_pem_public_key(p.read_bytes())
                except Exception as exc:  # noqa: BLE001
                    _LOG.error("samus.isv.pubkey_load_failed: %s", exc)
                    if env in ("production", "prod"):
                        raise
        elif env in ("production", "prod"):
            raise RuntimeError(
                "samus.isv.missing_major_pubkey: prod refuses without SAMUS_MAJOR_PUBLIC_KEY_PATH"
            )

    def poll_inbox(self) -> dict | None:
        """Scan local-disk inbox for kind=directed_capability.isv envelopes."""
        if not self._inbox.is_dir():
            return None
        latest: tuple[float, Path] | None = None
        for f in self._inbox.glob("*.yaml"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if latest is None or mt > latest[0]:
                latest = (mt, f)
        if latest is None:
            return None
        try:
            env_doc = yaml.safe_load(latest[1].read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("samus.isv.inbox_read_failed: %s", exc)
            return None
        if not isinstance(env_doc, dict):
            return None
        if env_doc.get("kind") != "directed_capability.isv":
            return None
        if env_doc.get("addressed_to") not in (None, "samus"):
            return None
        isv = env_doc.get("payload") or env_doc.get("isv") or env_doc
        try:
            return self.accept(isv, signature=env_doc.get("signature"))
        except ProtocolViolation as exc:
            _LOG.error("samus.isv.rejected: %s", exc)
            return None

    def accept(self, isv: dict, *, signature=None) -> dict:
        if not isinstance(isv, dict):
            raise ProtocolViolation("isv_not_a_mapping")
        if isv.get("issued_by") != "major":
            raise ProtocolViolation("isv_issuer_must_be_major")
        isv_id = isv.get("isv_id")
        if not isv_id:
            raise ProtocolViolation("isv_missing_id")
        expires_at = isv.get("expires_at")
        if not expires_at or _parse_ts(expires_at) <= _utcnow():
            raise ProtocolViolation("isv_expired")
        if self._major_pubkey is not None:
            if not signature:
                raise ProtocolViolation("isv_signature_missing")
            payload = _canonical_payload(isv)
            sig_bytes = (
                signature
                if isinstance(signature, bytes)
                else bytes.fromhex(signature.removeprefix("ed25519:").strip())
            )
            try:
                self._major_pubkey.verify(sig_bytes, payload)
            except InvalidSignature as exc:
                raise ProtocolViolation("isv_signature_invalid") from exc
        for f in self._active_dir.glob("*.yaml"):
            try:
                f.unlink()
            except OSError:
                pass
        (self._active_dir / f"{isv_id}.yaml").write_text(
            yaml.safe_dump(isv, sort_keys=False),
            encoding="utf-8",
        )
        _LOG.info("samus.isv.accepted: id=%s expires=%s", isv_id, expires_at)
        return isv

    def get_active_isv(self) -> dict | None:
        for f in self._active_dir.glob("*.yaml"):
            try:
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue
            exp = doc.get("expires_at")
            if exp and _parse_ts(exp) > _utcnow():
                return doc
            try:
                f.unlink()
            except OSError:
                pass
        return None

    def is_action_in_scope(self, action_class: str, action_payload=None):
        isv = self.get_active_isv()
        if isv is None:
            return False, "no_active_isv"
        goals = isv.get("assigned_goals") or []
        if goals:
            allowed: set[str] = set()
            for g in goals:
                if isinstance(g, dict):
                    allowed.update(g.get("allowed_action_classes") or [])
                    if g.get("action_class"):
                        allowed.add(g["action_class"])
                elif isinstance(g, str):
                    allowed.add(g)
            if allowed and action_class not in allowed:
                return False, f"action_class_not_in_assigned_goals:{action_class}"
        budget = isv.get("resource_budget") or {}
        if isinstance(action_payload, dict):
            est = action_payload.get("estimated_cost_usd")
            cap = budget.get("llm_spend_max_usd_session")
            if est is not None and cap is not None:
                try:
                    if float(est) > float(cap):
                        return False, "estimated_cost_exceeds_isv_budget"
                except (TypeError, ValueError):
                    pass
        return True, "in_scope"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_ts(s: str) -> _dt.datetime:
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _canonical_payload(isv: dict) -> bytes:
    redacted = {k: v for k, v in isv.items() if k not in ("signature", "signed_envelope")}
    return json.dumps(redacted, sort_keys=True, separators=(",", ":")).encode("utf-8")
