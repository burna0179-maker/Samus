"""AWS SNS X.509 message-signature verification for the feedback workcell.

The feedback endpoint at ``POST /api/ses/feedback`` is deployed on Cloud Run
with ``--ingress=all`` (public). Without signature verification any anonymous
caller could POST a forged SES bounce/complaint event and pollute the
suppression list. AWS publishes the canonical verification procedure here:

  https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html

This module is the strict verifier:

  - canonical string is built in the field order AWS specifies for each
    ``Type`` (Notification vs SubscriptionConfirmation/UnsubscribeConfirmation);
  - ``SignatureVersion`` must be ``"1"`` (SHA1+RSA, legacy default) or ``"2"``
    (SHA256+RSA, AWS-recommended);
  - ``SigningCertURL`` host must be ``sns.<region>.amazonaws.com`` over HTTPS;
  - the X.509 cert is fetched (with a small in-process TTL cache) and the RSA
    signature is verified via ``cryptography``;
  - if a TopicArn allowlist is configured, the message's TopicArn must match.

Returns / raises a strict :class:`SnsSignatureError` on any failure — no
soft-fail, no "warn and continue". Callers wire this in **before** any business
logic so a forged payload never reaches the suppression handlers.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509 import Certificate, load_pem_x509_certificate

_LOG = logging.getLogger("samus.feedback.sns_signature")

# AWS-documented allowlist of fields to canonicalize, in order, per Type.
# Source: AWS SNS Developer Guide, "Verifying the signatures of Amazon SNS messages".
_NOTIFICATION_FIELDS: tuple[str, ...] = (
    "Message",
    "MessageId",
    "Subject",  # optional; included only if present per spec
    "Timestamp",
    "TopicArn",
    "Type",
)
_SUBSCRIPTION_FIELDS: tuple[str, ...] = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)

_ALLOWED_SIGNATURE_VERSIONS: frozenset[str] = frozenset({"1", "2"})

# host must be sns.<region>.amazonaws.com — region is alphanumeric + hyphen.
_SIGNING_CERT_HOST_RE = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$", re.IGNORECASE)


class SnsSignatureError(Exception):
    """Raised when an SNS message fails any part of signature verification."""


class _CertCache:
    """Tiny TTL cache for fetched X.509 signing certs (per SigningCertURL).

    SNS rotates signing certs on a roughly multi-year cadence but the URL is
    stable for the lifetime of one cert. A 1-hour TTL keeps us safe across
    rotations without re-fetching on every request.
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Certificate]] = {}

    def get(self, url: str) -> Certificate | None:
        with self._lock:
            entry = self._entries.get(url)
            if entry is None:
                return None
            expires_at, cert = entry
            if expires_at < time.time():
                self._entries.pop(url, None)
                return None
            return cert

    def set(self, url: str, cert: Certificate) -> None:
        with self._lock:
            self._entries[url] = (time.time() + self._ttl, cert)

    def clear(self) -> None:  # pragma: no cover - exercised via tests only
        with self._lock:
            self._entries.clear()


_CERT_CACHE = _CertCache()


def _allowed_topic_arns() -> frozenset[str]:
    """Read SAMUS_FEEDBACK_ALLOWED_TOPIC_ARNS — comma-separated.

    Fail closed in non-development: an empty allowlist means any SNS TopicArn
    is accepted, which allows spoofed notifications from unrelated AWS accounts.
    In production the operator MUST set this variable to the specific ARNs for
    the HustleForge SES feedback topic.
    """
    import logging as _logging

    raw = os.getenv("SAMUS_FEEDBACK_ALLOWED_TOPIC_ARNS", "").strip()
    if not raw:
        env = os.getenv("SAMUS_ENV", "development") or "development"
        if env not in ("development", "dev", "test"):
            raise RuntimeError(
                "samus.sns_signature.allowlist_required: "
                "SAMUS_FEEDBACK_ALLOWED_TOPIC_ARNS must be set in non-development "
                "environments. An empty allowlist accepts SNS messages from any "
                "AWS account — set it to the specific topic ARN(s) for this deployment."
            )
        _logging.getLogger("samus.feedback.sns_signature").warning(
            "SAMUS_FEEDBACK_ALLOWED_TOPIC_ARNS unset — accepting any SNS TopicArn "
            "(development mode only)"
        )
        return frozenset()
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


def _fields_for_type(message_type: str) -> tuple[str, ...]:
    if message_type == "Notification":
        return _NOTIFICATION_FIELDS
    if message_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        return _SUBSCRIPTION_FIELDS
    raise SnsSignatureError(f"unsupported SNS Type: {message_type!r}")


def build_string_to_sign(message: Mapping[str, Any]) -> str:
    """Build the canonical bytestring AWS signs over.

    Format per AWS spec: for each field in the per-Type order, emit
    ``<field>\\n<value>\\n``. The ``Subject`` field is included **only** for
    Notifications and **only** if the message actually contains it.
    """
    message_type = str(message.get("Type") or "")
    fields = _fields_for_type(message_type)
    lines: list[str] = []
    for name in fields:
        if name == "Subject" and "Subject" not in message:
            continue
        value = message.get(name)
        if value is None:
            # Notification messages may have an empty Subject; spec says omit
            # if absent, include if present even when empty. Other required
            # fields missing is a hard fail.
            if name == "Subject":
                continue
            raise SnsSignatureError(f"missing required field for canonicalization: {name}")
        lines.append(name)
        lines.append(str(value))
    # Trailing newline after the last value is part of the spec.
    return "\n".join(lines) + "\n"


def _validate_signing_cert_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise SnsSignatureError(f"SigningCertURL must be HTTPS: {url!r}")
    host = parsed.hostname or ""
    if not _SIGNING_CERT_HOST_RE.match(host):
        raise SnsSignatureError(
            f"SigningCertURL host not in *.amazonaws.com SNS allowlist: {host!r}"
        )


def _fetch_signing_cert(url: str, *, http_get: Any | None = None) -> Certificate:
    """Fetch and parse the X.509 cert from ``SigningCertURL``.

    ``http_get`` is an injection seam for tests so they don't need to monkey-
    patch ``httpx``. Default uses ``httpx.get`` with a 10s timeout.
    """
    cached = _CERT_CACHE.get(url)
    if cached is not None:
        return cached

    _validate_signing_cert_url(url)

    if http_get is None:

        def _default_get(u: str) -> httpx.Response:
            return httpx.get(u, timeout=10.0, follow_redirects=False)

        http_get = _default_get

    try:
        resp = http_get(url)
    except Exception as exc:
        raise SnsSignatureError(f"failed to fetch SigningCertURL: {exc}") from exc

    status = getattr(resp, "status_code", None)
    if status != 200:
        raise SnsSignatureError(f"SigningCertURL returned status {status}")

    pem_bytes = getattr(resp, "content", None)
    if not pem_bytes:
        raise SnsSignatureError("SigningCertURL returned empty body")

    try:
        cert = load_pem_x509_certificate(pem_bytes)
    except Exception as exc:
        raise SnsSignatureError(f"SigningCertURL did not return a valid PEM cert: {exc}") from exc

    _CERT_CACHE.set(url, cert)
    return cert


def _hash_algorithm_for(version: str) -> hashes.HashAlgorithm:
    # AWS SignatureVersion "1" = SHA1, "2" = SHA256. Both are RSA over PKCS1v15.
    return hashes.SHA1() if version == "1" else hashes.SHA256()  # noqa: S303 — AWS spec


def verify_sns_message(
    message: Mapping[str, Any],
    *,
    allowed_topic_arns: Iterable[str] | None = None,
    http_get: Any | None = None,
) -> None:
    """Strict-verify an SNS message dict. Raises :class:`SnsSignatureError` on any failure.

    Steps:
      1. Validate ``Type`` is one we accept and ``SignatureVersion`` is "1"|"2".
      2. Validate ``SigningCertURL`` is HTTPS and matches the SNS host pattern.
      3. Validate the message's ``TopicArn`` is in the allowlist (if configured).
      4. Build the canonical string-to-sign per AWS spec.
      5. Fetch (with cache) and parse the X.509 cert.
      6. RSA-verify the base64-decoded ``Signature`` over the canonical bytes.

    No return value on success — exceptions are the contract.
    """
    if not isinstance(message, Mapping):
        raise SnsSignatureError("message must be a mapping")

    message_type = str(message.get("Type") or "")
    if message_type not in ("Notification", "SubscriptionConfirmation", "UnsubscribeConfirmation"):
        raise SnsSignatureError(f"unsupported SNS Type: {message_type!r}")

    signature_version = str(message.get("SignatureVersion") or "")
    if signature_version not in _ALLOWED_SIGNATURE_VERSIONS:
        raise SnsSignatureError(
            f"SignatureVersion must be one of {sorted(_ALLOWED_SIGNATURE_VERSIONS)};"
            f" got {signature_version!r}"
        )

    signing_cert_url = str(message.get("SigningCertURL") or "")
    if not signing_cert_url:
        raise SnsSignatureError("SigningCertURL is required")
    _validate_signing_cert_url(signing_cert_url)

    signature_b64 = str(message.get("Signature") or "")
    if not signature_b64:
        raise SnsSignatureError("Signature is required")
    try:
        signature_bytes = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise SnsSignatureError(f"Signature is not valid base64: {exc}") from exc

    # TopicArn allowlist — explicit param wins, else env-var, else no-op.
    allowlist = (
        frozenset(allowed_topic_arns) if allowed_topic_arns is not None else _allowed_topic_arns()
    )
    topic_arn = str(message.get("TopicArn") or "")
    if allowlist and topic_arn not in allowlist:
        raise SnsSignatureError(
            f"TopicArn {topic_arn!r} not in SAMUS_FEEDBACK_ALLOWED_TOPIC_ARNS allowlist"
        )

    string_to_sign = build_string_to_sign(message).encode("utf-8")

    cert = _fetch_signing_cert(signing_cert_url, http_get=http_get)
    public_key = cert.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise SnsSignatureError("SigningCert public key is not RSA")

    try:
        public_key.verify(
            signature_bytes,
            string_to_sign,
            padding.PKCS1v15(),
            _hash_algorithm_for(signature_version),
        )
    except InvalidSignature as exc:
        raise SnsSignatureError("signature did not verify against SigningCert") from exc
    except Exception as exc:
        # Defensive: cryptography can raise UnsupportedAlgorithm etc.
        raise SnsSignatureError(f"signature verification failed: {exc}") from exc


__all__ = [
    "SnsSignatureError",
    "build_string_to_sign",
    "verify_sns_message",
    "_CERT_CACHE",
]
