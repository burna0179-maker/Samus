"""Data classes for the Samus cognitive loop (Phase A skeleton).

Samus-local mirror of Anita's ``backend/core/workflows/cycle_models.py`` —
plain dataclasses (not Pydantic) so the loop carries zero per-stage
validation overhead. The field set is shaped to **Contract 1** of the
autonomy-layer build plan, i.e. the exact attribute names the
``MetaCognitionEngine`` wrapper (``recovery/meta_cognition_engine.py``)
reads off ``inp`` and ``result``:

* ``CycleInput`` exposes ``system_prompt`` (MUTABLE — the wrapper does
  ``inp.system_prompt += bias``), ``user_text``, ``channel``,
  ``plan_token`` and ``metadata``.
* ``CycleResult`` exposes ``elapsed_ms``, ``errors``, ``compliance_blocked``
  and ``plan_token`` (the four the wrapper's META-REFLECTION consumes),
  plus ``reply`` / ``ok`` / ``stages`` / ``persona_frame`` / ``reflect_score``.

DORMANCY: this module is additive and has NO live importer. It is the
substrate the (later-phase) ``CognitiveLoop`` + ``MetaCognitionEngine``
build on; nothing in the live Samus runtime imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StageStatus(str, Enum):
    """Per-stage outcome. Mirrors Anita's StageStatus."""

    OK = "ok"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class StageResult:
    """One pipeline stage's recorded outcome.

    ``output`` is a free-form per-stage dict; ``error`` carries the
    exception summary when ``status is StageStatus.ERROR``. Fail-closed:
    a stage that raises is converted to an ERROR StageResult by the loop,
    never propagated.
    """

    name: str
    status: StageStatus
    duration_ms: float = 0.0
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 3),
            "output": self.output,
            "error": self.error,
        }


@dataclass
class CycleInput:
    """Input to ``CognitiveLoop.cycle`` (Contract 1).

    ``system_prompt`` is deliberately a plain mutable ``str`` (not
    Optional) because the ``MetaCognitionEngine`` META-PERCEPTION stage
    appends a heuristic bias to it in place
    (``inp.system_prompt += f"\\n\\nMeta-Heuristic Bias:\\n{bias}"``)
    before handing the input to the loop. ``channel`` and ``plan_token``
    carry the situation-index keys the wrapper passes to
    ``SituationIndex.classify(...)``; ``metadata`` is a free-form bag for
    callers / later phases.
    """

    user_text: str = ""
    system_prompt: str = ""
    channel: str = "chat"
    plan_token: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CycleResult:
    """Result of ``CognitiveLoop.cycle`` (Contract 1).

    The first four fields are the contract surface the
    ``MetaCognitionEngine`` META-REFLECTION reads:

    * ``elapsed_ms`` — wall time of the loop body (autotuner latency input).
    * ``errors`` — list of stage-failure summaries (reward / autotuner input).
    * ``compliance_blocked`` — True when CLASSIFY short-circuited the cycle.
    * ``plan_token`` — echoed from the input so the persistence snapshot can
      key on it.

    The remaining fields describe the cycle outcome for any caller:
    ``reply`` (the EMITted text), ``ok`` (no ERROR stage), ``stages``
    (the per-stage audit trail), ``persona_frame`` (the Stage-1 frame dict
    when persona is wired; None otherwise) and ``reflect_score``
    (REFLECT's quality score; 1.0 = converged/perfect by default).
    """

    # --- Contract-1 surface read by MetaCognitionEngine ----------------
    elapsed_ms: float = 0.0
    errors: List[str] = field(default_factory=list)
    compliance_blocked: bool = False
    plan_token: str = ""
    # --- cycle outcome -------------------------------------------------
    reply: str = ""
    ok: bool = False
    stages: List[StageResult] = field(default_factory=list)
    persona_frame: Optional[Dict[str, Any]] = None
    reflect_score: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "elapsed_ms": round(self.elapsed_ms, 3),
            "errors": list(self.errors),
            "compliance_blocked": self.compliance_blocked,
            "plan_token": self.plan_token,
            "reply": self.reply,
            "ok": self.ok,
            "reflect_score": round(self.reflect_score, 4),
            "persona_frame": self.persona_frame,
            "stages": [s.to_dict() for s in self.stages],
        }


__all__ = [
    "StageStatus",
    "StageResult",
    "CycleInput",
    "CycleResult",
]
