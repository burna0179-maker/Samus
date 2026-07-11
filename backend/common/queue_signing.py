"""Per-message HMAC authenticity for SQS QueueEnvelopes (FIN-03).

SQS gives consumers only IAM-level trust: anyone holding ``sqs:SendMessage``
on a queue can inject an arbitrary CRM / fulfillment / finance action (forge a
close, an amount, a task). This module adds a per-message HMAC over the
envelope's stable content so a worker can prove a message was produced by a
holder of the shared inter-service HMAC key — not merely by *someone* with
queue-send IAM.

Keying reuses the existing inter-service HMAC material
(``security.resolve_service_key``): the SQS dispatcher is the gateway, so it
signs as the ``gateway`` caller; a worker resolves the same key to verify.
The shared key already lives in settings (``shared_hmac_key`` /
``SAMUS_HMAC_KEY_<SERVICE>``), so no new secret is introduced.

SAFE ROLLOUT — the control ships dormant. Enforcement is gated behind
``SAMUS_SQS_REQUIRE_HMAC`` (default OFF):

* OFF (audit mode, default): the dispatcher still SIGNS every envelope, and a
  worker VERIFIES and LOGS a mismatch/absence, but processes the message
  anyway. This lets every producer deploy the signing path with zero risk of
  dropping in-flight or legacy unsigned traffic.
* ON (enforce mode): the worker rejects unsigned / invalid messages
  fail-closed (the caller treats it as poison and does not act).

The canonical signed string is deterministic across producer + consumer:

    task_id \n service \n action \n trace_id \n idempotency_key \n
    sha256(canonical_json(payload)) \n sha256(canonical_json(metadata))

``None`` for ``trace_id`` / ``idempotency_key`` is encoded as the empty
string. ``canonical_json`` sorts keys + uses compact separators so both sides
hash byte-identical content. The ``hmac`` field is itself excluded from the
canonical string (it is the output, not an input).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from . import security
from .queue_contracts import QueueEnvelope

_LOG = logging.getLogger("samus.queue_signing")

# The producer caller identity SQS messages are signed as. The gateway is the
# sole SQS dispatcher today; binding the key to a fixed caller keeps producer
# and consumer key-resolution identical without trusting an in-band field.
_SQS_SIGNER_CALLER = "gateway"


def _canonical_json(obj: dict[str, Any]) -> str:
    """Deterministic JSON: sorted keys, compact separators, str-coerced."""
    return json.dumps(
        obj or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def envelope_canonical_string(envelope: QueueEnvelope) -> str:
    """The canonical string that the HMAC is computed over (excludes ``hmac``)."""
    payload_hash = hashlib.sha256(
        _canonical_json(envelope.payload).encode("utf-8")
    ).hexdigest()
    metadata_hash = hashlib.sha256(
        _canonical_json(envelope.metadata).encode("utf-8")
    ).hexdigest()
    parts = [
        envelope.task_id,
        envelope.service,
        envelope.action,
        envelope.trace_id or "",
        envelope.idempotency_key or "",
        payload_hash,
        metadata_hash,
    ]
    return "\n".join(parts)


def _resolve_sqs_key() -> str:
    """Resolve the HMAC key that signs/verifies SQS traffic.

    Reuses ``security.resolve_service_key`` against the gateway caller so the
    same key the inter-service HTTP path uses also covers the queue path. The
    shared key is read off the cached settings object (live via
    ``reload_settings``). Returns ``""`` when no key is configured at all.
    """
    try:
        from .settings import get_settings

        shared = getattr(get_settings(), "shared_hmac_key", "") or ""
    except Exception:  # noqa: BLE001 — settings unavailable -> no shared key
        shared = ""
    return security.resolve_service_key(_SQS_SIGNER_CALLER, shared)


def sign_envelope(envelope: QueueEnvelope) -> QueueEnvelope:
    """Return ``envelope`` with its ``hmac`` field populated.

    When no HMAC key is configured the envelope is returned unchanged
    (``hmac`` stays ``None``) — signing is best-effort on the producer so a
    key-less local stack keeps working; enforcement on the consumer is the
    fail-closed control.
    """
    key = _resolve_sqs_key()
    if not key:
        return envelope
    sig = security.sign_request(
        secret=key,
        method="SQS",
        path=f"/{envelope.service}",
        timestamp="",
        nonce="",
        body=envelope_canonical_string(envelope).encode("utf-8"),
        caller=_SQS_SIGNER_CALLER,
    )
    envelope.hmac = sig
    return envelope


def enforce_hmac() -> bool:
    """True iff ``SAMUS_SQS_REQUIRE_HMAC`` arms enforce mode (default OFF).

    Read live from the environment so an operator can arm enforcement after
    every producer has deployed the signing path, with only a worker restart.
    """
    raw = (os.getenv("SAMUS_SQS_REQUIRE_HMAC", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def verify_envelope(envelope: QueueEnvelope) -> bool:
    """True iff ``envelope.hmac`` is present and matches the recomputed HMAC.

    A constant-time compare guards the signature check. An absent ``hmac`` or
    an absent key returns False — the *caller* decides what that means based on
    :func:`enforce_hmac` (audit: log + process; enforce: reject).
    """
    sig = envelope.hmac
    if not sig:
        return False
    key = _resolve_sqs_key()
    if not key:
        return False
    expected = security.sign_request(
        secret=key,
        method="SQS",
        path=f"/{envelope.service}",
        timestamp="",
        nonce="",
        body=envelope_canonical_string(envelope).encode("utf-8"),
        caller=_SQS_SIGNER_CALLER,
    )
    return security.safe_compare(sig, expected)


def check_message_authenticity(envelope: QueueEnvelope, *, service: str) -> bool:
    """Verify + apply the audit/enforce policy. True iff the message may proceed.

    * enforce OFF (audit, default): always returns True; a verification
      failure is LOGGED (so operators can confirm every producer is signing
      before arming enforcement) but never blocks.
    * enforce ON: returns the verification result; a failure means the caller
      must treat the message as poison and NOT act on it.
    """
    ok = verify_envelope(envelope)
    if ok:
        return True
    enforcing = enforce_hmac()
    reason = "missing_hmac" if not envelope.hmac else "bad_hmac"
    if enforcing:
        _LOG.error(
            "sqs_message_hmac_rejected",
            extra={"service": service, "reason": reason,
                   "task_id": envelope.task_id, "action": envelope.action},
        )
        return False
    _LOG.warning(
        "sqs_message_hmac_audit_mismatch",
        extra={"service": service, "reason": reason,
               "task_id": envelope.task_id, "action": envelope.action,
               "mode": "audit"},
    )
    return True


__all__ = [
    "envelope_canonical_string",
    "sign_envelope",
    "verify_envelope",
    "enforce_hmac",
    "check_message_authenticity",
]
