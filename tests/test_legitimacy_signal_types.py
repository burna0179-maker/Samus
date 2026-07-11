"""Unit tests for the LegitimacySignal pydantic types + has_warmth helper."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.prospecting.legitimacy import (
    LegitimacyAssessment,
    LegitimacySignal,
    has_warmth,
)


def _sig(kind: str = "public_registry", confidence: str = "high") -> LegitimacySignal:
    return LegitimacySignal(
        kind=kind,  # type: ignore[arg-type]
        source="test://x",
        discovered_at=datetime.now(timezone.utc),
        evidence={"k": "v"},
        confidence=confidence,  # type: ignore[arg-type]
    )


def test_has_warmth_false_on_empty_list():
    assert has_warmth([]) is False


def test_has_warmth_true_when_any_signal():
    assert has_warmth([_sig()]) is True


def test_signal_rejects_low_confidence():
    with pytest.raises(ValidationError):
        LegitimacySignal(
            kind="rfp",
            source="x",
            discovered_at=datetime.now(timezone.utc),
            evidence={},
            confidence="low",  # type: ignore[arg-type]
        )


def test_signal_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        LegitimacySignal(
            kind="hunch",  # type: ignore[arg-type]
            source="x",
            discovered_at=datetime.now(timezone.utc),
            evidence={},
            confidence="high",
        )


def test_signal_requires_nonempty_source():
    with pytest.raises(ValidationError):
        LegitimacySignal(
            kind="rfp",
            source="",
            discovered_at=datetime.now(timezone.utc),
            evidence={},
            confidence="high",
        )


def test_assessment_has_warmth_mirrors_signals():
    a = LegitimacyAssessment(
        prospect_id="p1",
        signals=[_sig()],
        has_warmth=True,
        assessed_at=datetime.now(timezone.utc),
    )
    assert a.has_warmth is True
    assert len(a.signals) == 1
