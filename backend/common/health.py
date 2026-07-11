"""Canonical 4-state /health surface (Hustleforge agent canonical §9).

The four states (per Anita round-6 Agent BB operational debt):

  * ``ok`` — all probes pass.
  * ``degraded`` — at least one optional dependency failing, but the
    agent can still serve traffic.
  * ``awaiting_operator_action`` — a credential / config gap the
    operator must fix (e.g. AWS keys unset, ledger key missing). The
    agent is up but cannot do real work yet.
  * ``fail`` — a hard invariant broken (e.g. ledger tamper detected,
    settings load failure). Process should be restarted.

Existing ``backend/common/app_factory.create_base_app`` ships a trivial
``GET /health -> {"status": "ok"}`` for back-compat with the 1281 tests
already wired around it. This module adds a richer probe that any
workcell can mount on a different path (e.g. ``/healthz/full``) without
breaking the existing surface. The gateway can also call
:func:`probe` directly and surface the result on a private admin route.

Probes registered here are deliberately additive — no domain code is
required to call any of them. The shell never decides for a domain
module that its absent backend is degraded vs fail; that classification
lives with the registrar.

Public surface:
  * :class:`HealthState` — Enum-like literals (``OK``, ``DEGRADED``,
    ``AWAITING_OPERATOR_ACTION``, ``FAIL``).
  * :class:`ProbeResult` — single probe outcome dataclass.
  * :class:`HealthRegistry` — registrar + ``probe()`` that aggregates.
  * :func:`default_registry` — module-level singleton with the canonical
    Samus probes pre-registered (audit ledger integrity, settings load).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

log = logging.getLogger("samus.health")


class HealthState:
    """String literals — duck-typed so JSON serialisation is trivial."""

    OK = "ok"
    DEGRADED = "degraded"
    AWAITING_OPERATOR_ACTION = "awaiting_operator_action"
    FAIL = "fail"


# Ordering for "worst-state wins" aggregation. Higher index = worse.
_SEVERITY = {
    HealthState.OK: 0,
    HealthState.DEGRADED: 1,
    HealthState.AWAITING_OPERATOR_ACTION: 2,
    HealthState.FAIL: 3,
}


@dataclass
class ProbeResult:
    """One probe's verdict."""

    name: str
    state: str  # HealthState.*
    detail: str = ""
    # Optional structured extras (e.g. ledger seq, queue depth) the
    # admin route can pretty-print.
    extras: Dict[str, object] = field(default_factory=dict)


ProbeCallable = Callable[[], ProbeResult]


@dataclass
class HealthRegistry:
    """Probe registrar. Thread-safe — probes run sequentially under the
    lock, so a probe that takes a long time blocks others. Keep probes
    cheap (sub-100ms); offload anything slow to a background snapshot.
    """

    _probes: Dict[str, ProbeCallable] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def register(self, name: str, probe: ProbeCallable) -> None:
        """Add or replace a probe by name."""
        with self._lock:
            self._probes[name] = probe

    def unregister(self, name: str) -> None:
        with self._lock:
            self._probes.pop(name, None)

    def names(self) -> List[str]:
        with self._lock:
            return list(self._probes.keys())

    def probe(self) -> Dict[str, object]:
        """Run every registered probe; return the aggregate dict.

        Shape:

            {
              "state":  "ok" | "degraded" | "awaiting_operator_action" | "fail",
              "probes": {
                "<name>": {"state": "...", "detail": "...", "extras": {...}},
                ...
              }
            }

        ``state`` is the worst single probe state. An empty registry
        returns ``ok`` with no probes.
        """
        with self._lock:
            items = list(self._probes.items())

        results: Dict[str, Dict[str, object]] = {}
        worst = HealthState.OK
        for name, probe in items:
            try:
                r = probe()
            except Exception as exc:
                # A probe that raises is itself a fail signal — never let
                # the aggregator throw out into the response path.
                log.exception("health probe %r raised: %s", name, exc)
                r = ProbeResult(name=name, state=HealthState.FAIL, detail=f"probe raised: {exc!r}")
            results[name] = {
                "state": r.state,
                "detail": r.detail,
                "extras": r.extras,
            }
            if _SEVERITY[r.state] > _SEVERITY[worst]:
                worst = r.state
        return {"state": worst, "probes": results}


# ---------- canonical probes ---------------------------------------------


def probe_audit_ledger() -> ProbeResult:
    """Verify the daily audit chain; map any break to ``fail``.

    Per INV-3, any tamper / chain mismatch is a hard fail — the agent
    must refuse to keep appending until an operator intervenes.
    """
    try:
        from .audit_ledger import get_default_ledger

        state = get_default_ledger().verify()
    except Exception as exc:
        return ProbeResult(
            name="audit_ledger",
            state=HealthState.FAIL,
            detail=f"verify raised: {exc!r}",
        )
    if state.ok:
        return ProbeResult(
            name="audit_ledger",
            state=HealthState.OK,
            detail=f"chain_length={state.chain_length}",
            extras={"chain_length": state.chain_length, "last_hash": state.last_hash[:16]},
        )
    return ProbeResult(
        name="audit_ledger",
        state=HealthState.FAIL,
        detail=state.detail or "ledger verify failed",
        extras={"broken_at": state.broken_at, "chain_length": state.chain_length},
    )


def probe_ledger_secret() -> ProbeResult:
    """Check that a real ledger / shared HMAC key is configured.

    Missing key -> ``awaiting_operator_action`` (per INV-8 spirit — the
    audit chain falls back to a dev fallback and any signature beyond
    the dev process boundary is meaningless until the operator seals one).
    """
    try:
        from .config import get_settings

        s = get_settings()
    except Exception as exc:
        return ProbeResult(
            name="ledger_secret",
            state=HealthState.FAIL,
            detail=f"settings load raised: {exc!r}",
        )
    ledger_key = getattr(s, "samus_ledger_secret_key", "") or ""
    shared_key = getattr(s, "shared_hmac_key", "") or ""
    if ledger_key:
        return ProbeResult(
            name="ledger_secret",
            state=HealthState.OK,
            detail="samus_ledger_secret_key sealed",
        )
    if shared_key:
        return ProbeResult(
            name="ledger_secret",
            state=HealthState.DEGRADED,
            detail="using shared_hmac_key fallback (rotating it will invalidate the audit chain)",
        )
    return ProbeResult(
        name="ledger_secret",
        state=HealthState.AWAITING_OPERATOR_ACTION,
        detail="no SAMUS_LEDGER_SECRET_KEY or SAMUS_SHARED_HMAC_KEY set — seal one",
    )


def probe_settings_loaded() -> ProbeResult:
    """Smoke test: get_settings() returns without raising."""
    try:
        from .config import get_settings

        s = get_settings()
        return ProbeResult(
            name="settings",
            state=HealthState.OK,
            detail=f"env={s.env} service={s.service_name}",
        )
    except Exception as exc:
        return ProbeResult(
            name="settings",
            state=HealthState.FAIL,
            detail=f"settings load raised: {exc!r}",
        )


# ---------- module-level default registry ---------------------------------

_default: Optional[HealthRegistry] = None
_default_lock = threading.Lock()


def default_registry() -> HealthRegistry:
    """Lazy singleton registry pre-wired with canonical Samus probes.

    Domain workcells extend by importing this and calling ``register``;
    they MUST NOT replace probes registered here.
    """
    global _default
    if _default is not None:
        return _default
    with _default_lock:
        if _default is not None:
            return _default
        reg = HealthRegistry()
        reg.register("settings", probe_settings_loaded)
        reg.register("ledger_secret", probe_ledger_secret)
        reg.register("audit_ledger", probe_audit_ledger)
        _default = reg
        return _default


def reset_default_registry() -> None:
    """Test helper — drop the cached singleton."""
    global _default
    with _default_lock:
        _default = None
