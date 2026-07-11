"""Cognitive workcell — Stage-1 persona-frame derivation (compute-only).

This package hosts the ported frontier-grade cognitive stack stranded in
``recovery/``. Stage 1 (``persona_frame.py``) derives an operating-mode frame
from the emotional/self-model state exposed by
``backend/memory/persona_self_model.py``.

COMPUTE-ONLY + wire-dormant: nothing here is attached to any live
send/chat/outreach/cash_engine path. Construction and use are gated behind
``settings.persona_frame_enabled`` (default OFF); attaching a frame to real
behaviour is explicitly deferred to an operator decision.
"""

from .persona_frame import (
    ModeThresholds,
    OperatingMode,
    PersonaFrame,
    PersonaSystem,
)

__all__ = [
    "ModeThresholds",
    "OperatingMode",
    "PersonaFrame",
    "PersonaSystem",
]
