"""Per-provider email backend implementations.

Each module exposes a ``send_email_via_<provider>(...)`` function with a shared
return shape (``{message_id, channel, to, ts}``). The selector in
``common/email_backend.py`` dispatches to the right one based on
``settings.email_backend``.

``EmailBackendError`` is the shared base every provider error subclasses, so a
caller can ``except EmailBackendError`` to catch a failure from any backend and
``raise EmailBackendError(...)`` itself, regardless of which provider is wired.
"""


class EmailBackendError(Exception):
    """Base class for any email-backend provider failure."""


__all__ = ["EmailBackendError"]