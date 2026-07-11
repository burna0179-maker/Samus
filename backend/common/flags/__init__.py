"""Samus shared-core flag/config subsystem (E5 D13).

Two-layer model, deliberately separate:

* ``Settings`` (``backend/common/config.py``) — boot-time, env-typed,
  immutable configuration that drives WIRING decisions, bound from
  ``SAMUS_*`` env vars by ``bootstrap_settings``.
* This package — a runtime-mutable, persisted registry of operator-
  toggleable behaviour gates that MIRROR the ``Settings`` fields, so a
  one-line behaviour toggle does not require a restart.

Boot wiring (in the app/worker factory) installs the store and registers
the catalog::

    from backend.common.flags import init_default_store, register_all
    init_default_store()      # value JSON under the writable state root
    register_all()            # declare every Samus flag spec

Runtime call sites resolve the effective value, fail-soft to settings::

    from backend.common.flags import is_enabled
    from backend.common.config import get_settings
    s = get_settings()
    if is_enabled("cash_engine_enabled", s.cash_engine_enabled):
        ...

Public surface — kept narrow.
"""

from __future__ import annotations

from .catalog import build_specs, register_all
from .runtime import effective_value, is_enabled, is_enabled_fc
from .store import (
    FlagError,
    FlagImmutableError,
    FlagKind,
    FlagNotFoundError,
    FlagPrimitive,
    FlagSpec,
    FlagSpecConflictError,
    FlagStore,
    FlagTypeMismatchError,
    FlagValue,
    all_flags,
    default_persist_path,
    get_flag,
    init_default_store,
    register_flag,
    reset_default_store_for_testing,
    set_flag,
)

__all__ = [
    # store
    "FlagError",
    "FlagImmutableError",
    "FlagKind",
    "FlagNotFoundError",
    "FlagPrimitive",
    "FlagSpec",
    "FlagSpecConflictError",
    "FlagStore",
    "FlagTypeMismatchError",
    "FlagValue",
    "all_flags",
    "default_persist_path",
    "get_flag",
    "init_default_store",
    "register_flag",
    "reset_default_store_for_testing",
    "set_flag",
    # catalog
    "build_specs",
    "register_all",
    # runtime
    "effective_value",
    "is_enabled",
    "is_enabled_fc",
]
