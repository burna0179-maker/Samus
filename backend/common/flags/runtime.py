"""Runtime-effective flag resolution: a persisted operator flip wins over
the boot-time settings value, else the settings value is used.

Samus subsystems wire at boot from ``Settings`` (env-bound via
``bootstrap_settings``). For a behaviour gate that is safe to flip mid-
process, a runtime call site should consult the flag store so an operator
toggle (``set_flag``) takes effect without a restart. When the flag store
is not initialised (unit tests, or a boot that never installed it) or the
flag is not registered, resolution falls back to the eagerly-supplied
settings value — so importing this module changes nothing until the store
is installed.

Resolution order:

    1. flag store initialised AND flag registered  -> flag's current value
    2. otherwise                                   -> settings_fallback

The fallback is mandatory and passed EAGERLY (not lazily) so an
import-time misconfiguration of the flag store cannot silently shadow a
real settings value. Pure read; no side effects, no I/O.

Idiom for a runtime check site::

    from backend.common.flags.runtime import is_enabled
    from backend.common.config import get_settings
    s = get_settings()
    if is_enabled("cash_engine_enabled", s.cash_engine_enabled):
        ...

Fail-closed helper ``is_enabled_fc`` treats a missing fallback /
unresolvable flag as DISABLED (False) — for gates where "unknown" must
mean "off".
"""

from __future__ import annotations

from typing import TypeVar

from .store import FlagError, FlagPrimitive, get_flag

__all__ = ["effective_value", "is_enabled", "is_enabled_fc"]

_T = TypeVar("_T", bound=FlagPrimitive)


def effective_value(name: str, settings_fallback: _T) -> _T:
    """Return the operator-effective value for ``name``.

    Falls back to ``settings_fallback`` when the flag store is not
    initialised or the flag is not registered — AND when the flag has no
    PERSISTED operator flip (``is_default``). The catalog registers pure
    model defaults (env deliberately not applied — see catalog._defaults),
    so returning the registered default here would shadow an env-armed
    settings value: exactly what happened 2026-07-03 when store init moved
    into create_base_app and every env-armed flag (auto_stake_enabled=true)
    silently read as its catalog default (False). Only an explicit
    ``set_flag`` flip may override the boot-time settings value — which is
    what this module's docstring promised all along.
    """
    try:
        fv = get_flag(name)
    except FlagError:
        return settings_fallback
    if fv.is_default:
        return settings_fallback
    return fv.value  # type: ignore[return-value]


def is_enabled(name: str, settings_fallback: bool) -> bool:
    """Boolean convenience over :func:`effective_value`.

    Coerces the resolved value to ``bool``. Use for ``*_enabled`` gates.
    """
    return bool(effective_value(name, settings_fallback))


def is_enabled_fc(name: str, settings_fallback: bool = False) -> bool:
    """Fail-closed boolean resolution.

    Identical to :func:`is_enabled` but the default ``settings_fallback``
    is ``False`` — when neither a registered flag nor an explicit fallback
    says otherwise, the gate is treated as DISABLED. For controls where an
    unresolved state must mean "off".
    """
    return bool(effective_value(name, settings_fallback))
