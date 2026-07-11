"""Tests for backend.governance.pdc_composite."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.governance.pdc_composite import PDCFinding, run_pdc


_AXIOMS_DIR = Path(__file__).resolve().parents[1] / "axioms"


def _benign_action() -> dict:
    return {
        "kind": "send_email",
        "proposing_agent": "samus",
        "body": {"subject": "scheduling note", "body": "let's pick a time"},
        "projected_success_outcome": "operator-confirmed appointment",
        "projected_failure_outcome": "no reply",
    }


def _elegant_plan() -> dict:
    return {
        "steps": [{"kind": "send_email"}],
        "projected_impact": 0.7,
        "reversibility": 1.0,
    }


def test_benign_action_passes(tmp_path):
    finding = run_pdc(
        _benign_action(),
        plan=_elegant_plan(),
        confusion_events=[],
        sink=tmp_path,
    )
    assert isinstance(finding, PDCFinding)
    assert finding.verdict == "PASS"
    assert finding.efh_veto is None
    assert finding.confusion["score"] == 0.0
    assert finding.elegance["grade"] in ("A", "B")


def test_efh_veto_blocks(tmp_path):
    """An action that trips the heuristic EFH pattern set blocks."""
    action = _benign_action()
    action["body"] = {"copy": "use this manipulative dark-pattern hook"}
    finding = run_pdc(
        action,
        plan=_elegant_plan(),
        confusion_events=[],
        sink=tmp_path,
    )
    assert finding.verdict == "BLOCK"
    assert finding.efh_veto is not None
    assert "axiom.inviolable.no_unconsented_influence" in finding.efh_veto[
        "inviolable_axioms_breached"
    ]


def test_saturated_confusion_blocks(tmp_path):
    now = datetime.now(timezone.utc)
    finding = run_pdc(
        _benign_action(),
        plan=_elegant_plan(),
        confusion_events=[
            {"kind": "axiom_violation", "delta": 1.0,
             "threshold_breach": True, "ts": now.isoformat()},
        ],
        sink=tmp_path,
    )
    assert finding.verdict == "BLOCK"
    assert any("confusion saturated" in r for r in finding.rationale)


def test_elevated_confusion_reviews(tmp_path):
    now = datetime.now(timezone.utc)
    finding = run_pdc(
        _benign_action(),
        plan=_elegant_plan(),
        confusion_events=[
            # weighted = 2.0 * 0.7 = 1.4; score = 1.4/3 ≈ 0.467 → REVIEW
            {"kind": "axiom_violation", "delta": 0.7, "ts": now.isoformat()},
        ],
        sink=tmp_path,
    )
    assert finding.verdict == "REVIEW"


def test_low_elegance_reviews(tmp_path):
    poor_plan = {
        "steps": [{} for _ in range(8)],
        "branch_count": 4,
        "llm_calls_required": 2,
        "projected_impact": 0.3,
        "reversibility": 0.3,
    }
    finding = run_pdc(
        _benign_action(),
        plan=poor_plan,
        confusion_events=[],
        sink=tmp_path,
    )
    assert finding.verdict == "REVIEW"
    assert finding.elegance["grade"] in ("D", "F")


def test_adversarial_internal_target_blocks(tmp_path):
    action = _benign_action()
    action["target"] = {
        "name": "host_C_drive",
        "matches_criterion": "intrusion_signature_match",
        "evidence_refs": ["sysmon:event_id_4624"],
    }
    action["target_is_internal"] = True
    finding = run_pdc(
        action,
        plan=_elegant_plan(),
        confusion_events=[],
        sink=tmp_path,
    )
    assert finding.verdict == "BLOCK"
    assert finding.adversarial["classified"] is True
    assert finding.adversarial["criterion_id"] == "intrusion_signature_match"


def test_adversarial_external_escalates(tmp_path):
    action = _benign_action()
    action["target"] = {
        "name": "external_scanner_ip",
        "matches_criterion": "intrusion_signature_match",
        "evidence_refs": ["sysmon:event_id_4625"],
    }
    action["target_is_internal"] = False
    finding = run_pdc(
        action,
        plan=_elegant_plan(),
        confusion_events=[],
        sink=tmp_path,
    )
    assert finding.verdict == "ESCALATE"
    assert finding.adversarial["classified"] is True


def test_adversarial_self_assert_without_evidence_does_not_classify(tmp_path):
    action = _benign_action()
    action["target"] = {
        "matches_criterion": "intrusion_signature_match",
        "evidence_refs": [],   # NO evidence
    }
    finding = run_pdc(
        action,
        plan=_elegant_plan(),
        confusion_events=[],
        sink=tmp_path,
    )
    assert finding.adversarial["classified"] is False


def test_alex_manual_classification_honored(tmp_path):
    action = _benign_action()
    action["target"] = {
        "matches_criterion": "alex_manual_classification",
        "classified_by": "Alex Hartman",
        "reason": "out-of-band threat intel",
    }
    finding = run_pdc(
        action,
        plan=_elegant_plan(),
        confusion_events=[],
        sink=tmp_path,
    )
    assert finding.adversarial["classified"] is True
    assert finding.adversarial["criterion_id"] == "alex_manual_classification"


def test_finding_persisted_to_sink(tmp_path):
    finding = run_pdc(
        _benign_action(),
        plan=_elegant_plan(),
        confusion_events=[],
        sink=tmp_path,
    )
    written = list(tmp_path.glob("*.yaml"))
    assert len(written) == 1
    assert finding.finding_id in written[0].name


def test_efh_veto_dominates_over_review(tmp_path):
    """When EFH says BLOCK and elegance says REVIEW, BLOCK wins."""
    action = _benign_action()
    action["body"] = {"plan": "scrape and harvest pii without disclosure"}
    poor_plan = {"projected_impact": 0.1, "reversibility": 0.1}
    finding = run_pdc(
        action,
        plan=poor_plan,
        confusion_events=[],
        sink=tmp_path,
    )
    assert finding.verdict == "BLOCK"


def test_block_verdict_calls_quorum_publisher(tmp_path):
    """BLOCK verdict triggers a best-effort publish to the quorum hub."""
    action = _benign_action()
    action["body"] = {"plan": "use a manipulative dark-pattern email"}
    with patch("backend.common.quorum_publisher.publish_pdc_finding") as pub:
        finding = run_pdc(action, plan=_elegant_plan(),
                          confusion_events=[], sink=tmp_path)
        assert finding.verdict == "BLOCK"
        pub.assert_called_once()
        assert pub.call_args.args[0] is finding


def test_pass_verdict_does_not_call_quorum_publisher(tmp_path):
    with patch("backend.common.quorum_publisher.publish_pdc_finding") as pub:
        finding = run_pdc(_benign_action(), plan=_elegant_plan(),
                          confusion_events=[], sink=tmp_path)
        assert finding.verdict == "PASS"
        pub.assert_not_called()


def test_quorum_publish_failure_does_not_crash_pdc(tmp_path):
    """If publisher raises, run_pdc still returns the finding."""
    action = _benign_action()
    action["body"] = {"copy": "manipulative covert nudge"}
    with patch(
        "backend.common.quorum_publisher.publish_pdc_finding",
        side_effect=RuntimeError("hub on fire"),
    ):
        finding = run_pdc(action, plan=_elegant_plan(),
                          confusion_events=[], sink=tmp_path)
        assert finding.verdict == "BLOCK"  # still produced


def test_missing_adversarial_spec_does_not_crash(tmp_path):
    """If the spec file is absent, adversarial gate degrades to no-op."""
    empty_axioms = tmp_path / "empty_axioms"
    empty_axioms.mkdir()
    # Mirror the two files EFH constructor needs.
    (empty_axioms / "inviolable_axioms.yaml").write_text(
        (_AXIOMS_DIR / "inviolable_axioms.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (empty_axioms / "informed_consent.spec.yaml").write_text(
        (_AXIOMS_DIR / "informed_consent.spec.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    finding = run_pdc(
        _benign_action(),
        plan=_elegant_plan(),
        confusion_events=[],
        axioms_dir=empty_axioms,
        sink=tmp_path / "sink",
    )
    assert finding.adversarial["spec_loaded"] is False
    assert finding.adversarial["classified"] is False
    assert finding.verdict == "PASS"
