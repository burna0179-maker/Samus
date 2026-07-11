"""``input_hash``-keyed persistent result cache for the SEO workcell.

WHY THIS EXISTS
---------------
``audit_site`` already deduplicates via ``GLOBAL_IDEMPOTENCY_STORE`` keyed on
the target URL, but that store is a pure in-process ``OrderedDict``. On Cloud
Run the container filesystem + process both die on every scale-to-zero, so a
fresh instance boots with a COLD cache and recomputes work it has already done
— firing the LLM/network path and appending an audit-ledger row with an
``input_hash`` identical to earlier runs. The evidence: the same site audited
9 times in one day, all sharing one ``input_hash``.

This module is a DURABLE sidecar cache keyed on that same deterministic
``input_hash`` (``events._deterministic_hash`` over the request payload). A hit
within the TTL window returns the prior result verbatim and skips the expensive
recompute. It is backed by an append-only JSONL ledger so it survives process
restarts (and, when ``SAMUS_LEDGER_BACKEND=firestore`` is wired elsewhere, the
same input-hash key is portable to a cross-instance store).

WIRED-DORMANT
-------------
The whole feature is gated by ``settings.seo_audit_cache_enabled`` (env
``SAMUS_AUDIT_CACHE_ENABLED``), which DEFAULTS TO FALSE. With the flag off the
cache is never consulted or written and ``audit_site`` behaves exactly as
before. An operator flips the flag to activate it; nothing changes in
production until then.

SAFE FALLBACK
-------------
Every cache read is best-effort: a missing/unreadable/corrupt sidecar, a
malformed record, or a payload that no longer validates against the current
result model all degrade to a cache MISS, so the caller recomputes normally.
The cache can never make ``audit_site`` fail — at worst it is a no-op.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from backend.common import events, persistence

log = logging.getLogger("samus.seo.audit_cache")

# Default TTL: 24h. An audit older than this is re-run even on a hit so a site
# that changed (new content, fixed issues) eventually gets a fresh score.
_DEFAULT_TTL_SECONDS = 24 * 3600

_CACHE_PATH_DEFAULT = "/opt/samus/data/seo/seo_audit_cache.jsonl"

# How many trailing records to scan for a matching input_hash. The sidecar is
# small (one row per distinct audit result) and we only care about recent
# entries, so a bounded tail keeps the lookup O(1)-ish regardless of history.
_SCAN_LIMIT = 500


def _cache_path() -> str:
    """Resolve the sidecar cache path.

    Prefers an explicit ``SAMUS_SEO_AUDIT_CACHE_PATH``. Otherwise derives a
    sibling of the active audit ledger (``SAMUS_SEO_AUDIT_PATH``) so the cache
    lives next to the evidence it mirrors; falls back to the module default.
    """
    explicit = os.getenv("SAMUS_SEO_AUDIT_CACHE_PATH")
    if explicit:
        return explicit
    audit_path = os.getenv("SAMUS_SEO_AUDIT_PATH")
    if audit_path:
        base, _, _ = audit_path.rpartition(".")
        if base:
            return f"{base}_cache.jsonl"
    return _CACHE_PATH_DEFAULT


def _ttl_seconds() -> int:
    """Cache TTL in seconds. Configurable via ``SAMUS_AUDIT_CACHE_TTL_SECONDS``."""
    raw = os.getenv("SAMUS_AUDIT_CACHE_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_TTL_SECONDS
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS
    return val if val > 0 else _DEFAULT_TTL_SECONDS


def compute_input_hash(payload: Any) -> str:
    """Deterministic ``input_hash`` for a request payload.

    Delegates to the SAME hash the audit ledger records
    (``events._deterministic_hash``) so a cache key lines up 1:1 with the
    ``input_hash`` field in ``seo_audit.jsonl``.
    """
    return events._deterministic_hash(payload)


def _cache_enabled() -> bool:
    """Read the WIRE flag fresh so an operator flip takes effect on next call.

    Defaults to the pre-feature behaviour (disabled) if settings can't be read
    for any reason — the cache is strictly additive and must fail safe.
    """
    try:
        from backend.common.config import get_settings

        return bool(getattr(get_settings(), "seo_audit_cache_enabled", False))
    except Exception:  # noqa: BLE001 — never let config reads break the caller
        return False


def get_cached(input_hash: str) -> dict[str, Any] | None:
    """Return the cached result dict for ``input_hash`` if a fresh hit exists.

    A "fresh" hit is the most-recent sidecar record whose ``input_hash``
    matches AND whose ``cached_at`` epoch is within the TTL window. Returns
    ``None`` on a miss, on any read/parse error (safe fallback -> recompute),
    or when the flag is off. Never raises.
    """
    if not _cache_enabled():
        return None
    try:
        ledger = persistence.JsonlLedger(_cache_path())
        records = ledger.tail(_SCAN_LIMIT)
    except Exception as exc:  # noqa: BLE001 — corrupt/missing sidecar -> miss
        log.debug("seo audit cache read failed: %s", exc)
        return None

    ttl = _ttl_seconds()
    now = time.time()
    # Walk newest-first so the freshest matching entry wins.
    for rec in reversed(records):
        if not isinstance(rec, dict):
            continue
        if rec.get("input_hash") != input_hash:
            continue
        cached_at = rec.get("cached_at")
        try:
            age = now - float(cached_at)
        except (TypeError, ValueError):
            continue
        if age < 0 or age >= ttl:
            # Future-dated (clock skew) or expired -> treat as no usable hit.
            return None
        result = rec.get("result")
        if isinstance(result, dict):
            return result
        return None
    return None


def put_cached(input_hash: str, result: dict[str, Any]) -> None:
    """Append a cache record for ``input_hash``. Best-effort; never raises.

    No-op when the flag is off. Write failures (read-only FS, ephemeral Cloud
    Run disk) are swallowed and logged — the caller already has its result.
    """
    if not _cache_enabled():
        return
    try:
        ledger = persistence.JsonlLedger(_cache_path())
        ledger.append(
            {
                "input_hash": input_hash,
                "cached_at": time.time(),
                "result": result,
            }
        )
    except Exception as exc:  # noqa: BLE001 — cache write must never block work
        log.warning("seo audit cache write failed: %s", exc)


def cached_or_compute(
    payload: Any,
    compute: Callable[[], dict[str, Any]],
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Return ``(result_dict, from_cache)`` for a request.

    * Flag off  -> always calls ``compute`` (``from_cache`` False), no I/O.
    * ``force_refresh`` True -> bypasses the read, always recomputes, and
      refreshes the cache entry so the next non-forced call is warm.
    * Otherwise -> returns a fresh cached hit when present, else computes and
      caches the fresh result.

    ``compute`` must return a plain JSON-serialisable dict (the result model's
    ``model_dump()``). The caller is responsible for re-validating the dict back
    into its model.
    """
    if not _cache_enabled():
        return compute(), False

    input_hash = compute_input_hash(payload)

    if not force_refresh:
        hit = get_cached(input_hash)
        if hit is not None:
            log.info("seo audit cache hit input_hash=%s", input_hash[:12])
            return hit, True

    result = compute()
    put_cached(input_hash, result)
    return result, False
