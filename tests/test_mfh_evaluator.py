"""MFH (Meaning Failure Handler) — advisory meaning-anchor evaluator (HOTL T5).

MFH is the advisory sibling of EFH: it FLAGS meaning-anchor concerns (never
vetoes) and its assessment rides on the cash_engine review's decision.made
event.
"""

from __future__ import annotations


def test_mfh_clean_action_not_flagged():
    from backend.governance.mfh_evaluator import evaluate_meaning

    out = evaluate_meaning(
        {
            "kind": "cash_engine_review",
            "stake_sentence": "We will rebuild Acme's booking flow so they can take "
            "reservations without us after launch.",
        }
    )
    assert out["flagged"] is False
    assert out["anchors"] == []


def test_mfh_flags_treadmill_language():
    from backend.governance.mfh_evaluator import evaluate_meaning

    out = evaluate_meaning(
        {
            "kind": "cash_engine_review",
            "stake_sentence": "A recurring fee to keep the lights on; the site only "
            "works while we maintain it and it's hard to cancel.",
        }
    )
    assert out["flagged"] is True
    # Multiple anchors should trip on this dense treadmill+lock-in sentence.
    assert "axiom.meaning.capability_over_dependency" in out["anchors"]
    assert "axiom.meaning.reversibility_for_recipient" in out["anchors"]
    assert out["notes"]


def test_mfh_flags_vanity_metric():
    from backend.governance.mfh_evaluator import evaluate_meaning

    out = evaluate_meaning(
        {
            "body": {"plan": "Inflate the metric so it looks good on paper."},
        }
    )
    assert out["flagged"] is True
    assert "axiom.meaning.externality_reality" in out["anchors"]


def test_mfh_never_raises_on_garbage():
    from backend.governance.mfh_evaluator import MeaningFailureHandler

    h = MeaningFailureHandler()
    # A non-dict-ish nested structure still flattens without raising.
    out = h.evaluate({"a": [1, 2, {"b": None}], "c": 3.5})
    assert out["flagged"] is False


def test_mfh_only_flags_registered_anchors(tmp_path):
    """A pattern for an anchor absent from the signed registry must not flag."""
    import yaml
    from backend.governance.mfh_evaluator import MeaningFailureHandler

    # Registry with only ONE of the five anchors present.
    (tmp_path / "meaning_anchors.yaml").write_text(
        yaml.safe_dump(
            {
                "meaning_anchors": [
                    {"id": "axiom.meaning.reversibility_for_recipient"},
                ],
            }
        ),
        encoding="utf-8",
    )
    h = MeaningFailureHandler(axioms_dir=tmp_path)
    # Treadmill (capability) language would normally flag, but that anchor is
    # not in this registry — only the reversibility hit should survive.
    out = h.evaluate({"t": "treadmill dependency; sunk-cost lock keeps them in"})
    assert out["flagged"] is True
    assert out["anchors"] == ["axiom.meaning.reversibility_for_recipient"]


# ---------------------------------------------------------------------------
# integration: cash_engine gate emits the MFH advisory as a decision.made event
# ---------------------------------------------------------------------------


def test_cash_engine_gate_emits_meaning_advisory_event(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SAMUS_CASH_ENGINE_ENABLED", "true")

    from backend.cash_engine.gate import evaluate_gate
    from backend.cash_engine.models import RevenueTriggerRequest

    class _Opp:
        opportunity_id = "opp_1"
        stake_sentence = "Lock them in with switching costs so they can't leave."

    class _Crm:
        @staticmethod
        def get_opportunity_for_prospect(pid):
            return _Opp()

    out = evaluate_gate(
        RevenueTriggerRequest(prospect_id="pr_1", trigger_source="manual_review"),
        crm=_Crm(),
    )
    # The gate may block on the codex/stake path — irrelevant here; we only
    # assert the advisory event fired regardless of the downstream verdict.
    assert out is not None

    from backend.common.business_events import DECISION_MADE, read_events

    evs = read_events(opportunity_id="opp_1", event_types=[DECISION_MADE])
    adv = [e for e in evs if (e.get("metadata") or {}).get("decision") == "meaning_advisory"]
    assert len(adv) == 1
    assert adv[0]["metadata"]["meaning_flagged"] is True
    assert "axiom.meaning.reversibility_for_recipient" in adv[0]["metadata"]["meaning_anchors"]
