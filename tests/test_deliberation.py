"""Deliberation router — value-of-computation depth decision (backend/common/deliberation.py)."""
from __future__ import annotations

from types import SimpleNamespace

from backend.common import deliberation as d


# --- pure decide_depth ------------------------------------------------------


def test_low_value_is_fast():
    depth, score, _ = d.decide_depth(value=0.1, uncertainty=0.5)
    assert depth == d.FAST
    assert score < d._T_FAST


def test_high_value_high_uncertainty_is_debate():
    depth, score, _ = d.decide_depth(value=1.0, uncertainty=1.0, reversibility=1.0)
    assert depth == d.DEBATE


def test_high_stakes_irreversible_uncertain_escalates():
    depth, _, rationale = d.decide_depth(
        value=0.9, uncertainty=0.6, reversibility=0.1,
    )
    assert depth == d.ESCALATE
    assert "escalate" in rationale


def test_urgency_caps_slow_paths():
    # Would be DEBATE, but hard urgency caps it to STANDARD.
    depth, _, rationale = d.decide_depth(
        value=1.0, uncertainty=1.0, reversibility=1.0, urgency=0.9,
    )
    assert depth == d.STANDARD
    assert "urgency" in rationale


def test_urgency_soft_cap_to_deep():
    depth, _, _ = d.decide_depth(
        value=1.0, uncertainty=1.0, reversibility=1.0, urgency=0.65,
    )
    assert depth == d.DEEP


def test_inputs_are_clamped():
    depth, _, _ = d.decide_depth(value=5.0, uncertainty=-1.0)  # clamp to 1.0 / 0.0
    # value 1.0, uncertainty 0.0 -> score = 1*0.35*1 = 0.35 -> STANDARD
    assert depth == d.STANDARD


# --- depth_to_max_tokens ----------------------------------------------------


def test_depth_token_ladder_is_monotonic():
    base = 4000
    toks = [d.depth_to_max_tokens(x, base) for x in (d.FAST, d.STANDARD, d.DEEP, d.DEBATE)]
    assert toks == sorted(toks)
    assert d.depth_to_max_tokens(d.ESCALATE, base) == 0  # no compute; human path


# --- affordability cap ------------------------------------------------------


class _FakeStore:
    """can_spend allows only requests <= max_ok tokens."""

    def __init__(self, max_ok: int) -> None:
        self.max_ok = max_ok

    def can_spend(self, workcell, est_tokens):
        return SimpleNamespace(allowed=(est_tokens <= self.max_ok))


def test_affordable_depth_downgrades_when_broke(monkeypatch):
    import backend.common.llm_budget as budget
    # Only ~FAST (0.25*4000=1000) is affordable.
    monkeypatch.setattr(budget, "get_store", lambda: _FakeStore(max_ok=1000))
    assert d.affordable_depth("prospecting", 4000) == d.FAST


def test_affordable_depth_no_store_does_not_cap(monkeypatch):
    import backend.common.llm_budget as budget

    def _boom():
        raise RuntimeError("no store")

    monkeypatch.setattr(budget, "get_store", _boom)
    assert d.affordable_depth("prospecting", 4000) == d.DEBATE  # fail-open, no cap


def test_deliberate_applies_budget_cap(monkeypatch):
    import backend.common.llm_budget as budget
    monkeypatch.setattr(budget, "get_store", lambda: _FakeStore(max_ok=1000))
    # Would be DEBATE by VOC, but budget only affords FAST.
    decision = d.deliberate(
        value=1.0, uncertainty=1.0, reversibility=1.0,
        workcell="prospecting", base_tokens=4000,
    )
    assert decision.depth == d.FAST
    assert "budget caps" in decision.rationale


def test_deliberate_escalate_ignores_budget(monkeypatch):
    import backend.common.llm_budget as budget
    monkeypatch.setattr(budget, "get_store", lambda: _FakeStore(max_ok=1))
    decision = d.deliberate(
        value=0.9, uncertainty=0.6, reversibility=0.1,
        workcell="prospecting", base_tokens=4000,
    )
    assert decision.depth == d.ESCALATE  # a human hand-off is not a budget outcome
    assert decision.escalate is True
    assert decision.max_tokens == 0


# --- route ------------------------------------------------------------------


def test_deliberate_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.common import deliberation_routes

    # The route module binds check_capability at import; patch it there.
    monkeypatch.setattr(deliberation_routes, "check_capability", lambda *a, **k: None)

    app = FastAPI()
    deliberation_routes.register_routes(app)
    client = TestClient(app)

    r = client.post("/admin/deliberate", json={"value": 0.1, "uncertainty": 0.5})
    assert r.status_code == 200, r.text
    assert r.json()["depth"] == d.FAST

    # Missing 'value' -> 400.
    assert client.post("/admin/deliberate", json={}).status_code == 400
