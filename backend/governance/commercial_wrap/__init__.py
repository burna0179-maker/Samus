"""Commercial action wrapping + RBL band consumer for Samus.

Single entry point: ``commit_commercial_action`` ensures EFH-first ordering,
RBL band gating, ISV scope check, template OR EFH evaluation, dual-channel
audit envelope, then emits the ValueExchangeRecord.
"""
from .wrap import (
    CommercialActionRefusal,
    RblBandConsumer,
    commit_commercial_action,
    ingest_rbl_band,
    COMMERCIAL_ACTION_CLASSES,
    REQUIRED_METADATA,
)

__all__ = [
    "CommercialActionRefusal",
    "RblBandConsumer",
    "commit_commercial_action",
    "ingest_rbl_band",
    "COMMERCIAL_ACTION_CLASSES",
    "REQUIRED_METADATA",
]
