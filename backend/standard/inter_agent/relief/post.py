"""Sign + POST one relief request to Anita's /api/relief/intake. Fail-closed.

The ``post_fn`` the :class:`ReliefForwarder` calls. Reuses
``broker_client._build_envelope`` (Samus → Anita HMAC AgentEnvelope, signed via
``RotatingHMACKey.for_agent("samus")`` == ``SS_HMAC_KEY_SAMUS``) so the relief
egress shares Samus's single inter-agent signing path. The only addition over
the broker's ``_post_signed`` is the ``X-Request-Timestamp`` / ``X-Nonce``
replay headers — the relief intake is mounted on Anita's MAIN gateway (which
runs ReplayProtectionMiddleware), unlike the broker server.

Every failure (no key / unreachable / non-2xx / declined) returns ``False`` —
a failure is never a success, the item is retried next run. This NEVER applies
or resolves anything: it asks Anita to enqueue the item for deliberation.
"""

# canon-carve-out: outbound HTTP boundary — HMAC AgentEnvelope signed Samus->Anita; resolves nothing.
from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

_LOG = logging.getLogger("samus.inter_agent.relief.post")

_INTAKE_PATH = "/api/relief/intake"


def post_relief_to_anita(
    request: Dict[str, Any],
    *,
    anita_url: str,
    timeout_sec: float = 5.0,
) -> bool:
    """Return True iff Anita accepted the relief request. Fail-closed."""
    if not anita_url:
        _LOG.debug("relief post: no anita_url configured")
        return False

    try:
        from backend.common.broker_client import _build_envelope

        wire = _build_envelope(dict(request))
    except Exception as exc:  # noqa: BLE001 — no key / sign failure ⇒ cannot send
        _LOG.warning("relief post: envelope build failed (%r) — fail-closed", exc)
        return False

    url = anita_url.rstrip("/") + _INTAKE_PATH
    # verify=False ONLY for self-signed loopback, mirroring broker_client.
    verify = not (url.startswith("https://127.0.0.1") or url.startswith("https://localhost"))
    timeout = httpx.Timeout(
        connect=min(0.5, timeout_sec), read=timeout_sec, write=timeout_sec, pool=timeout_sec
    )
    try:
        with httpx.Client(timeout=timeout, verify=verify) as client:
            resp = client.post(
                url,
                json=wire,
                headers={
                    "Content-Type": "application/json",
                    "X-Request-Timestamp": str(wire["ts"]),
                    "X-Nonce": str(wire["nonce"]),
                },
            )
    except Exception as exc:  # noqa: BLE001 — unreachable Anita ⇒ fail-closed
        _LOG.info("relief post: anita unreachable (%r) — fail-closed", exc)
        return False

    if resp.status_code != 200:
        _LOG.debug("relief post: anita http %s", resp.status_code)
        return False
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(body, dict) and body.get("accepted"))


__all__ = ["post_relief_to_anita"]
