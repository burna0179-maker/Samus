"""Per-target base-URL resolution for HTTP fallback dispatch.

The gateway's queue path bypasses this; the HTTP fallback (used when no SQS
queue is configured for ``target``) needs a base URL to POST ``/work`` to.
``resolve_target`` reads the ``gateway_urls`` map from settings.
"""

from __future__ import annotations

from backend.common.settings import get_settings


def resolve_target(target: str) -> str:
    """Return the base URL configured for ``target``.

    Raises ``KeyError`` if the target is not in ``settings.gateway_urls``.
    """
    urls = get_settings().gateway_urls
    if target not in urls:
        raise KeyError(target)
    return urls[target]
