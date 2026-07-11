"""``POST /quorum/vote`` — Samus's VOTER endpoint for the cross-agent quorum.

Canonical spec: ``.consolidation/QUORUM_VOTE_PROTOCOL.md`` (2026-06-01 rework).

The collector (Major) broadcasts a peer's CROSS_AGENT proposal to every
roster voter's ``POST /quorum/vote``; the voter runs the 3-axis reasoning
(:mod:`backend.standard.inter_agent.quorum_voter`) and returns a ballot.

This module is the BOUNDARY CONTRACT for that endpoint. It is responsible
for three things, in this order:

1. **Dormant gate** (FIRST — before any work). The route is fully coded but
   inert until the operator sets ``SAMUS_QUORUM_VOTING_ENABLED``. While
   unset the route returns ``503 quorum voting dormant``. This mirrors the
   dormancy posture of ``SAMUS_QUORUM_PUBLISH_ENABLED`` (see
   ``backend/common/quorum_client.py``). Second dormancy layer: Samus is
   intentionally NOT in the operator-signed quorum roster, so Major will not
   even broadcast to it until the operator adds it. We do not touch the
   roster.

2. **HMAC AgentEnvelope verification** (FAIL-CLOSED). The body must be a wire
   ``AgentEnvelope`` FROM ``major`` (collector), signed with Major's rotating
   HMAC key (``SS_HMAC_KEY_MAJOR`` / ``MAJOR_AGENT_HMAC_SECRET``). We do NOT
   rely on ``VerifyHMACMiddleware`` — the test suite disables it process-wide
   and, more importantly, the *inter-agent* envelope scheme is distinct from
   the intra-service ``X-Samus-*`` HMAC the middleware enforces. So the route
   verifies the envelope itself, exactly as the broker SERVER side does
   inbound. Outcomes:

     * missing / malformed / non-major envelope, or bad signature → **403**
       (we surface 401 only for a structurally-absent envelope; an envelope
       that decodes but fails verification is 403). Never 200.
     * Major's key unprovisioned OR the shared ``security_client`` is
       unavailable → **503** (cannot verify ⇒ cannot accept ⇒ fail-closed,
       but it's an ops gap, not a forged caller).

   Replay / freshness is delegated to ``AgentEnvelope.verify`` (it enforces a
   ±TTL window + a process-global nonce replay cache); we do not re-implement
   it.

3. **Reasoning + ballot** — only after both gates pass do we hand the inner
   proposal payload to ``decide_ballot`` and return the ballot JSON.

Wiring: ``register(app)`` is called by ``backend/gateway/app.py`` (the
operator-facing workcell that already mounts the inter-agent surface). Wired
but dormant.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request

from .quorum_voter import decide_ballot

_LOG = logging.getLogger("samus.inter_agent.quorum_vote_route")

# Env flag (read at REQUEST time so tests / ops can flip without a rebuild).
ENV_VOTING_ENABLED = "SAMUS_QUORUM_VOTING_ENABLED"

# The collector that is allowed to drive this endpoint.
COLLECTOR_AGENT = "major"
VOTER_AGENT = "samus"

# --- D6-01: sign this voter's vote responses ------------------------------
# Samus Ed25519-signs each ballot with its roster quorum key (SamusQuorumKey)
# so the collector (Major) cannot forge or alter Samus's ballot. Best-effort:
# an unprovisioned key yields an UNSIGNED response. NOTE: Samus is NOT yet in
# the operator-signed roster (admission is the operator U8 step), so the
# collector cannot verify Samus's signature until it is admitted — until then a
# Samus vote is treated as unverifiable (dropped only when require_signed is
# armed). The signing path is wired now so it is live the moment Samus is
# admitted + SAMUS_QUORUM_VOTING_ENABLED is set.
_kp_cache: Dict[str, Any] = {"loaded": False, "kp": None}
_kp_lock = threading.Lock()


def _load_quorum_keypair():
    if _kp_cache["loaded"]:
        return _kp_cache["kp"]
    with _kp_lock:
        if _kp_cache["loaded"]:
            return _kp_cache["kp"]
        kp = None
        try:
            # Root on PYTHONPATH at runtime => _shared.security; the route's
            # path shim only adds the _shared DIR (=> security.* top-level), so
            # fall back to that for shim-only / test environments.
            try:
                from _shared.security.agent_keypair import (
                    DPAPI_SCOPE_SAMUS,
                    load_agent_keypair,
                )
            except ModuleNotFoundError:
                _ensure_security_client_on_path()
                from security.agent_keypair import (  # type: ignore[no-redef]
                    DPAPI_SCOPE_SAMUS,
                    load_agent_keypair,
                )

            kp = load_agent_keypair(VOTER_AGENT, scope=DPAPI_SCOPE_SAMUS)
        except Exception as exc:  # noqa: BLE001 — best-effort; unsigned acceptable pre-admission
            _LOG.warning(
                "quorum_vote: %s quorum key unavailable, votes UNSIGNED: %s",
                VOTER_AGENT,
                exc,
            )
            kp = None
        _kp_cache["kp"] = kp
        _kp_cache["loaded"] = True
        return kp


def _sign_vote_response(verdict: Dict[str, Any], proposal_id: str) -> Dict[str, Any]:
    """Attach a signed vote envelope to ``verdict`` (best-effort; never raises)."""
    kp = _load_quorum_keypair()
    if kp is None:
        return verdict
    try:
        try:
            from _shared.security.quorum_vote_envelope import attach_vote_signature
        except ModuleNotFoundError:
            _ensure_security_client_on_path()
            from security.quorum_vote_envelope import (  # type: ignore[no-redef]
                attach_vote_signature,
            )

        return attach_vote_signature(
            verdict,
            private_key=kp.private_key,
            voter=VOTER_AGENT,
            proposal_id=proposal_id,
        )
    except Exception as exc:  # noqa: BLE001 — unsigned fallback
        _LOG.warning("quorum_vote: sign failed, returning unsigned: %s", exc)
        return verdict


# Bound the request body we will buffer + JSON-decode. A quorum envelope is a
# small JSON object; anything larger is malformed / hostile. The body-size
# middleware also caps this, but the route is its own boundary so we re-cap.
_MAX_BODY_BYTES = 256 * 1024


def is_voting_enabled() -> bool:
    """Read ``SAMUS_QUORUM_VOTING_ENABLED`` live. Default OFF (dormant)."""
    return os.environ.get(ENV_VOTING_ENABLED, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
        "y",
    )


# ---------------------------------------------------------------------------
# security_client import shim — mirror broker_client._ensure_security_client_on_path.
# ---------------------------------------------------------------------------

_SHARED_CLIENT_PATH_ENSURED = False


def _ensure_security_client_on_path() -> None:
    """Add ``_shared/`` to ``sys.path`` so ``security_client`` imports.

    Identical strategy to ``broker_client._ensure_security_client_on_path``:
    walk up from this file looking for ``_shared/security_client``. Idempotent.
    """
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


class _VerificationUnavailable(Exception):
    """The verifier cannot run (no key / security_client missing) → 503."""


class _EnvelopeRejected(Exception):
    """The envelope is absent/malformed/forged/not-from-major → 401/403."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _verify_major_envelope(body: Any) -> dict[str, Any]:
    """Verify an inbound wire ``AgentEnvelope`` from the collector (Major).

    Returns the inner ``payload`` dict (the proposal) on success.

    FAIL-CLOSED contract:
      * ``body`` not a dict / missing envelope fields → :class:`_EnvelopeRejected`
        (401 if structurally absent, 403 if it decodes but is wrong/forged).
      * ``from_agent`` is not ``major`` → :class:`_EnvelopeRejected` (403).
      * signature / freshness / replay failure → :class:`_EnvelopeRejected` (403).
      * Major's verifying key unprovisioned OR ``security_client`` unavailable
        → :class:`_VerificationUnavailable` (503).
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
        _LOG.error("quorum_vote: security_client unavailable: %s", exc)
        raise _VerificationUnavailable("security_client_unavailable") from exc

    # Resolve Major's verifying key. Absent key material is an OPS gap, not a
    # forged caller → 503 (fail-closed: we still refuse the request).
    try:
        verifying_key = RotatingHMACKey.for_agent(COLLECTOR_AGENT)
    except RotatingHMACKeyError as exc:
        _LOG.error(
            "quorum_vote: no HMAC key configured for collector '%s' "
            "(SS_HMAC_KEY_MAJOR / MAJOR_AGENT_HMAC_SECRET): %s",
            COLLECTOR_AGENT,
            exc,
        )
        raise _VerificationUnavailable("collector_key_unprovisioned") from exc

    # Decode the wire envelope. A malformed structure is a rejected caller.
    try:
        env = AgentEnvelope.from_wire(body)
    except EnvelopeError as exc:
        raise _EnvelopeRejected(f"malformed_envelope: {exc}", status_code=403) from exc

    # The envelope must be addressed FROM the collector. Reject before we even
    # spend an HMAC verification on a non-major sender.
    if env.from_agent != COLLECTOR_AGENT:
        raise _EnvelopeRejected(
            f"envelope from_agent='{env.from_agent}', expected '{COLLECTOR_AGENT}'",
            status_code=403,
        )

    # Cryptographic verification (signature + freshness + replay). Any failure
    # is a rejected caller, never accepted.
    try:
        env.verify(verifying_key)
    except (EnvelopeSignatureInvalid, EnvelopeStale, EnvelopeReplay) as exc:
        raise _EnvelopeRejected(
            f"envelope_verification_failed: {type(exc).__name__}",
            status_code=403,
        ) from exc
    except EnvelopeError as exc:
        # Any other envelope-level error (e.g. version mismatch) → reject.
        raise _EnvelopeRejected(
            f"envelope_verification_failed: {exc}",
            status_code=403,
        ) from exc

    payload = env.payload
    if not isinstance(payload, dict):
        raise _EnvelopeRejected("envelope_payload_not_object", status_code=403)
    return dict(payload)


# ---------------------------------------------------------------------------
# Route registration.
# ---------------------------------------------------------------------------


def register(app: FastAPI) -> None:
    """Mount ``POST /quorum/vote`` on ``app``. Wired but dormant by default.

    Idempotent-ish: FastAPI allows duplicate path registration, so callers
    should register once (the gateway does).
    """

    @app.post("/quorum/vote")
    async def quorum_vote(request: Request) -> dict[str, Any]:
        # 1) DORMANT GATE — first, before any work (read, parse, verify).
        if not is_voting_enabled():
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "quorum_voting_dormant",
                    "message": (
                        "Samus cross-agent quorum voting is dormant. Set "
                        f"{ENV_VOTING_ENABLED}=1 to activate."
                    ),
                },
            )

        # Read the raw body with a hard cap (the route is its own boundary).
        raw = await request.body()
        if len(raw) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request_body_too_large")
        import json  # noqa: PLC0415 — local; keeps import-time surface minimal

        try:
            body = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            # An unparseable body cannot be a valid signed envelope → reject.
            raise HTTPException(status_code=403, detail="malformed_request_body")

        # 2) HMAC ENVELOPE VERIFY — fail-closed.
        try:
            proposal = _verify_major_envelope(body)
        except _VerificationUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail=f"verification_unavailable: {exc}",
            ) from exc
        except _EnvelopeRejected as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=str(exc),
            ) from exc

        # 3) REASON — pure, deterministic. Never raises on a malformed inner
        #    proposal (decide_ballot is fail-closed); but guard anyway so an
        #    unexpected error becomes a 500 via the app's handler, never a
        #    silent accept.
        ballot = decide_ballot(proposal)
        _LOG.info(
            "quorum_vote: cast vote=%s on proposal_id=%s (self=%s eco=%s abuse=%s)",
            ballot.get("vote"),
            ballot.get("proposal_id"),
            ballot.get("self_benefit"),
            ballot.get("ecosystem_benefit"),
            ballot.get("abuse_risk"),
        )
        # D6-01 — sign the ballot so the collector cannot forge/alter it.
        return _sign_vote_response(ballot, str(ballot.get("proposal_id") or ""))

    _LOG.info(
        "quorum_vote: route mounted (dormant=%s) — set %s=1 to activate",
        not is_voting_enabled(),
        ENV_VOTING_ENABLED,
    )


__all__ = [
    "register",
    "is_voting_enabled",
    "ENV_VOTING_ENABLED",
    "COLLECTOR_AGENT",
    "VOTER_AGENT",
]
