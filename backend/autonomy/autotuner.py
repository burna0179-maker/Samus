"""``Autotuner`` — META-REFLECTION runtime self-tuning (Contract 2).

Observes each cycle's latency / error / compliance signals and maintains a
small set of bounded internal tuning knobs (advisory only — nothing here is
wired to a live effector in this phase). Flag-gated + fail-safe.

Contract 2 method:
    ``adjust(latency=, errors=, compliance_blocked=) -> None``

Flag gating behind ``settings.autonomy_autotuner_enabled`` (default OFF).
When OFF, ``adjust`` is a pure no-op (no state mutation, no persistence) and
``snapshot`` returns ``{}`` — byte-identical to "autotuner absent".

DORMANCY: constructed no-arg; no live importer; inert unless armed AND the
(dormant) wrapper constructs it. The knobs are observational; promoting any
knob to a live runtime parameter is deferred to a later operator-gated phase.
"""

from __future__ import annotations

import json
import logging
import time as _time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Latency band (ms) the tuner nudges its advisory "patience" knob around.
_LATENCY_TARGET_MS = 1500.0


def _flag_enabled() -> bool:
    try:
        from backend.common.config import get_settings

        return bool(getattr(get_settings(), "autonomy_autotuner_enabled", False))
    except Exception:  # pragma: no cover
        return False


def _state_path():
    from backend.common.state_paths import state_path

    return state_path("autonomy", "autotuner_state.json")


class Autotuner:
    """Bounded advisory self-tuner. Flag-gated, fail-safe, no live effector."""

    def __init__(self, *, enabled: Optional[bool] = None) -> None:
        self._enabled = _flag_enabled() if enabled is None else bool(enabled)
        # Advisory knobs — bounded EMAs. Observational only in this phase.
        self._samples = 0
        self._latency_ema_ms = 0.0
        self._error_rate_ema = 0.0
        self._block_rate_ema = 0.0
        if self._enabled:
            self._load()

    def adjust(
        self,
        *,
        latency: float = 0.0,
        errors: Any = None,
        compliance_blocked: bool = False,
    ) -> None:
        """Fold one cycle's signals into the advisory knobs.

        ``errors`` may be a list (the CycleResult.errors shape) or a count.
        Disabled => no-op. Never raises.
        """
        if not self._enabled:
            return
        try:
            err_count = len(errors) if hasattr(errors, "__len__") else int(errors or 0)
            alpha = 0.1
            self._samples += 1
            self._latency_ema_ms = (1 - alpha) * self._latency_ema_ms + alpha * float(
                latency or 0.0
            )
            self._error_rate_ema = (1 - alpha) * self._error_rate_ema + alpha * (
                1.0 if err_count else 0.0
            )
            self._block_rate_ema = (1 - alpha) * self._block_rate_ema + alpha * (
                1.0 if compliance_blocked else 0.0
            )
            self._save()
        except Exception:  # pragma: no cover — tuning must never break a cycle
            log.exception("Autotuner.adjust failed")

    def snapshot(self) -> Dict[str, Any]:
        """Return the advisory knob state. Empty when disabled."""
        if not self._enabled:
            return {}
        return {
            "samples": self._samples,
            "latency_ema_ms": round(self._latency_ema_ms, 3),
            "error_rate_ema": round(self._error_rate_ema, 4),
            "block_rate_ema": round(self._block_rate_ema, 4),
            "latency_target_ms": _LATENCY_TARGET_MS,
        }

    # ---- persistence (fail-open) --------------------------------------
    def _load(self) -> None:
        try:
            path = _state_path()
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._samples = int(raw.get("samples", 0))
                self._latency_ema_ms = float(raw.get("latency_ema_ms", 0.0))
                self._error_rate_ema = float(raw.get("error_rate_ema", 0.0))
                self._block_rate_ema = float(raw.get("block_rate_ema", 0.0))
        except Exception:  # pragma: no cover
            log.exception("Autotuner load failed; starting fresh")

    def _save(self) -> None:
        try:
            path = _state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": "samus.autonomy.autotuner_state/1",
                "ts": _time.time(),
                "samples": self._samples,
                "latency_ema_ms": self._latency_ema_ms,
                "error_rate_ema": self._error_rate_ema,
                "block_rate_ema": self._block_rate_ema,
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except Exception:  # pragma: no cover
            log.exception("Autotuner save failed")


__all__ = ["Autotuner"]
