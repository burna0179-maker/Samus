"""operator_console pack -- operator-facing UI + chat enrichment (Samus).

Jinja2 SPA shell at ``GET /console`` + ``/api/console/*`` JSON REST.
Built on STANDARD chat enrichment + persona registry. Pattern lifted in
shape (not in source) from Sapphire v2.4.0; Sapphire is AGPL-3.0.

Operator wires the pack into any Samus FastAPI app (or a dedicated
console service) via :func:`backend.packs.operator_console.pod.register`,
or via the top-level :func:`register` delegator below which is the
Canon §4 pack-wire convention the gateway's ``create_app`` uses.

Auth: optional bearer-token gate via ``SAMUS_OPERATOR_TOKEN`` env var.
Off by default (Samus's existing services rely on AWS-side perimeter).
"""
from __future__ import annotations

from typing import Any

from .pod import register as _pod_register

__tier__ = "PACKS"
__pack_id__ = "operator_console"
__pack_version__ = "1.0.0"


def register(app: Any, **kwargs: Any) -> dict[str, Any]:
    """Canon §4 pack-level register hook.

    Called by the host app's ``create_app`` (in Samus today: the gateway
    workcell) once the pack is on the inclusion list. Delegates to
    :func:`.pod.register` with every keyword argument forwarded unchanged
    so the bootstrap (which calls ``module.register(app)`` with no
    kwargs) gets the pod's default-loading behaviour, and custom callers
    can still override any knob.

    Samus has no Canon §4 manifest resolver yet -- per the microservices
    layout each workcell is its own FastAPI app built via
    ``backend.common.app_factory.create_base_app`` -- so the gateway
    calls this delegator directly rather than iterating
    ``plan.packs_in_order`` the way Major's monolithic ``create_app``
    does. Same wire shape, same kwargs contract.
    """
    return _pod_register(app, **kwargs)


__all__ = ["register"]
