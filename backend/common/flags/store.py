"""Runtime-mutable, persisted feature-flag store (Hustleforge agent §5).

Samus shared-core D13 (flags/config). ABSORBED from the ecosystem flag
reference (Darwin ``backend/core/flags/store.py`` mirrored from Major /
Anita) and ADAPTED to Samus's flat ``backend/common/`` layout:

  * No ``shared.utils.safe_io`` dependency (Samus has no such module) —
    the atomic JSON write is self-contained here, matching the existing
    ``os.replace``-based atomic writes elsewhere in ``backend/common``
    (apollo_budget, llm_budget, persistence).
  * No ``declare_posture`` / ``AuthorityTier`` security coupling — Samus
    does not carry the Darwin/Major security-posture package; this module
    is a pure read/persist primitive with no privileged side effects.
  * Persistence path is resolved through ``backend.common.state_paths``
    so the value JSON lands on the writable data volume in the container
    (``SAMUS_STATE_ROOT``) and under ``<code root>/state`` on the host.

Design contract (identical to the reference so the operator surface is
fleet-uniform):

* **Registration is write-once on the spec.** A second ``register()`` for
  a flag whose ``FlagSpec`` matches the original is a no-op so duplicate
  imports (e.g. under pytest's repeated module loads) don't trip the
  guard. A second register with a different shape raises so silent schema
  drift is impossible.
* **Values live in a single JSON file**, written atomically.
* **``mutable=False`` is enforced after first set.** A flag can still be
  ``reset()``ed back to default (operator intent).
* **Evidence-ledger integration is optional + best-effort.** When a ledger
  handle is supplied, every accepted ``set()`` appends a ``flag.changed``
  event; a ledger failure is logged but never unwinds the flag change.

Type kinds are limited to ``bool | int | float | str``. JSON serialises
each round-trip exactly. Cross-type coercion is deliberately rejected
(``bool`` is checked before ``int`` because ``bool`` subclasses ``int``).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

log = logging.getLogger("samus.flags")

__all__ = [
    "FlagError",
    "FlagImmutableError",
    "FlagNotFoundError",
    "FlagSpec",
    "FlagSpecConflictError",
    "FlagStore",
    "FlagTypeMismatchError",
    "FlagValue",
    "FlagKind",
    "FlagPrimitive",
    "all_flags",
    "default_persist_path",
    "get_flag",
    "init_default_store",
    "register_flag",
    "reset_default_store_for_testing",
    "set_flag",
]

FlagKind = Literal["bool", "int", "float", "str"]
FlagPrimitive = bool | int | float | str


# ---------------------------------------------------------------------
# Atomic IO (self-contained — mirrors the os.replace pattern used by the
# other backend/common stores; no external safe_io dependency).
# ---------------------------------------------------------------------


def _read_json(path: Path, *, default: Any = None) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_persist_path() -> Path:
    """Resolved on-disk location of the flag value map.

    Routed through ``backend.common.state_paths`` so the file lands on the
    writable data volume in the container and under ``<code root>/state``
    on the host (tests / host-venv runs).
    """
    from ..state_paths import state_path

    return state_path("flags", "flag_values.json")


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------


class FlagError(Exception):
    """Base class for the flag-subsystem exception family."""


class FlagNotFoundError(FlagError):
    """Raised when a flag name is referenced without prior registration."""


class FlagTypeMismatchError(FlagError):
    """Raised when a ``set()`` value does not match the declared kind."""


class FlagImmutableError(FlagError):
    """Raised when ``set()`` targets a flag whose ``mutable=False`` and a
    non-default value is already persisted."""


class FlagSpecConflictError(FlagError):
    """Raised when ``register()`` is called a second time with a spec whose
    shape differs from the original."""


# ---------------------------------------------------------------------
# Dataclasses (frozen — flag identity must not drift under callers)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class FlagSpec:
    """Declarative description of a flag.

    ``default_value`` is what ``get()`` returns when nothing has been
    persisted. ``kind`` constrains the type of any future ``set()``.
    ``env_var`` (Samus addition) records the ``SAMUS_*`` env var this flag
    mirrors so the catalog can be verified for parity with
    ``bootstrap_settings()`` and an operator can see the boot-time binding.
    ``mutable=False`` pins the value after first set; ``reset()`` is still
    allowed (operator intent).
    """

    name: str
    description: str
    default_value: FlagPrimitive
    kind: FlagKind
    mutable: bool = True
    category: str = "general"
    env_var: str = ""
    since_version: str = "1.0.0"

    def shape_key(self) -> tuple:
        """Hashable identity tuple used for the write-once guard."""
        return (
            self.name,
            self.description,
            self.default_value,
            self.kind,
            self.mutable,
            self.category,
            self.env_var,
            self.since_version,
        )


@dataclass(frozen=True)
class FlagValue:
    """Current value of a flag plus provenance metadata for the dashboard."""

    name: str
    value: FlagPrimitive
    set_by: str
    set_at_ts: float
    is_default: bool


# ---------------------------------------------------------------------
# Evidence-ledger duck type
# ---------------------------------------------------------------------


class _LedgerLike(Protocol):
    """Duck type for the optional evidence ledger handle.

    The store calls ``ledger.append(event, payload)`` first (Samus's
    audit-ledger interface). If the ledger does not expose ``append``, it
    falls back to ``ledger.record(event, payload)`` (Darwin's interface) so
    a cross-agent ledger stays compatible without a wrapper.
    """


# ---------------------------------------------------------------------
# Type validation
# ---------------------------------------------------------------------


def _type_matches(value: Any, kind: FlagKind) -> bool:
    """Whether ``value`` is exactly the declared kind.

    ``bool`` is checked before ``int`` because ``bool`` is an ``int``
    subclass — a flag declared ``int`` must not silently accept ``True``.
    """
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return isinstance(value, float)
    if kind == "str":
        return isinstance(value, str)
    return False


# ---------------------------------------------------------------------
# FlagStore
# ---------------------------------------------------------------------


class FlagStore:
    """Persistence + mutation engine for the flag subsystem."""

    _PERSIST_VERSION = 1
    _ACTOR_DEFAULT = "default"

    def __init__(
        self,
        *,
        persist_path: Path,
        evidence_ledger: _LedgerLike | None = None,
    ) -> None:
        self._persist_path = Path(persist_path)
        self._evidence = evidence_ledger
        self._lock = threading.RLock()
        self._specs: dict[str, FlagSpec] = {}
        # Persisted values keyed by flag name. Missing key = use default.
        # Value tuple: (value, set_by, set_at_ts).
        self._values: dict[str, tuple[FlagPrimitive, str, float]] = {}
        self._load_from_disk()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> None:
        raw = _read_json(self._persist_path, default=None)
        if not isinstance(raw, dict):
            return
        values = raw.get("values")
        if not isinstance(values, dict):
            return
        for name, entry in values.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            if "value" not in entry:
                continue
            value = entry.get("value")
            set_by = str(entry.get("set_by", self._ACTOR_DEFAULT))
            try:
                set_at_ts = float(entry.get("set_at_ts", 0.0))
            except (TypeError, ValueError):
                set_at_ts = 0.0
            self._values[name] = (value, set_by, set_at_ts)

    def _flush_to_disk(self) -> None:
        payload = {
            "version": self._PERSIST_VERSION,
            "values": {
                name: {
                    "value": value,
                    "set_by": set_by,
                    "set_at_ts": set_at_ts,
                }
                for name, (value, set_by, set_at_ts) in self._values.items()
            },
        }
        _write_json_atomic(self._persist_path, payload)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, spec: FlagSpec) -> FlagSpec:
        """Register ``spec``. Idempotent on identical shape, raises on
        shape conflict."""
        if not _type_matches(spec.default_value, spec.kind):
            raise FlagTypeMismatchError(
                f"flag {spec.name!r}: default_value {spec.default_value!r} "
                f"does not match declared kind {spec.kind!r}"
            )
        with self._lock:
            existing = self._specs.get(spec.name)
            if existing is not None:
                if existing.shape_key() == spec.shape_key():
                    return existing
                raise FlagSpecConflictError(
                    f"flag {spec.name!r} already registered with a different shape "
                    f"(existing={existing.shape_key()}, new={spec.shape_key()})"
                )
            self._specs[spec.name] = spec
            return spec

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, name: str) -> FlagValue:
        with self._lock:
            spec = self._specs.get(name)
            if spec is None:
                raise FlagNotFoundError(f"flag {name!r} is not registered")
            persisted = self._values.get(name)
            if persisted is None:
                return FlagValue(
                    name=spec.name,
                    value=spec.default_value,
                    set_by=self._ACTOR_DEFAULT,
                    set_at_ts=0.0,
                    is_default=True,
                )
            value, set_by, set_at_ts = persisted
            # A persisted value whose type no longer matches the declared
            # kind (e.g. a hand-edited JSON or a schema change) is rejected
            # in favour of the declared default — fail closed to the known
            # spec rather than serve a type-confused value.
            if not _type_matches(value, spec.kind):
                log.warning(
                    "flag %r persisted value %r does not match kind %r; "
                    "serving default %r",
                    name,
                    value,
                    spec.kind,
                    spec.default_value,
                )
                return FlagValue(
                    name=spec.name,
                    value=spec.default_value,
                    set_by=self._ACTOR_DEFAULT,
                    set_at_ts=0.0,
                    is_default=True,
                )
            return FlagValue(
                name=spec.name,
                value=value,
                set_by=set_by,
                set_at_ts=set_at_ts,
                is_default=False,
            )

    def all(self) -> list[FlagValue]:
        with self._lock:
            return [self.get(name) for name in sorted(self._specs)]

    def specs(self) -> list[FlagSpec]:
        with self._lock:
            return [self._specs[name] for name in sorted(self._specs)]

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return name in self._specs

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def set(self, name: str, value: FlagPrimitive, *, actor: str) -> FlagValue:
        with self._lock:
            spec = self._specs.get(name)
            if spec is None:
                raise FlagNotFoundError(f"flag {name!r} is not registered")
            if not _type_matches(value, spec.kind):
                raise FlagTypeMismatchError(
                    f"flag {name!r}: value {value!r} does not match kind {spec.kind!r}"
                )
            persisted = self._values.get(name)
            already_set = persisted is not None
            if not spec.mutable and already_set:
                raise FlagImmutableError(
                    f"flag {name!r} is immutable and already has a persisted value"
                )
            previous_value: FlagPrimitive = (
                persisted[0] if persisted is not None else spec.default_value
            )
            ts = time.time()
            self._values[name] = (value, actor, ts)
            self._flush_to_disk()
            new_value = FlagValue(
                name=spec.name,
                value=value,
                set_by=actor,
                set_at_ts=ts,
                is_default=False,
            )
            self._emit_changed(spec, previous_value, value, actor)
            return new_value

    def reset(self, name: str, *, actor: str) -> FlagValue:
        with self._lock:
            spec = self._specs.get(name)
            if spec is None:
                raise FlagNotFoundError(f"flag {name!r} is not registered")
            persisted = self._values.pop(name, None)
            self._flush_to_disk()
            default_value = FlagValue(
                name=spec.name,
                value=spec.default_value,
                set_by=self._ACTOR_DEFAULT,
                set_at_ts=0.0,
                is_default=True,
            )
            if persisted is not None:
                previous_value = persisted[0]
                self._emit_changed(spec, previous_value, spec.default_value, actor)
            return default_value

    # ------------------------------------------------------------------
    # Ledger plumbing (Samus .append() or Darwin .record())
    # ------------------------------------------------------------------

    def _emit_changed(
        self,
        spec: FlagSpec,
        previous: FlagPrimitive,
        current: FlagPrimitive,
        actor: str,
    ) -> None:
        if self._evidence is None:
            return
        payload: Mapping[str, Any] = {
            "flag": spec.name,
            "kind": spec.kind,
            "category": spec.category,
            "previous_value": previous,
            "new_value": current,
            "actor": actor,
        }
        try:
            # Samus canonical: ledger.append(event, payload)
            appender = getattr(self._evidence, "append", None)
            if callable(appender):
                appender("flag.changed", payload)
                return
            # Darwin canonical: ledger.record(event, payload)
            recorder = getattr(self._evidence, "record", None)
            if callable(recorder):
                recorder("flag.changed", payload)
        except Exception as exc:  # best-effort: the flag is already flipped
            log.error(
                "flag.changed evidence write failed (flag=%s category=%s "
                "previous=%r new=%r actor=%s): %s",
                spec.name,
                spec.category,
                previous,
                current,
                actor,
                exc,
            )


# ---------------------------------------------------------------------
# Module-level singleton + convenience API
# ---------------------------------------------------------------------

_REGISTRY: FlagStore | None = None
_REGISTRY_LOCK = threading.Lock()


def init_default_store(
    persist_path: Path | None = None,
    *,
    evidence_ledger: _LedgerLike | None = None,
) -> FlagStore:
    """Install the process-global :class:`FlagStore` singleton.

    ``persist_path`` defaults to :func:`default_persist_path`. Each call
    replaces the existing singleton and re-loads from disk, so the boot
    orchestrator installs the real store while tests construct their own.
    """
    global _REGISTRY
    path = Path(persist_path) if persist_path is not None else default_persist_path()
    store = FlagStore(persist_path=path, evidence_ledger=evidence_ledger)
    with _REGISTRY_LOCK:
        _REGISTRY = store
    return store


def reset_default_store_for_testing() -> None:
    """Clear the process-global singleton. Test-only helper."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def _require_store() -> FlagStore:
    if _REGISTRY is None:
        raise FlagError(
            "flag store has not been initialised; "
            "call backend.common.flags.init_default_store(...) at boot"
        )
    return _REGISTRY


def register_flag(spec: FlagSpec) -> FlagSpec:
    """Idempotent registration on the process-global singleton."""
    return _require_store().register(spec)


def get_flag(name: str) -> FlagValue:
    """Read a flag from the process-global singleton."""
    return _require_store().get(name)


def set_flag(name: str, value: FlagPrimitive, *, actor: str) -> FlagValue:
    """Mutate a flag on the process-global singleton."""
    return _require_store().set(name, value, actor=actor)


def all_flags() -> list[FlagValue]:
    """Snapshot every flag on the process-global singleton."""
    return _require_store().all()
