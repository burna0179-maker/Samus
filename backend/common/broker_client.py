"""Resource-broker client — fail-closed reservation gate for scarce resources.

This module is the SAMUS-SIDE half of the meta-governance L1 resource
broker. The SERVER side lives on Anita (`feat/anita-l1-broker`) at the
HTTP routes ``POST /broker/reserve`` and ``POST /broker/release``.

Interop shape
-------------

The broker server requires an inbound :class:`AgentEnvelope` HMAC for
``reserve`` + ``release`` (see ``Anita/backend/packs/resource_broker/
routes.py:verify_peer_hmac_or_401``). This is the inter-agent envelope
scheme — NOT Samus's intra-service ``signed_post_json_sync`` HMAC. The
envelope:

  * is signed with :class:`RotatingHMACKey` resolved as
    ``RotatingHMACKey.for_agent("samus")`` (reads ``SS_HMAC_KEY_SAMUS``
    or ``SAMUS_AGENT_HMAC_SECRET`` as a sender-side fallback);
  * carries ``from_agent="samus"`` + ``to_agent="anita"`` + the request
    payload as ``payload``;
  * requires this process to have called ``init_thumbprint("samus")``.
    We lazy-init the thumbprint inside :func:`_get_signing_key` so
    Samus boot is unaffected.

Failure model (FAIL-CLOSED — invariant from the protocol design)
---------------------------------------------------------------

Every error path on :func:`reserve` raises :class:`BrokerDenied`:

  * HTTP non-200 (notably 409 deny) → BrokerDenied with the server reason
  * network/connection refused/timeout → BrokerDenied(reason="broker_unreachable")
  * malformed/missing response shape → BrokerDenied(reason="server_error")
  * HMAC envelope construction failure → BrokerDenied(reason="broker_unreachable")
  * missing inter-agent key when ``samus_broker_disable_in_dev_when_no_key``
    is True → reserve returns a dummy reservation + release is a no-op (so
    pytest stays green out of the box)

:func:`release` is intentionally lenient: any failure is logged but
returns normally, because the broker auto-reclaims at the reservation's
TTL. We never block the caller on a failed release.

Wiring
------

Wired into ``backend/common/llm_client.py:anthropic_messages`` at the
two insertion points specified by the recon report:

  * after the budget gate clears, BEFORE the HTTP call — call ``reserve()``
    with ``cost=global_decision.estimated_usd`` so the broker sees the
    same USD figure Samus's own gate saw;
  * after every recorded spend AND every exception path — call
    ``release()`` with the real ``actual_cost`` (from the response usage
    block) or ``actual_cost=0.0`` on error.

The integration translates :class:`BrokerDenied` into the existing
:class:`BudgetExceeded` exception at the boundary so callers handle one
exception type. The broker reason is wrapped into the QuotaDecision so
log lines still surface it.
"""
# canon-carve-out: outbound HTTP boundary — HMAC AgentEnvelope signed
# before every reserve/release; fail-closed on any failure.
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from .net_limits import ResponseTooLarge as _ResponseTooLarge


_LOG = logging.getLogger("samus.common.broker_client")


# ---------------------------------------------------------------------------
# Frozen public surface — must match RECON.md §6 verbatim.
# ---------------------------------------------------------------------------

class BrokerDenied(Exception):
    """Raised when the broker denies a reservation; FAIL-CLOSED.

    Carries a structured reason for caller logging and an optional
    ``retry_after_sec`` hint surfaced by the broker (e.g. when the
    ecosystem_health scalar is gating spend).
    """

    def __init__(
        self,
        *,
        kind: str,
        reason: str,
        retry_after_sec: float | None = None,
    ) -> None:
        super().__init__(f"broker_denied:{kind}:{reason}")
        self.kind = kind
        self.reason = reason
        self.retry_after_sec = retry_after_sec


@dataclass(frozen=True)
class Reservation:
    """Opaque grant handle returned by the broker on success."""

    id: str
    kind: str
    granted_cost: float
    expires_at: float
    priority: int


# ---------------------------------------------------------------------------
# Priority mapping — workcell class → broker priority (lower = higher priority).
# Tunable via SAMUS_BROKER_PRIORITY_JSON for ops without a code change.
# ---------------------------------------------------------------------------

_BROKER_PRIORITY_BY_WORKCELL_CLASS: dict[str, int] = {
    "prospecting": 5,
    "outreach": 5,
    "fulfillment": 3,
    "voice": 4,
}
_DEFAULT_BROKER_PRIORITY: int = 5


def workcell_priority_for(workcell: str) -> int:
    """Map a workcell label to a broker priority integer.

    Resolution order:
      1. Per-workcell exact match in settings JSON (if set);
      2. Built-in workcell-class match in :data:`_BROKER_PRIORITY_BY_WORKCELL_CLASS`;
      3. Default priority (5).

    Lower numbers mean HIGHER priority on the broker side (0 = critical
    safety, 10 = opportunistic). Mirrors the broker server's priority
    semantics (RECON.md §6).
    """
    if not workcell:
        return _DEFAULT_BROKER_PRIORITY
    cleaned = workcell.strip().lower()
    # Settings-driven override first (operator can re-tune without restart).
    try:
        from .config import get_settings

        raw = (get_settings().samus_broker_priority_json or "").strip()
        if raw:
            import json as _json

            overrides = _json.loads(raw)
            if isinstance(overrides, dict):
                if cleaned in overrides:
                    return int(overrides[cleaned])
                # also accept workcell-class match in the override map
                for key, val in overrides.items():
                    if cleaned.startswith(str(key).lower()):
                        return int(val)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("broker_client priority override parse skipped: %s", exc)
    # Built-in class-prefix match (so e.g. "outreach_email" maps to outreach).
    for cls, prio in _BROKER_PRIORITY_BY_WORKCELL_CLASS.items():
        if cleaned == cls or cleaned.startswith(cls + "_") or cleaned.startswith(cls + "-"):
            return int(prio)
    return _DEFAULT_BROKER_PRIORITY


# ---------------------------------------------------------------------------
# Internal helpers — signing-key + envelope construction.
# ---------------------------------------------------------------------------

_SHARED_CLIENT_PATH_ENSURED = False


def _ensure_security_client_on_path() -> None:
    """Add ``_shared/`` to ``sys.path`` so we can import ``security_client``.

    Mirrors the broker server's ``_ensure_security_client_on_path`` so the
    same import works from any depth under the worktree.
    """
    global _SHARED_CLIENT_PATH_ENSURED
    if _SHARED_CLIENT_PATH_ENSURED:
        return
    for up in range(2, 8):
        try:
            cand = Path(__file__).resolve().parents[up] / "_shared"
        except IndexError:
            break
        if (cand / "security_client" / "agent_envelope.py").exists():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            _SHARED_CLIENT_PATH_ENSURED = True
            return


def _inter_agent_key_configured() -> bool:
    """Return True iff an inter-agent (samus) HMAC env var is set AND decodes.

    The broker requires ``RotatingHMACKey.for_agent("samus")`` which reads
    ``SS_HMAC_KEY_SAMUS`` first then ``SAMUS_AGENT_HMAC_SECRET`` and decodes
    the value as hex. If neither is set (the common dev / pytest case) OR
    the value is present but not valid hex (placeholder strings, base64
    leftovers in a dev ``.env``), we can't even attempt to sign — short-
    circuit to the dev-disable path. Validating decodability here (not
    just presence) prevents every existing anthropic_messages test from
    fail-closing when the dev .env carries a non-hex placeholder.
    """
    raw = os.environ.get("SS_HMAC_KEY_SAMUS") or os.environ.get("SAMUS_AGENT_HMAC_SECRET")
    if not raw:
        return False
    try:
        bytes.fromhex(raw.strip().removeprefix("0x").removeprefix("0X"))
    except ValueError:
        return False
    return True


def _get_signing_key() -> Any:
    """Resolve the inter-agent rotating HMAC key for Samus.

    Lazy-imports the shared security_client + lazy-inits the per-process
    thumbprint on first use so Samus boot is not coupled to the broker
    feature. Raises a plain exception on failure; callers translate to
    :class:`BrokerDenied`.
    """
    _ensure_security_client_on_path()
    from security_client.rotating_hmac import RotatingHMACKey
    from security_client.thumbprint import (
        ThumbprintNotInitialised,
        get_my_thumbprint,
        init_thumbprint,
    )

    try:
        get_my_thumbprint()
    except ThumbprintNotInitialised:
        # Lazy-init bound to the literal 'samus' agent_id so the envelope's
        # from_agent/thumbprint check inside AgentEnvelope.create() passes.
        init_thumbprint("samus")
    return RotatingHMACKey.for_agent("samus")


def _build_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Construct a wire-form AgentEnvelope from Samus → Anita."""
    _ensure_security_client_on_path()
    from security_client.agent_envelope import AgentEnvelope

    signing_key = _get_signing_key()
    env = AgentEnvelope.create(
        from_agent="samus",
        to_agent="anita",
        payload=payload,
        signing_key=signing_key,
    )
    return env.to_wire()


# ---------------------------------------------------------------------------
# Settings access — keep import-time side effects minimal.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _BrokerConfig:
    enabled: bool
    base_url: str
    reserve_timeout_sec: float
    release_timeout_sec: float
    disable_in_dev_when_no_key: bool


def _load_config() -> _BrokerConfig:
    """Read broker settings at call time (so tests can rotate flags)."""
    try:
        from .config import get_settings

        s = get_settings()
        cfg = _BrokerConfig(
            enabled=bool(getattr(s, "samus_broker_enabled", True)),
            base_url=str(
                getattr(s, "samus_broker_base_url", "https://127.0.0.1:8420")
            ).rstrip("/"),
            reserve_timeout_sec=float(
                getattr(s, "samus_broker_reserve_timeout_sec", 1.5)
            ),
            release_timeout_sec=float(
                getattr(s, "samus_broker_release_timeout_sec", 1.5)
            ),
            disable_in_dev_when_no_key=bool(
                getattr(s, "samus_broker_disable_in_dev_when_no_key", True)
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "broker_client settings load failed; treating broker as disabled: %s",
            exc,
        )
        return _BrokerConfig(
            enabled=False,
            base_url="https://127.0.0.1:8420",
            reserve_timeout_sec=1.5,
            release_timeout_sec=1.5,
            disable_in_dev_when_no_key=True,
        )
    # Dev-disable: no inter-agent key configured + flag says "keep pytest
    # green out of the box" → treat the broker as disabled. This is the
    # only path that silently grants without contacting the broker; it
    # exists exclusively for the no-secrets dev/test posture.
    if cfg.enabled and cfg.disable_in_dev_when_no_key and not _inter_agent_key_configured():
        _LOG.warning(
            "broker_client: inter-agent HMAC key not configured "
            "(SS_HMAC_KEY_SAMUS / SAMUS_AGENT_HMAC_SECRET); "
            "treating broker as disabled (samus_broker_disable_in_dev_when_no_key=True)."
        )
        return _BrokerConfig(
            enabled=False,
            base_url=cfg.base_url,
            reserve_timeout_sec=cfg.reserve_timeout_sec,
            release_timeout_sec=cfg.release_timeout_sec,
            disable_in_dev_when_no_key=True,
        )
    return cfg


# ---------------------------------------------------------------------------
# Dummy reservation marker — used only when the broker is disabled.
# ---------------------------------------------------------------------------

_DISABLED_RESERVATION_ID = "broker-disabled-dummy"


def _is_disabled_reservation(reservation: Reservation) -> bool:
    return reservation.id == _DISABLED_RESERVATION_ID


# ---------------------------------------------------------------------------
# HTTP plumbing — keep small + sync. No retries (broker is local + bounded).
# ---------------------------------------------------------------------------

def _post_signed(
    url: str,
    envelope_wire: dict[str, Any],
    *,
    timeout_sec: float,
) -> httpx.Response:
    """POST the envelope JSON to ``url`` with sync httpx + tight timeout.

    No retries — reserve is on the LLM-call hot path; the broker is
    expected to be local. A slow broker means a slow LLM call. Retries
    would multiply the latency.
    """
    timeout = httpx.Timeout(
        connect=min(0.5, timeout_sec),
        read=timeout_sec,
        write=timeout_sec,
        pool=timeout_sec,
    )
    # verify=False ONLY for self-signed loopback — outbound to any other
    # host MUST verify. This mirrors the operator_console TLS posture (HTTPS
    # but self-signed on loopback for local dev). host.docker.internal is
    # the same machine seen from inside a container (Docker Desktop alias
    # for the host gateway), so it gets the same loopback exemption.
    verify = not (
        url.startswith("https://127.0.0.1")
        or url.startswith("https://localhost")
        or url.startswith("https://host.docker.internal")
    )
    with httpx.Client(timeout=timeout, verify=verify) as client:
        resp = client.post(
            url,
            json=envelope_wire,
            headers={"Content-Type": "application/json"},
        )
        # S3: bound the broker envelope before any caller decodes it. Broker
        # replies are tiny reservation envelopes; an over-cap body is a
        # malformed/hostile response and the fail-closed callers treat it as a
        # denial. Enforce here so every read site (resp.json/resp.text) is
        # bounded.
        from .net_limits import BROKER_MAX_BYTES, check_httpx_size
        check_httpx_size(resp, max_bytes=BROKER_MAX_BYTES, source="broker")
        return resp


# ---------------------------------------------------------------------------
# Public API — reserve / release.
# ---------------------------------------------------------------------------

def reserve(
    *,
    kind: str,
    cost: float,
    priority: int,
    workcell: str,
    timeout_sec: float = 1.0,
) -> Reservation:
    """Reserve a scarce-resource slice from the broker.

    Returns a :class:`Reservation` on grant. Raises :class:`BrokerDenied`
    on any failure (network, deny, malformed, key missing, etc.) — there
    is no third "maybe" outcome.

    When ``samus_broker_enabled=False`` (or dev-disable kicks in)
    returns a sentinel reservation with id ``broker-disabled-dummy`` that
    :func:`release` will silently no-op on, so the disabled state is
    fully transparent to callers.
    """
    cfg = _load_config()
    if not cfg.enabled:
        # Dev-disable: silent grant, sentinel reservation. The matching
        # release() recognises the sentinel and skips the HTTP call.
        return Reservation(
            id=_DISABLED_RESERVATION_ID,
            kind=str(kind),
            granted_cost=float(cost),
            expires_at=time.time() + 300.0,
            priority=int(priority),
        )

    # Bound the effective timeout to what the caller asked for AND what
    # settings allow — whichever is SMALLER. We do not extend latency
    # past the operator-configured cap.
    effective_timeout = min(float(timeout_sec), cfg.reserve_timeout_sec)
    if effective_timeout <= 0:
        effective_timeout = cfg.reserve_timeout_sec

    payload = {
        "kind": str(kind),
        "cost": float(cost),
        "priority": int(priority),
        "workcell": str(workcell),
        "requested_at": time.time(),
    }

    # Build the signed envelope. Signing failures are FAIL-CLOSED.
    try:
        envelope_wire = _build_envelope(payload)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "broker_client reserve: envelope build failed (kind=%s workcell=%s): %s",
            kind, workcell, exc,
        )
        raise BrokerDenied(
            kind=str(kind),
            reason="broker_unreachable",
        ) from exc

    url = cfg.base_url + "/broker/reserve"
    try:
        resp = _post_signed(url, envelope_wire, timeout_sec=effective_timeout)
    except httpx.HTTPError as exc:
        _LOG.warning(
            "broker_client reserve: transport error (kind=%s workcell=%s): %s",
            kind, workcell, exc,
        )
        raise BrokerDenied(
            kind=str(kind),
            reason="broker_unreachable",
        ) from exc
    except _ResponseTooLarge as exc:
        # S3: an over-cap broker reply is malformed/hostile — fail closed.
        _LOG.warning(
            "broker_client reserve: response over cap (kind=%s workcell=%s): %s",
            kind, workcell, exc,
        )
        raise BrokerDenied(kind=str(kind), reason="server_error") from exc

    # 200 = grant. 409 = explicit deny. Anything else = fail-closed.
    if resp.status_code == 200:
        try:
            body = resp.json()
        except (ValueError, Exception) as exc:  # noqa: BLE001
            _LOG.warning(
                "broker_client reserve: 200 with non-JSON body (kind=%s): %s",
                kind, exc,
            )
            raise BrokerDenied(kind=str(kind), reason="server_error") from exc
        if not isinstance(body, dict):
            raise BrokerDenied(kind=str(kind), reason="server_error")
        try:
            return Reservation(
                id=str(body["reservation_id"]),
                kind=str(body.get("kind", kind)),
                granted_cost=float(body["granted_cost"]),
                expires_at=float(body["expires_at"]),
                priority=int(body.get("priority", priority)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            _LOG.warning(
                "broker_client reserve: 200 with malformed grant (kind=%s): %s",
                kind, exc,
            )
            raise BrokerDenied(kind=str(kind), reason="server_error") from exc

    if resp.status_code == 409:
        reason = "broker_denied"
        retry_after: float | None = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail", body)
                if isinstance(detail, dict):
                    reason = str(detail.get("reason", reason))
                    ra = detail.get("retry_after_sec")
                    if ra is not None:
                        retry_after = float(ra)
        except Exception:  # noqa: BLE001
            pass
        raise BrokerDenied(
            kind=str(kind),
            reason=reason,
            retry_after_sec=retry_after,
        )

    # Any other status (401, 403, 5xx, etc.) — treat as deny + log.
    snippet = (resp.text or "")[:200]
    _LOG.warning(
        "broker_client reserve: unexpected status %s (kind=%s workcell=%s): %s",
        resp.status_code, kind, workcell, snippet,
    )
    raise BrokerDenied(kind=str(kind), reason="server_error")


def release(
    reservation: Reservation,
    *,
    actual_cost: float,
    outcome: Literal["ok", "error", "abandoned"],
) -> None:
    """Release a reservation. Idempotent + best-effort.

    Failure to release is logged + swallowed — the broker auto-reclaims
    at the reservation's ``expires_at`` so a transient release-time
    network blip does not block the caller or leak a phantom hold.
    """
    if _is_disabled_reservation(reservation):
        # Dev-disable path: there's no server-side state to free.
        return

    cfg = _load_config()
    if not cfg.enabled:
        # Same dev-disable path but reservation predates the flag flip.
        return

    payload = {
        "reservation_id": str(reservation.id),
        "actual_cost": float(actual_cost),
        "outcome": str(outcome),
    }

    try:
        envelope_wire = _build_envelope(payload)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "broker_client release: envelope build failed (id=%s): %s",
            reservation.id, exc,
        )
        return

    url = cfg.base_url + "/broker/release"
    try:
        resp = _post_signed(
            url, envelope_wire, timeout_sec=cfg.release_timeout_sec,
        )
    except (httpx.HTTPError, _ResponseTooLarge) as exc:
        # release() is intentionally lenient — any failure (transport or an
        # over-cap body, S3) is logged and swallowed; the broker auto-reclaims
        # at the reservation TTL. Never block the caller on a failed release.
        _LOG.warning(
            "broker_client release: transport error (id=%s outcome=%s): %s",
            reservation.id, outcome, exc,
        )
        return

    if resp.status_code != 200:
        snippet = (resp.text or "")[:200]
        _LOG.warning(
            "broker_client release: non-200 status %s (id=%s outcome=%s): %s",
            resp.status_code, reservation.id, outcome, snippet,
        )


__all__ = [
    "BrokerDenied",
    "Reservation",
    "reserve",
    "release",
    "workcell_priority_for",
]
