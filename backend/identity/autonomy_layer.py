"""ECO-ADR-0001 shared-autonomy contract wiring for Samus.

Samus already had ``backend/common/autonomy.py`` — but that is a domain
MAPE-K planner, NOT the shared cross-agent autonomy CONTRACT. The other
agents ride ``_shared.autonomy`` (posture-driven mode resolution, shadow-mode,
the reversible-fires / irreversible-gates rung ladder, canonical telemetry).
This module grafts that contract onto Samus so it is posture-gated like every
other agent.

WHAT THIS DOES (and deliberately does NOT do):

* It resolves Samus's autonomy MODE from the operator posture + flags via
  ``_shared.autonomy.modes.resolve_band_mode`` — the SAME double-gate the
  band-enact agents use: ACTIVE actuation requires BOTH posture=present AND
  ``SN_AUTONOMY_LAYER_AUTHORITY`` armed. Running the present launcher alone
  never actuates a Samus outward act.
* It builds an :class:`_shared.autonomy.AutonomyContract` from Samus's
  registered profile (``get_profile("samus")`` — REVERSIBLE ceiling, inward
  pipeline autonomous, OUTWARD acts gate) fused with Samus's domain cycle
  (the existing ``backend.common.autonomy.run_cycle``).
* MANDATORY SHADOW-MODE + TELEMETRY: in any posture short of armed-present the
  contract runs in SHADOW (records the ACT it WOULD take, never actuates) or
  DORMANT, and every drive emits the canonical autonomy telemetry schema. This
  satisfies the ECO-ADR-0001 "mandatory shadow-mode + telemetry — for testing"
  requirement.
* Samus's OUTWARD acts (send / call / charge) are NOT actuated here. The
  contract is wired with NO live actuator by default (``actuator=None``): a
  FIRE-rung effect records ``carried_out=False`` rather than touching anything,
  and the existing per-domain gates (Stake Sentence, the $1/day cap, the
  triple-gate website pattern, EFH) remain the authoritative controls. The
  shared layer is purely additive observability + posture gating on top.

FAIL-SAFE: ``build_samus_autonomy`` never raises (``_shared.autonomy.boot``
swallows construction errors and returns None). The autonomy layer is never
on Samus's critical boot path — if anything is wrong the gateway boots exactly
as before, without an autonomy contract.

DORMANT BY DEFAULT: nothing here changes runtime behaviour until the operator
posture is 'present' AND ``SAMUS_AUTONOMY_LAYER_ENABLED`` is set. With the flag
unset (the default) the gateway lifespan does not even build the contract.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

from .shared_bootstrap import ensure_shared_importable

log = logging.getLogger("samus.identity.autonomy_layer")

ENV_LAYER_ENABLED = "SAMUS_AUTONOMY_LAYER_ENABLED"
_TRUE = {"1", "true", "yes", "on", "y", "t"}
_AGENT = "samus"


def layer_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the shared autonomy layer is armed for Samus (default OFF)."""
    env = env if env is not None else os.environ
    return str(env.get(ENV_LAYER_ENABLED, "")).strip().lower() in _TRUE


def _materialize(cycle_input: Any, reflection: Any, candidates: dict) -> Any:
    """Build the next governed-cycle input from the prior reflection.

    Samus's domain cycle takes a flat dict (objective + inputs); the shared
    contract calls ``materialize`` between chained steps to fold a reflection
    back into the next input. We keep the input as-is on the first step and
    pass the reflection through unchanged on subsequent ones — Samus's MAPE-K
    cycle is single-pass today, so chaining is observational (SHADOW records
    the would-chain without re-running anything consequential).
    """
    return cycle_input


def _run(cycle_input: Any) -> dict[str, Any]:
    """Adapter onto Samus's existing domain MAPE-K cycle.

    Reuses ``backend.common.autonomy.run_cycle`` — the shared contract governs
    WHEN/whether it chains + actuates; the domain logic itself is unchanged.
    """
    from backend.common import autonomy as domain_autonomy  # noqa: PLC0415

    if isinstance(cycle_input, Mapping):
        task_id = str(cycle_input.get("task_id") or "")
        objective = str(cycle_input.get("objective") or "")
        inputs = dict(cycle_input.get("inputs") or {})
    else:
        task_id, objective, inputs = "", str(cycle_input or ""), {}
    return domain_autonomy.run_cycle(task_id=task_id, objective=objective, inputs=inputs)


def resolve_samus_mode(
    *, env: Mapping[str, str] | None = None,
    quorum_available: Optional[bool] = None,
):
    """Resolve Samus's autonomy MODE through the shared double/triple gate.

    Returns a ``_shared.autonomy.Mode`` (or None if the shared contract is
    unavailable). ACTIVE requires posture=present AND
    ``SN_AUTONOMY_LAYER_AUTHORITY`` armed; otherwise SHADOW (away/present-
    unarmed) or DORMANT (unresolved posture / kill-switch).
    """
    if not ensure_shared_importable():
        return None
    from _shared.autonomy.modes import resolve_band_mode  # noqa: PLC0415

    return resolve_band_mode(
        _AGENT, env=dict(env) if env is not None else None,
        quorum_available=quorum_available,
    )


def build_samus_autonomy(
    *, env: Mapping[str, str] | None = None,
    telemetry_sink: Any = None,
) -> Optional[Any]:
    """Build Samus's shared ``AutonomyContract`` (or None — never raises).

    The contract is wired with NO live actuator (Samus's outward acts ride
    their own domain gates), so it is observational + posture-gated:
    SHADOW/DORMANT record what they WOULD do; even an armed-ACTIVE FIRE-rung
    effect records ``carried_out=False`` because no actuator is supplied.
    """
    if not ensure_shared_importable():
        log.warning("samus autonomy: _shared not importable — skipping autonomy layer")
        return None
    try:
        from _shared.autonomy.boot import build_autonomy  # noqa: PLC0415
        from _shared.autonomy.modes import resolve_band_mode  # noqa: PLC0415

        env_map = dict(env) if env is not None else None
        # Resolve through the band double-gate so the contract is in SHADOW
        # unless the operator has explicitly armed present+authority for Samus.
        mode = resolve_band_mode(_AGENT, env=env_map)

        contract = build_autonomy(
            _AGENT,
            run=_run,
            materialize=_materialize,
            # No planner override -> the default MarkerPlanner from Samus's
            # registered profile (clarify_markers=("ambiguous_outreach",)).
            env=env_map,
            mode=mode,
            # No actuator / gate: outward acts ride their existing domain gates.
            actuator=None,
            gate=None,
            telemetry_sink=telemetry_sink,
        )
        if contract is not None:
            log.info(
                "samus autonomy: shared contract built (mode=%s) — outward acts "
                "still ride their domain gates; this layer is observational + "
                "posture-gated.",
                getattr(mode, "value", mode),
            )
        return contract
    except Exception:  # noqa: BLE001 — must never break gateway boot
        log.exception("samus autonomy: build failed — booting without it")
        return None


__all__ = [
    "ENV_LAYER_ENABLED",
    "layer_enabled",
    "resolve_samus_mode",
    "build_samus_autonomy",
]
