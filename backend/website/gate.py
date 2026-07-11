"""The gates a website-build stage must clear before it runs.

Three checks, layered, all fail-closed:

  1. **Codex gate** (:func:`codex_gate`) — every stage re-validates through
     :func:`backend.common.codex.validator.check_action`. A blocked verdict
     names the rule and escalates; an unavailable registry refuses (never
     guesses). This is identical to the cash engine's per-handoff gate.

  2. **Operator-approval gate** (:func:`approval_ok`) — in *supervised* mode a
     stage runs only if the operator has signed it off (it is in the order's
     ``approved_stages``). In *autonomous* mode (``website_autonomous_enabled``)
     approval is implied. This is the human-in-the-loop seam that makes the
     walk-through and the unattended run the same code path.

  3. **Outward gate** (:func:`outward_ok`) — the irreversible, customer-facing
     stages (publish, deliver) additionally require
     ``website_live_publish_enabled``. With it off the build runs fully —
     provisioning, populating an *unpublished* site — but never goes live or
     contacts the customer.

Keeping all three here (not scattered through the handlers) means "what is
Samus allowed to do without me, and when" is answerable from one file.
"""
from __future__ import annotations

from typing import Any

from backend.common.codex.exceptions import CodexUnavailable
from backend.common.codex.models import ProposedAction, Verdict
from backend.common.codex.validator import check_action

from .state import OUTWARD_STAGES, WebsiteBuildState

__all__ = ["codex_gate", "approval_ok", "outward_ok"]


def codex_gate(*, capability: str, payload: dict[str, Any]) -> Verdict:
    """Run a stage handoff through the Codex; fail-closed on an unavailable registry.

    Website-build actions have no dedicated ``ActionKind`` in the protocol, so
    they validate as ``"other"`` (the same kind the cash engine's contact /
    reengage handoffs use) with a descriptive ``capability`` — the banned-phrase
    (G2) scan and any ``other``-scoped rules still apply to the copy we publish.
    """
    action = ProposedAction(
        service="website",
        capability=capability,
        action_kind="other",
        payload=payload,
        proposed_by="website",
    )
    try:
        return check_action(action)
    except CodexUnavailable as exc:
        return Verdict(
            allowed=False,
            violated_rule_id="CODEX_UNAVAILABLE",
            reason=f"Codex registry unavailable; refusing build handoff (fail-closed): {exc}",
        )


def approval_ok(
    state: WebsiteBuildState, stage: str, *, autonomous: bool,
) -> bool:
    """Is ``stage`` cleared to run by the operator-approval gate?

    Autonomous mode (operator flipped ``website_autonomous_enabled``) implies
    approval for every stage. Supervised mode requires the stage to be in the
    order's ``approved_stages`` — i.e. the operator explicitly signed it off in
    the walk-through.
    """
    if autonomous:
        return True
    return stage in state.approved_stages


def outward_ok(stage: str, *, live_publish_enabled: bool) -> bool:
    """Is an outward/irreversible ``stage`` allowed to take its real action?

    Non-outward stages are always allowed (they draft/populate an unpublished
    site). Outward stages (publish, deliver) require the explicit live flag.
    """
    if stage not in OUTWARD_STAGES:
        return True
    return bool(live_publish_enabled)
