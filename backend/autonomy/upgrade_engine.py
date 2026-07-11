"""``UpgradeEngine`` — META-REFLECTION upgrade *proposer* (Contract 2).

Evaluates a completed cycle for signals that the architecture should evolve
(persistent latency, repeated errors, repeated compliance blocks) and, when
warranted, **writes a PROPOSAL record** to a JSONL ledger. It is propose-only
by hard design:

    !!! execute() NEVER self-modifies code, config, or any live behaviour. !!!
    It appends one structured proposal record for a human/governance review.

This is the load-bearing safety property of the whole autonomy layer
(build-plan: "autonomy PROPOSES, gates DISPOSE"): the upgrade path can only
ever produce a reviewable artifact, never a mutation.

Contract 2 methods:
    ``evaluate(result=, situation=) -> bool``   (True => a proposal is warranted)
    ``execute() -> None``                       (append ONE proposal record)

Flag gating behind ``settings.autonomy_upgrade_enabled`` (default OFF). When
OFF, ``evaluate`` always returns ``False`` and ``execute`` is a no-op (no disk
write) — byte-identical to "engine absent".

DORMANCY: constructed no-arg; no live importer; inert unless armed AND the
(dormant) wrapper constructs it.
"""

from __future__ import annotations

import json
import logging
import time as _time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Thresholds at which a proposal is warranted (conservative — a proposal is a
# review artifact, but we still don't want to spam the ledger every cycle).
_LATENCY_PROPOSE_MS = 5000.0
_ERROR_COUNT_PROPOSE = 2


def _flag_enabled() -> bool:
    try:
        from backend.common.config import get_settings

        return bool(getattr(get_settings(), "autonomy_upgrade_enabled", False))
    except Exception:  # pragma: no cover
        return False


def _proposals_path():
    from backend.common.state_paths import state_path

    return state_path("autonomy", "upgrade_proposals.jsonl")


class UpgradeEngine:
    """Propose-only architecture-upgrade evaluator. Flag-gated, fail-safe.

    NEVER self-modifies — ``execute`` only appends a proposal record.
    """

    def __init__(self, *, enabled: Optional[bool] = None) -> None:
        self._enabled = _flag_enabled() if enabled is None else bool(enabled)
        # The pending proposal staged by the most recent ``evaluate`` that
        # returned True; consumed (and cleared) by ``execute``.
        self._pending: Optional[Dict[str, Any]] = None

    def evaluate(self, *, result: Any = None, situation: Dict[str, Any] | None = None) -> bool:
        """Return True iff this cycle warrants an upgrade proposal.

        Disabled => always False (and never stages a proposal). Reads the
        Contract-1 CycleResult surface (``elapsed_ms`` / ``errors`` /
        ``compliance_blocked``) duck-typed so it tolerates any result shape.
        Never raises.
        """
        if not self._enabled:
            return False
        try:
            situation = situation or {}
            elapsed = float(getattr(result, "elapsed_ms", 0.0) or 0.0)
            errors = getattr(result, "errors", []) or []
            err_count = len(errors) if hasattr(errors, "__len__") else int(errors or 0)
            compliance_blocked = bool(getattr(result, "compliance_blocked", False))

            reasons = []
            if elapsed >= _LATENCY_PROPOSE_MS:
                reasons.append(f"latency_ms>={_LATENCY_PROPOSE_MS:.0f}:{elapsed:.0f}")
            if err_count >= _ERROR_COUNT_PROPOSE:
                reasons.append(f"error_count>={_ERROR_COUNT_PROPOSE}:{err_count}")
            if compliance_blocked:
                reasons.append("compliance_blocked")

            if not reasons:
                self._pending = None
                return False

            self._pending = {
                "schema": "samus.autonomy.upgrade_proposal/1",
                "ts": _time.time(),
                "status": "proposed",  # never auto-applied
                "kind": "architecture_upgrade",
                "reasons": reasons,
                "situation": {
                    "risk": str(situation.get("risk", "")),
                    "type": str(situation.get("type", "")),
                },
                "signals": {
                    "elapsed_ms": round(elapsed, 3),
                    "error_count": err_count,
                    "compliance_blocked": compliance_blocked,
                },
                "note": "PROPOSAL ONLY — requires human/governance review; no auto-apply.",
            }
            return True
        except Exception:  # pragma: no cover — evaluation must never break a cycle
            log.exception("UpgradeEngine.evaluate failed")
            self._pending = None
            return False

    def execute(self) -> None:
        """Append the staged proposal to the JSONL ledger. NEVER self-modifies.

        Disabled, or no pending proposal => no-op. Fail-open: a write error is
        logged + swallowed. This method has NO code-path that mutates Samus's
        own code, config, or runtime behaviour — by design it can only ever
        write one review record.
        """
        if not self._enabled:
            return
        proposal = self._pending
        if not proposal:
            return
        try:
            path = _proposals_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(proposal, sort_keys=True) + "\n")
        except Exception:  # pragma: no cover — write is best-effort
            log.exception("UpgradeEngine.execute proposal write failed")
        finally:
            self._pending = None


__all__ = ["UpgradeEngine"]
