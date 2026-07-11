"""Tests for the taste gate (gate 5) inside backend.governance.pdc_composite.

The gate must be fully inert for legacy callers (no taste_artifact) so existing
PDC behavior is byte-for-byte unchanged, and only contribute a SOFT (REVIEW)
signal when armed — never a BLOCK.
"""
from __future__ import annotations

from pathlib import Path

from backend.governance.pdc_composite import run_pdc

_AXIOMS_DIR = Path(__file__).resolve().parents[1] / "axioms"


def _benign_action() -> dict:
    return {
        "kind": "publish_website",
        "proposing_agent": "samus",
        "body": {"subject": "deliverable", "body": "ship the landing page"},
        "projected_success_outcome": "client site live",
        "projected_failure_outcome": "no publish",
    }


def _elegant_plan() -> dict:
    return {"steps": [{"kind": "publish"}], "projected_impact": 0.7, "reversibility": 1.0}


def test_no_artifact_leaves_taste_inert(tmp_path):
    finding = run_pdc(_benign_action(), plan=_elegant_plan(), confusion_events=[], sink=tmp_path)
    assert finding.taste == {"evaluated": False}
    assert finding.verdict == "PASS"
    # to_dict carries the taste section
    assert finding.to_dict()["taste"] == {"evaluated": False}


def test_clean_artifact_passes(tmp_path):
    artifact = {"document": "Build faster operations. Get started.", "kind": "proposal"}
    finding = run_pdc(
        _benign_action(), plan=_elegant_plan(), confusion_events=[], sink=tmp_path,
        taste_artifact=artifact, taste_gate=True,
    )
    assert finding.taste["evaluated"] is True
    assert finding.taste["passed"] is True
    assert finding.verdict == "PASS"


def test_slop_artifact_trips_review_not_block(tmp_path):
    # em-dash = hard taste fail; gate folds it in as REVIEW (never BLOCK).
    artifact = {"document": "Our craft — refined — ships fast.", "kind": "proposal"}
    finding = run_pdc(
        _benign_action(), plan=_elegant_plan(), confusion_events=[], sink=tmp_path,
        taste_artifact=artifact, taste_gate=True,
    )
    assert finding.taste["passed"] is False
    assert finding.verdict == "REVIEW"
    assert any("taste audit failed" in line for line in finding.rationale)


def test_gate_off_records_but_does_not_change_verdict(tmp_path):
    artifact = {"document": "Our craft — refined — ships fast."}
    finding = run_pdc(
        _benign_action(), plan=_elegant_plan(), confusion_events=[], sink=tmp_path,
        taste_artifact=artifact, taste_gate=False,
    )
    # audit still recorded for observability...
    assert finding.taste["evaluated"] is True
    assert finding.taste["passed"] is False
    # ...but with the gate off the verdict is untouched.
    assert finding.verdict == "PASS"


def test_low_score_passing_artifact_trips_review_floor(tmp_path):
    # a passing (warn-only) artifact whose score dips below the floor -> REVIEW
    artifact = {"document": "Quietly trusted by teams. h-screen hero. Get in touch. Contact us."}
    finding = run_pdc(
        _benign_action(), plan=_elegant_plan(), confusion_events=[], sink=tmp_path,
        taste_artifact=artifact, taste_gate=True, taste_review_floor=0.95,
    )
    assert finding.taste["passed"] is True
    assert finding.verdict == "REVIEW"
    assert any("taste below review floor" in line for line in finding.rationale)
