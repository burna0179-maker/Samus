"""Embedded ngrok tunnel for the voice workcell.

The voice workcell exposes ``POST /vapi/webhook`` for Vapi end-of-call
events. In dev (operator's laptop) and on the VM where Caddy isn't fronting
the workcell, the webhook isn't publicly reachable. Rather than running
``ngrok http`` in a separate terminal and pasting the URL into the Vapi
dashboard, this module opens an ngrok tunnel from inside the workcell
process and returns the public URL.

The FastAPI startup hook calls :func:`start_ngrok_listener` once, then
hands the URL to :func:`patch_vapi_assistant_server_url` so the configured
assistant always points at the live tunnel — no operator paste required.

Degraded modes (each logs a warning + returns ``TunnelResult`` with
``url=None`` and a populated ``error`` instead of raising):

  - ``NGROK_AUTHTOKEN`` unset       -> ``authtoken_unset``
  - ``ngrok`` package import fails  -> ``ngrok_import_failed``
  - ``ngrok.forward()`` raises      -> ``ngrok_forward_failed: <exc>``

The workcell stays healthy in every degraded path; only outbound
end-of-call events from Vapi are affected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass


_LOG = logging.getLogger("samus.voice.tunnel")


@dataclass
class TunnelResult:
    """Outcome of one tunnel-startup attempt. Either ``url`` or ``error`` is set."""

    url: str | None
    error: str | None
    listener: object | None = None  # opaque ngrok listener handle; kept for shutdown


def start_ngrok_listener(
    *,
    port: int,
    authtoken: str,
    reserved_domain: str = "",
) -> TunnelResult:
    """Open an ngrok HTTP tunnel forwarding to ``localhost:port``.

    Returns :class:`TunnelResult`. Never raises — the workcell must stay
    healthy even if the tunnel fails, since the rest of /vapi/webhook is
    still usable via the docker network for tests + sibling workcells.

    ``reserved_domain`` pins the tunnel to a paid-plan reserved domain when
    set (e.g. ``samus-voice.ngrok.app``). When empty, ngrok issues a random
    ``*.ngrok-free.app`` URL on every call.
    """
    if not authtoken:
        _LOG.info(
            "ngrok tunnel skipped: NGROK_AUTHTOKEN not set "
            "(voice workcell stays up; /vapi/webhook is docker-network-only)",
        )
        return TunnelResult(url=None, error="authtoken_unset")

    try:
        import ngrok  # local import so module-load doesn't pull the native lib
    except ImportError as exc:
        _LOG.warning("ngrok import failed: %s", exc)
        return TunnelResult(url=None, error="ngrok_import_failed")

    forward_kwargs: dict = {"authtoken": authtoken}
    if reserved_domain:
        forward_kwargs["domain"] = reserved_domain

    try:
        listener = ngrok.forward(str(port), **forward_kwargs)
    except Exception as exc:  # noqa: BLE001 — ngrok wraps several exc types
        _LOG.warning("ngrok forward failed: %s", exc)
        return TunnelResult(url=None, error=f"ngrok_forward_failed: {exc}")

    # ngrok-python's listener.url() returns the public https URL.
    try:
        url = listener.url()
    except Exception as exc:  # noqa: BLE001 — defensive: SDK shape may shift
        _LOG.warning("ngrok listener.url() failed: %s", exc)
        return TunnelResult(
            url=None,
            error=f"ngrok_url_failed: {exc}",
            listener=listener,
        )

    _LOG.info("ngrok tunnel open: %s -> localhost:%d", url, port)
    return TunnelResult(url=url, error=None, listener=listener)


def patch_vapi_assistant_server_url(
    *,
    assistant_id: str,
    server_url: str,
    server_url_secret: str = "",
    vapi_api_key: str,
    client: object | None = None,
) -> tuple[bool, str | None]:
    """PATCH the Vapi assistant's ``server.url`` (and optionally secret).

    Returns ``(ok, error_or_none)``. Never raises; the voice workcell must
    survive a Vapi outage / bad config without crashing.

    ``client`` is injectable so tests can pass a stub instead of constructing
    a real VapiClient (which would require a working API key).
    """
    if not assistant_id:
        _LOG.info("vapi assistant patch skipped: VAPI_ASSISTANT_ID not set")
        return False, "assistant_id_unset"
    if not vapi_api_key and client is None:
        _LOG.warning(
            "vapi assistant patch skipped: VAPI_API_KEY not set "
            "(cannot reach Vapi REST API even though we have a tunnel URL)",
        )
        return False, "vapi_api_key_unset"

    if client is None:
        from .client import VapiClient

        client = VapiClient(api_key=vapi_api_key)

    try:
        client.update_assistant(  # type: ignore[attr-defined]
            assistant_id=assistant_id,
            server_url=server_url,
            server_url_secret=server_url_secret or None,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "vapi assistant patch failed for %s: %s "
            "(tunnel up but end-of-call webhooks won't reach us)",
            assistant_id,
            exc,
        )
        return False, f"vapi_patch_failed: {exc}"

    _LOG.info("vapi assistant %s server.url patched to %s", assistant_id, server_url)
    return True, None
