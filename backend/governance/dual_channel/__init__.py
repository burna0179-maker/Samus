"""Dual-channel mirror — every commercial commit produces a synchronously
acked ActionTakenEnvelope to Major via Optimus before the commercial action
lands externally.
"""
from .mirror import (
    DualChannelMirror,
    AuditBlackoutError,
    ProtocolViolation,
)

__all__ = [
    "DualChannelMirror",
    "AuditBlackoutError",
    "ProtocolViolation",
]
