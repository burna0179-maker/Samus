"""Agora contribution endpoint (Samus side) — Agora v2 participant.

Anita (the Agora moderator) POSTs an HMAC-signed AgentEnvelope to each peer at
``POST /agora/contribute`` carrying ``{deliberation_id, topic, round, ts}``; the
peer returns its UNIQUELY-HELD, source-tagged evidence + a stance grounded in it
(AGORA-1). Samus contributes its **commercial / resource reality** — the one piece
of evidence no other agent holds: Samus runs an Anthropic-only inference path and
does **not** contend for the local RTX 4090, plus the outbound guardrail posture
of the revenue engine. That is real, distinct evidence (for the seed GPU-
serialization topic it is directly relevant: Samus is a non-contender).

Verification mirrors ``quorum_vote_route._verify_major_envelope`` exactly but for
the Agora collector == ``anita`` (``RotatingHMACKey.for_agent("anita")`` reads
``SS_HMAC_KEY_ANITA``). Fail-closed: 401 missing, 403 forged / wrong sender,
503 key-unprovisioned. Gated by ``SN_AGORA_CONTRIBUTE_ENABLED`` (default OFF) —
a disabled endpoint never errors; it returns an honest ``have_evidence=False``
contribution so Anita's collector simply sees fewer distinct evidences.

Samus runs no local LLM for this path, so the stance is DETERMINISTIC.

NOTE: no ``from __future__ import annotations`` — it breaks FastAPI/pydantic
forward-ref resolution of the ``Request`` parameter type (same caveat as Optimus's
quorum_assess_route), turning the auth gate into a 422 body-validation error.
"""

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.gateway.agora_contribute")

COLLECTOR_AGENT = "anita"  # the Agora moderator signs the contribute request
SOURCE_KIND = "samus.commercial"
ENV_CONTRIBUTE_ENABLED = "SN_AGORA_CONTRIBUTE_ENABLED"

_SHARED_CLIENT_PATH_ENSURED = False


def _ensure_security_client_on_path() -> None:
    """Add ``_shared/`` to sys.path so ``security_client`` imports (idempotent)."""
    global _SHARED_CLIENT_PATH_ENSURED
    if _SHARED_CLIENT_PATH_ENSURED:
        return
    for up in range(2, 9):
        try:
            cand = Path(__file__).resolve().parents[up] / "_shared"
        except IndexError:
            break
        if (cand / "security_client" / "agent_envelope.py").exists():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            _SHARED_CLIENT_PATH_ENSURED = True
            return


def is_contribute_enabled() -> bool:
    """Read ``SN_AGORA_CONTRIBUTE_ENABLED`` live. Default OFF (dormant)."""
    return os.environ.get(ENV_CONTRIBUTE_ENABLED, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
        "y",
    )


class _VerificationUnavailable(Exception):
    """Verifier cannot run (no key / security_client missing) → 503."""


class _EnvelopeRejected(Exception):
    """Envelope absent/malformed/forged/wrong-sender → 401/403."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _verify_anita_envelope(body: Any) -> dict[str, Any]:
    """Verify an inbound wire ``AgentEnvelope`` from the Agora collector (Anita).

    Returns the inner payload dict on success. Fail-closed (mirrors
    quorum_vote_route._verify_major_envelope but for COLLECTOR_AGENT="anita").
    """
    if not isinstance(body, dict):
        raise _EnvelopeRejected("missing envelope", status_code=401)

    _ensure_security_client_on_path()
    try:
        from security_client.agent_envelope import (  # type: ignore
            AgentEnvelope,
            EnvelopeError,
            EnvelopeReplay,
            EnvelopeSignatureInvalid,
            EnvelopeStale,
        )
        from security_client.rotating_hmac import (  # type: ignore
            RotatingHMACKey,
            RotatingHMACKeyError,
        )
    except Exception as exc:  # noqa: BLE001 — import failure ⇒ cannot verify ⇒ 503
        _LOG.error("agora_contribute: security_client unavailable: %s", exc)
        raise _VerificationUnavailable("security_client_unavailable") from exc

    try:
        verifying_key = RotatingHMACKey.for_agent(COLLECTOR_AGENT)  # SS_HMAC_KEY_ANITA
    except RotatingHMACKeyError as exc:
        _LOG.error("agora_contribute: collector '%s' key unprovisioned: %s", COLLECTOR_AGENT, exc)
        raise _VerificationUnavailable("collector_key_unprovisioned") from exc

    try:
        env = AgentEnvelope.from_wire(body)
    except EnvelopeError as exc:
        raise _EnvelopeRejected(f"malformed_envelope: {exc}", status_code=403) from exc

    if env.from_agent != COLLECTOR_AGENT:
        raise _EnvelopeRejected(
            f"envelope from_agent='{env.from_agent}', expected '{COLLECTOR_AGENT}'",
            status_code=403,
        )

    try:
        env.verify(verifying_key)
    except (EnvelopeSignatureInvalid, EnvelopeStale, EnvelopeReplay) as exc:
        raise _EnvelopeRejected(
            f"envelope_verification_failed: {type(exc).__name__}", status_code=403
        ) from exc
    except EnvelopeError as exc:
        raise _EnvelopeRejected(f"envelope_verification_failed: {exc}", status_code=403) from exc

    payload = env.payload
    if not isinstance(payload, dict):
        raise _EnvelopeRejected("envelope_payload_not_object", status_code=403)
    return dict(payload)


def _gather_commercial_evidence() -> dict[str, Any]:
    """Samus's unique, source-tagged commercial/resource evidence (real facts)."""
    return {
        "have_evidence": True,
        "role": "revenue + fulfillment engine",
        "local_gpu_contender": False,  # Anthropic-only path; NOT the local 4090
        "inference_path": "anthropic-only",
        "llm_daily_cap_usd": 1.0,
        "outbound_guardrails": [
            "stake_sentence",
            "can_spam",
            "evidence_source",
            "reward_density",
            "legitimacy_filter",
            "apollo_budget",
        ],
        "evidence_namespace": SOURCE_KIND,
    }


def _evidence_hash(evidence_payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        evidence_payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _deterministic_stance(topic: str, evidence: dict[str, Any]) -> str:
    if not evidence.get("have_evidence"):
        return "samus: contribution disabled; no stance."
    return (
        f"samus commercial lens on '{topic[:120]}': samus runs an Anthropic-only "
        "inference path and does NOT contend for the local 4090, so GPU serialization "
        "among the local agents does not affect samus's workload directly. From a "
        "revenue-continuity stance, whatever protocol is chosen must not stall the "
        "shared local inference the downstream local agents (and the sales pipeline "
        "that depends on them) rely on."
    )


def _build_contribution(payload: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    topic = str(payload.get("topic", ""))
    return {
        "agent": "samus",
        "source_kind": SOURCE_KIND,
        "evidence_hash": _evidence_hash(evidence),
        "evidence_payload": evidence,
        "stance": _deterministic_stance(topic, evidence),
        "deliberation_id": str(payload.get("deliberation_id", "")),
        "round": payload.get("round", 0),
        "ts": time.time(),
    }


def register(app) -> None:
    """Mount ``POST /agora/contribute`` on ``app`` (mirrors quorum_vote_route.register)."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.post("/agora/contribute")
    async def agora_contribute(request: Request):  # noqa: ANN202
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"detail": "invalid_json"})

        # Verify FIRST (auth gate) — even a disabled endpoint must not accept an
        # unauthenticated caller; only after a verified Anita envelope do we
        # decide enabled-vs-disabled (which is an honest inadmissible payload).
        try:
            payload = _verify_anita_envelope(body)
        except _EnvelopeRejected as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
        except _VerificationUnavailable as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})

        if not is_contribute_enabled():
            evidence = {"have_evidence": False, "reason": "contribute_disabled"}
            return _build_contribution(payload, evidence)

        evidence = _gather_commercial_evidence()
        result = _build_contribution(payload, evidence)
        _LOG.info(
            "agora_contribute from=anita topic=%s evidence_hash=%s",
            str(payload.get("topic", ""))[:60],
            result["evidence_hash"][:12],
        )
        return result


__all__ = [
    "COLLECTOR_AGENT",
    "SOURCE_KIND",
    "is_contribute_enabled",
    "register",
    "_verify_anita_envelope",
    "_gather_commercial_evidence",
]
