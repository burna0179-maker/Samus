"""Strategic Compression Engine — guidance_laws.jsonl rollup (Concept 2).

Concept 2 of Samus_Assimilation_Plan_Institutional_Cognition_2026-07-06.md
turns the deterministic-lessons output of ``consolidator.distill`` into
structured, queryable business-law records — "wisdom over knowledge" —
via ``experiments.promoter.emit_guidance_law``. These tests cover:

  * GuidanceLaw dataclass roundtrip through the JSONL ledger.
  * distill() emits laws when there are lessons.
  * distill() emits ZERO laws when the day is empty (no placeholder row).
  * Ledger read helper survives missing / malformed rows.
"""

from __future__ import annotations

import json
from datetime import date

import pytest


TODAY = date.today().isoformat()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("SAMUS_ATTRIBUTION_PATH", str(tmp_path / "attr.json"))
    monkeypatch.setenv("SAMUS_CONVERSION_FUNNEL_PATH", str(tmp_path / "funnel.jsonl"))
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(tmp_path / "rewards.jsonl"))
    monkeypatch.setenv("SAMUS_FEEDBACK_STORE_PATH", str(tmp_path / "metrics.json"))
    monkeypatch.setenv("SAMUS_CONSOLIDATION_LLM_ENABLED", "0")
    monkeypatch.delenv("DDB_ATTRIBUTION_TABLE", raising=False)
    from backend.attribution import store as attr_store

    attr_store.reset_store()
    yield
    attr_store.reset_store()


def _seed_funnel(*, leads=0, opportunities=0, closed_won=0, industry="plumbing"):
    from backend.common.conversion_funnel import record_stage

    for i in range(leads):
        record_stage("lead", entity_id=f"l{i}", industry=industry)
    for i in range(opportunities):
        record_stage("opportunity", entity_id=f"o{i}", industry=industry)
    for i in range(closed_won):
        record_stage("closed_won", entity_id=f"w{i}", industry=industry)


def _seed_rewards(tmp_path, count=3, *, paid=1, day=TODAY):
    rows = []
    for i in range(count):
        rows.append(
            {
                "opportunity_id": f"opp-{i}",
                "reward": 4.0,
                "components": {"terminal_paid": 1.0 if i < paid else 0.0},
                "computed_at": f"{day}T10:0{i}:00+00:00",
                "correlation_id": "",
            }
        )
    path = tmp_path / "rewards.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# GuidanceLaw dataclass + JSONL roundtrip
# ---------------------------------------------------------------------------
class TestGuidanceLawRoundtrip:
    def test_emit_then_read_preserves_fields(self):
        from backend.experiments.promoter import (
            GuidanceLaw,
            emit_guidance_law,
            read_guidance_laws,
        )

        law = GuidanceLaw(
            law_id="law-2026-07-06-00",
            law="pattern: 'dental' industry produced 6/10 closed_won deals",
            evidence_count=10,
            confidence=0.5833,
            promoted_at="2026-07-06T00:00:00Z",
            source_pattern_ids=["rec-abc123", "distilled-2026-07-06"],
            category="revenue_acceleration",
        )
        assert emit_guidance_law(law) is True

        laws = read_guidance_laws()
        assert len(laws) == 1
        got = laws[0]
        assert got.law_id == "law-2026-07-06-00"
        assert got.law.startswith("pattern: 'dental'")
        assert got.evidence_count == 10
        assert abs(got.confidence - 0.5833) < 1e-9
        assert got.promoted_at == "2026-07-06T00:00:00Z"
        assert got.source_pattern_ids == ["rec-abc123", "distilled-2026-07-06"]
        assert got.category == "revenue_acceleration"

    def test_multiple_emits_append_in_order(self):
        from backend.experiments.promoter import (
            GuidanceLaw,
            emit_guidance_law,
            read_guidance_laws,
        )

        for i in range(3):
            emit_guidance_law(
                GuidanceLaw(
                    law_id=f"law-x-{i}",
                    law=f"pattern {i}",
                    evidence_count=i + 1,
                    confidence=0.5,
                    promoted_at="2026-07-06T00:00:00Z",
                )
            )
        laws = read_guidance_laws()
        assert [law.law_id for law in laws] == ["law-x-0", "law-x-1", "law-x-2"]

    def test_read_survives_missing_file(self):
        from backend.experiments.promoter import read_guidance_laws

        assert read_guidance_laws() == []

    def test_read_skips_malformed_rows(self, tmp_path, monkeypatch):
        # Write a file with one valid + one garbage row and confirm the reader
        # returns the valid row only (fail-open, never raises).
        from backend.experiments.promoter import (
            _GUIDANCE_LAWS_JSONL,
            read_guidance_laws,
        )
        from backend.common.state_paths import state_path

        path = state_path(*_GUIDANCE_LAWS_JSONL)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "law_id": "law-good",
                        "law": "keep me",
                        "evidence_count": 5,
                        "confidence": 0.6,
                        "promoted_at": "2026-07-06T00:00:00Z",
                        "source_pattern_ids": ["a"],
                        "category": "ops",
                    }
                )
                + "\n"
            )
            fh.write("not-json\n")
        laws = read_guidance_laws()
        assert len(laws) == 1 and laws[0].law_id == "law-good"

    def test_business_event_emitted(self, monkeypatch):
        # emit_guidance_law fires a ``law.promoted`` business event so the
        # unified stream sees the same "promoted" moment as the promoter.
        from backend.experiments import promoter
        from backend.experiments.promoter import GuidanceLaw, emit_guidance_law

        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            promoter,
            "emit_business_event",
            lambda et, **kw: events.append((et, kw)) or {},
        )
        emit_guidance_law(
            GuidanceLaw(
                law_id="law-evt",
                law="pattern: event",
                evidence_count=1,
                confidence=0.5,
                promoted_at="2026-07-06T00:00:00Z",
            )
        )
        assert events and events[0][0] == "law.promoted"
        assert events[0][1]["workcell"] == "experiments"
        assert events[0][1]["metadata"]["law_id"] == "law-evt"


# ---------------------------------------------------------------------------
# distill() rollup hook
# ---------------------------------------------------------------------------
class TestDistillRollup:
    def test_distill_emits_laws_when_lessons_exist(self, tmp_path):
        from backend.cognitive import consolidator
        from backend.experiments.promoter import read_guidance_laws

        _seed_funnel(leads=10, opportunities=6, closed_won=3)
        _seed_rewards(tmp_path)
        out = consolidator.distill(TODAY)
        assert out["lessons"] >= 1
        assert out["laws_emitted"] == out["lessons"]

        laws = read_guidance_laws()
        assert len(laws) == out["lessons"]
        # Every emitted law carries a law-id keyed on the distill day and a
        # source_pattern_ids link back to the guidance record.
        for law in laws:
            assert law.law_id.startswith(f"law-{TODAY}-")
            assert law.law  # non-empty text
            assert law.evidence_count >= 1
            assert 0.0 <= law.confidence <= 1.0
            assert law.promoted_at
            assert any(pid.startswith("distilled-") for pid in law.source_pattern_ids)

    def test_empty_day_emits_no_laws(self):
        # The pre-existing empty-day contract of distill() must hold — no
        # lessons means no rollup rows at all (no placeholder entry).
        from backend.cognitive import consolidator
        from backend.experiments.promoter import read_guidance_laws

        out = consolidator.distill(TODAY)
        # Contract: empty-day return is exactly {"lessons": 0, "ingested": 0}.
        # If this ever changes, the guidance-laws hook is the FIRST suspect.
        assert out == {"lessons": 0, "ingested": 0}
        assert read_guidance_laws() == []

    def test_evidence_count_parses_fraction_from_recommendation(self):
        # A lesson like "6/10 closed_won" should surface evidence_count=10
        # and a Laplace-smoothed confidence — matches belief_ledger's model.
        from backend.cognitive.consolidator import _evidence_and_confidence

        lesson = {
            "recommendation": ("pattern: 'dental' industry produced 6/10 closed_won deals"),
            "rationale": "provenance: conversion_funnel ledger",
            "expected_impact": "high",
        }
        ev, conf = _evidence_and_confidence(lesson, lesson["rationale"])
        assert ev == 10
        # (6+1)/(10+2) = 0.5833...
        assert abs(conf - 0.5833) < 1e-3

    def test_evidence_count_falls_back_to_impact_floor(self):
        from backend.cognitive.consolidator import _evidence_and_confidence

        lesson = {
            "recommendation": "pattern: qualitative note",
            "rationale": "provenance: none",
            "expected_impact": "medium",
        }
        ev, conf = _evidence_and_confidence(lesson, lesson["rationale"])
        assert ev == 1
        assert conf == 0.5
