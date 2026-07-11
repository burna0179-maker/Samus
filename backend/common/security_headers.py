"""SecurityHeadersMiddleware — S5 browser-hardening headers.

Samus is primarily an inter-service HMAC mesh, but the **gateway** workcell
also mounts the ``operator_console`` pack — a Jinja2 SPA shell served at
``/console`` with ``/api/console/*`` JSON routes that a human operator reaches
from a browser. That browser-facing surface needs the standard hardening
headers (clickjacking, MIME-sniff, referrer, HSTS) or it is exposed to the
usual web attacks.

Adding the headers in ``create_base_app`` applies them to every workcell, not
just the gateway. That is intentional and safe:

  * On a machine-to-machine JSON response (the bulk of the mesh) the headers
    are inert — the HMAC client reads the JSON body and ignores response
    headers entirely, so nothing about the mesh contract changes.
  * On the operator console they are the actual protection.

The headers are RESPONSE-side only; they do not touch request parsing or the
HMAC verification path, so the mesh is unaffected. The set mirrors Darwin's
``security_headers.py`` (itself mirrored from Major / Canon §6). HSTS is safe
to emit unconditionally — clients only honour it on an HTTPS response, and the
console is fronted by TLS.

CSP note: the console SPA serves its own static JS/CSS from ``/console/...``
(same-origin) and an inline bootstrap, so a ``default-src 'none'`` (Darwin's
machine-API value) would break it. The CSP here is ``self``-based with the
inline allowances the SPA shell needs, scoped tight enough to still block
cross-origin script/object injection. The non-console JSON workcells don't
render HTML so the CSP is irrelevant to them.
"""
from __future__ import annotations

from typing import Mapping

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# Canonical hardening header set. Exposed as a module constant so tests can
# import and assert the exact values.
SECURITY_HEADERS: Mapping[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "X-XSS-Protection": "0",
    "Referrer-Policy": "no-referrer",
    # The operator console renders an HTML SPA shell from same-origin assets
    # plus an inline bootstrap; allow 'self' + inline so the console works
    # while still blocking cross-origin script/object injection. Pure-JSON
    # workcells never render HTML so this is inert for them.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # Clients only honour HSTS on an HTTPS response; harmless over plain HTTP.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject the S5 hardening headers on every response.

    Uses ``setdefault`` so a handler that deliberately sets a *more*
    restrictive value (or a route-specific CSP) is preserved; the canonical
    set fills in anything the handler did not specify.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # noqa: D401
        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


__all__ = ["SecurityHeadersMiddleware", "SECURITY_HEADERS"]
