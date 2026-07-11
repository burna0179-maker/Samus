"""HMAC signing primitives for inter-service auth.

Per doc §3.12. Every signed request includes timestamp, nonce, and signature
headers; the receiving middleware re-derives the signature and rejects
mismatches.

Keying model (R-1 remediation, 2026-05-20)
-------------------------------------------
Historically every workcell signed and verified with one shared
``SAMUS_SHARED_HMAC_KEY``. That is a flat trust model: one compromised
workcell can forge any other's traffic, and the receiver has no way to learn
*who* called.

The remediation is **additive and non-breaking**:

- Each workcell MAY have its own key, env ``SAMUS_HMAC_KEY_<SERVICE>``
  (service name uppercased — e.g. ``SAMUS_HMAC_KEY_CRM``). The *sender* signs
  with ITS own key.
- If a service has no per-service key configured, it falls back to
  ``SAMUS_SHARED_HMAC_KEY``. The live stack today has only the shared key, so
  with this fallback nothing changes until the operator chooses to seed
  per-service keys.
- The signed request carries an ``X-Samus-Caller`` header naming the calling
  service. That caller name is folded INTO the HMAC-signed canonical string
  (see :func:`sign_request`'s ``caller`` arg) so it cannot be forged: an
  attacker who tampers with ``X-Samus-Caller`` invalidates the signature.
- The receiver reads ``X-Samus-Caller``, looks up that caller's key, and
  verifies. A valid signature therefore *proves* the caller identity, which
  the authorization layer (``capabilities.authorize``) then evaluates.

``resolve_service_key`` is the single chokepoint for "given a service name,
what key signs/verifies its traffic" — used by both the signer
(``http_client``) and the verifier (``middleware``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time


def sign_request(
    secret: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    caller: str | None = None,
    query_string: str = "",
) -> str:
    """Return the HMAC-SHA256 signature as a 64-char lowercase hex string.

    Canonical string (no ``caller``, no query):
        METHOD\\npath\\ntimestamp\\nnonce\\nsha256(body)

    Canonical string (``caller`` provided — R-1 caller-identity binding):
        METHOD\\npath\\ntimestamp\\nnonce\\nsha256(body)\\ncaller

    Canonical string (``query_string`` provided — query-tampering protection):
        METHOD\\npath?query\\ntimestamp\\nnonce\\nsha256(body)[\\ncaller]

    ``query_string`` is appended to path when non-empty, preventing an
    attacker from swapping ``?redirect=evil`` after the signature is computed.
    Both args are optional and default-safe so existing call sites produce
    the byte-identical legacy canonical string.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    signed_path = f"{path}?{query_string}" if query_string else path
    parts = [method.upper(), signed_path, timestamp, nonce, body_hash]
    if caller is not None:
        parts.append(caller)
    canonical = "\n".join(parts).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def generate_nonce() -> str:
    """32-character hex nonce."""
    return secrets.token_hex(16)


def generate_timestamp() -> str:
    """Epoch seconds as decimal string."""
    return str(int(time.time()))


def safe_compare(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    return hmac.compare_digest(a, b)


def per_service_key_env_name(service: str) -> str:
    """Env-var name for a workcell's dedicated HMAC key.

    ``crm`` -> ``SAMUS_HMAC_KEY_CRM``. The service name is uppercased and any
    non-alphanumeric character is replaced with ``_`` so multi-word workcell
    names (``signal_filter`` -> ``SAMUS_HMAC_KEY_SIGNAL_FILTER``) resolve
    cleanly.
    """
    safe = "".join(c if c.isalnum() else "_" for c in (service or "").strip())
    return f"SAMUS_HMAC_KEY_{safe.upper()}"


def resolve_service_key(service: str, shared_key: str) -> str:
    """Resolve the HMAC key that signs/verifies ``service``'s traffic.

    Resolution order (R-1 — additive, non-breaking):

    1. The per-service env var ``SAMUS_HMAC_KEY_<SERVICE>`` if set & non-empty.
    2. Otherwise the shared key (``SAMUS_SHARED_HMAC_KEY``, passed in as
       ``shared_key`` so the caller can hand us the already-resolved settings
       value rather than re-reading the env).

    Returns ``""`` only when BOTH are unset — the caller decides what an empty
    key means (the signer raises, the verifier 503s), exactly as the
    pre-R-1 behaviour did with the shared key alone.

    The env var is read live (``os.environ``) rather than off the cached
    ``Settings`` object so an operator can seed a per-service key and have a
    workcell pick it up on the next ``reload_settings()`` / process restart
    without a settings-schema change being mandatory. ``settings`` also
    surfaces the resolved map via ``per_service_hmac_keys`` for /show-config
    visibility; this function is the authoritative resolver.
    """
    env_key = os.environ.get(per_service_key_env_name(service), "")
    if env_key and env_key.strip():
        return env_key.strip()
    return shared_key or ""
