"""Coverage for commercial_wrap.commit_commercial_action.

Primary focus: the PDC composite observer hookup
(``SAMUS_PDC_OBSERVE_ENABLED`` -> ``pdc_composite.run_pdc``) layered on top
of the EFH gate. The observer must:

  * stay dormant by default (flag off),
  * fire exactly once, after an EFH pass, when the flag is on,
  * never turn a passing commit into a refusal (fail-open), and
  * never be reached when EFH vetoes (the gate short-circuits first).

EFH remains the only load-bearing gate; the observer is informational.

The commit path previously carried no direct tests, so this module also
pins baseline gate behaviour (happy path, EFH veto, unknown class, missing
metadata) that the observer rides on.
"""
from __future__ import annotations

import pytest

from backend.common import config
from backend.governance import pdc_composite
from backend.governance.commercial_wrap import wrap
from backend.governance.commercial_wrap.wrap import (
    CommercialActionRefusal,
    commit_commercial_action,
)


class _PassEFH:
    """EFH evaluator stub that always passes (returns no veto)."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def evaluate(self, proposed: dict) -> None:
        self.seen.append(proposed)
        return None


class _VetoEFH:
    """EFH evaluator stub that always vetoes."""

    def evaluate(self, proposed: dict) -> dict:
        return {"veto_id": "v-test-1"}


# llm_call_above_threshold has the smallest required-metadata set.
_LLM_PAYLOAD = {"estimated_cost_usd": 0.02, "purpose": "unit-test"}


@pytest.fixture(autouse=True)
def _ver_to_tmp(tmp_path, monkeypatch):
    """Redirect the ValueExchangeRecord sink off the real state/ tree."""
    monkeypatch.setattr(wrap, "_VER_DIR", tmp_path / "ver")


def _set_observer_flag(monkeypatch, value: bool) -> None:
    """Flip samus_pdc_observe_enabled on the live cached Settings instance.

    Patching the instance attribute (rather than replacing get_settings)
    keeps the lru_cache + cache_clear contract conftest relies on intact.
    """
    settings = config.get_settings()
    monkeypatch.setattr(settings, "samus_pdc_observe_enabled", value)


def _commit(efh, **over):
    kwargs = dict(
        action_class="llm_call_above_threshold",
        action_payload=dict(_LLM_PAYLOAD),
        commercial_destination="unit-test",
        isv_consumer=None,
        template_registry=None,
        efh_evaluator=efh,
        dual_channel=None,
        rbl_consumer=None,
    )
    kwargs.update(over)
    return commit_commercial_action(**kwargs)


# --- baseline gate behaviour ------------------------------------------------

def test_happy_path_commits_with_efh_pass_attribution():
    rec = _commit(_PassEFH())
    assert rec["status"] == "committed"
    assert rec["efh_verdict_ref"] == "efh_pass:llm_call_above_threshold"


def test_efh_veto_refuses_commit():
    with pytest.raises(CommercialActionRefusal) as ei:
        _commit(_VetoEFH())
    assert "efh_veto:v-test-1" in str(ei.value)


def test_unknown_action_class_refused():
    with pytest.raises(CommercialActionRefusal):
        _commit(_PassEFH(), action_class="not_a_real_class")


def test_missing_required_metadata_refused():
    with pytest.raises(CommercialActionRefusal) as ei:
        _commit(_PassEFH(), action_payload={"purpose": "x"})
    assert "missing_required_metadata" in str(ei.value)


def test_no_efh_and_no_template_refuses():
    with pytest.raises(CommercialActionRefusal) as ei:
        _commit(None)
    assert "no_efh_evaluator_and_no_template" in str(ei.value)


# --- PDC composite observer hookup -----------------------------------------

def test_pdc_observer_dormant_by_default(monkeypatch):
    calls: list = []
    monkeypatch.setattr(pdc_composite, "run_pdc", lambda *a, **k: calls.append((a, k)))
    _set_observer_flag(monkeypatch, False)
    _commit(_PassEFH())
    assert calls == []


def test_pdc_observer_fires_once_when_enabled(monkeypatch):
    calls: list = []
    monkeypatch.setattr(pdc_composite, "run_pdc", lambda *a, **k: calls.append((a, k)))
    _set_observer_flag(monkeypatch, True)
    payload = dict(_LLM_PAYLOAD, plan={"steps": ["a", "b"]})
    _commit(_PassEFH(), action_payload=payload)

    assert len(calls) == 1
    args, kwargs = calls[0]
    proposed = args[0]
    assert proposed["proposing_agent"] == "samus"
    assert proposed["body"]["action_class"] == "llm_call_above_threshold"
    # The plan rides through to the composite for the elegance scorer.
    assert kwargs["plan"] == {"steps": ["a", "b"]}


def test_pdc_observer_is_fail_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("composite blew up")

    monkeypatch.setattr(pdc_composite, "run_pdc", _boom)
    _set_observer_flag(monkeypatch, True)
    # A failing observer must NOT turn a passing commit into a refusal.
    rec = _commit(_PassEFH())
    assert rec["status"] == "committed"


def test_pdc_observer_not_reached_on_efh_veto(monkeypatch):
    calls: list = []
    monkeypatch.setattr(pdc_composite, "run_pdc", lambda *a, **k: calls.append(1))
    _set_observer_flag(monkeypatch, True)
    with pytest.raises(CommercialActionRefusal):
        _commit(_VetoEFH())
    # The EFH veto short-circuits before the observer ever runs.
    assert calls == []
