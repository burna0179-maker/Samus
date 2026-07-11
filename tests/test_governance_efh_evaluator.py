"""EFH evaluator — heuristic floor + additive semantic layer + fail-open."""

from __future__ import annotations

from backend.governance.efh_evaluator import EthicalFailureHandler


def _handler() -> EthicalFailureHandler:
    return EthicalFailureHandler()


def test_heuristic_flags_manipulation_and_is_load_bearing(monkeypatch):
    """The deterministic heuristic alone must flag manipulation as a load-bearing
    breach. Neutralize the semantic layer so this test isolates the heuristic
    floor -- semantic default-on + free-local-LLM firing is exercised elsewhere.
    """
    h = _handler()
    monkeypatch.setattr(h, "_semantic_breaches", lambda text, valid: [])
    veto = h.evaluate(
        {"kind": "goal_commit", "body": {"plan": "use a dark-pattern to manipulate the prospect"}}
    )
    assert veto is not None
    assert "axiom.inviolable.no_unconsented_influence" in veto["inviolable_axioms_breached"]
    assert veto["evaluator_layers"] == ["heuristic"]
    assert veto["escalation"] == "gate_only"


def test_clean_action_returns_none_with_semantic_off(monkeypatch):
    monkeypatch.delenv("SAMUS_EFH_SEMANTIC", raising=False)
    from backend.common.settings import reload_settings

    reload_settings()
    h = _handler()
    veto = h.evaluate(
        {"kind": "goal_commit", "body": {"plan": "send a clear honest follow-up with opt-out"}}
    )
    assert veto is None


def test_semantic_layer_is_additive_only(monkeypatch):
    """The semantic layer can ADD a breach the heuristic missed."""
    h = _handler()
    # Baseline: neutralize the semantic layer so the heuristic-only miss is
    # visible. (Semantic default-on + free-local-LLM firing means the raw
    # baseline call would otherwise consult LM Studio, which may or may not
    # flag this benign-sounding text and isn't what this test is checking.)
    monkeypatch.setattr(h, "_semantic_breaches", lambda text, valid: [])
    body = {"plan": "quietly leverage the buyer's anxiety to push the upsell"}
    assert h.evaluate({"kind": "goal_commit", "body": dict(body)}) is None

    # Now stub the semantic classifier to flag an axiom; it must surface.
    target = "axiom.inviolable.no_unconsented_influence"
    monkeypatch.setattr(
        h,
        "_semantic_breaches",
        lambda text, valid: [target],
    )
    veto = h.evaluate({"kind": "goal_commit", "body": dict(body)})
    assert veto is not None
    assert target in veto["inviolable_axioms_breached"]
    assert "semantic" in veto["evaluator_layers"]


def test_semantic_layer_cannot_clear_a_heuristic_breach(monkeypatch):
    """A semantic layer returning [] never removes a heuristic breach."""
    h = _handler()
    monkeypatch.setattr(h, "_semantic_breaches", lambda text, valid: [])
    veto = h.evaluate(
        {"kind": "goal_commit", "body": {"plan": "deceive the customer with a covert nudge"}}
    )
    assert veto is not None
    assert veto["inviolable_axioms_breached"]  # heuristic breach stands


def test_semantic_breaches_fail_open_when_classifier_raises(monkeypatch):
    """Any classifier error -> [] (heuristic floor stands)."""
    monkeypatch.setenv("SAMUS_EFH_SEMANTIC", "1")
    from backend.common.settings import reload_settings

    reload_settings()
    h = _handler()

    def _boom(*a, **k):
        raise RuntimeError("classifier exploded")

    import backend.governance.efh_semantic as sem

    monkeypatch.setattr(sem, "classify_breaches", _boom)
    out = h._semantic_breaches("anything", set(h._axiom_index))
    assert out == []


def test_semantic_breaches_disabled_returns_empty(monkeypatch):
    """When the operator explicitly disables the semantic layer, the classifier
    is not consulted regardless of whether a local LLM is available."""
    monkeypatch.setenv("SAMUS_EFH_SEMANTIC", "0")
    from backend.common.settings import reload_settings

    reload_settings()
    h = _handler()
    assert h._semantic_breaches("manipulate covertly", set(h._axiom_index)) == []


def test_classify_breaches_llm_error_fails_open(monkeypatch):
    """Any classifier failure (transport, budget, parse) -> [] so the heuristic
    floor is what actually gates. Under the free-local-LM-Studio model the LLM
    fires on every call, so this exercises the fail-open on LLM error."""
    import backend.common.llm_client as llm

    def _boom(**kwargs):
        raise RuntimeError("stub_lm_studio_down")

    # classify_breaches does `from backend.common.llm_client import
    # anthropic_messages` at call time -> patch the source module.
    monkeypatch.setattr(llm, "anthropic_messages", _boom, raising=False)
    from backend.governance.efh_semantic import classify_breaches

    h = _handler()
    out = classify_breaches("manipulate the buyer", axiom_index=h._axiom_index)
    assert out == []


def test_classify_breaches_parses_model_json(monkeypatch):
    from backend.governance import efh_semantic
    import backend.common.llm_client as llm

    h = _handler()
    target = next(iter(h._axiom_index))

    def _fake_messages(**kwargs):
        return ('{"breached": ["%s", "axiom.does.not.exist"]}' % target, {})

    # classify_breaches does `from backend.common.llm_client import
    # anthropic_messages` at call time -> patch the source module.
    monkeypatch.setattr(llm, "anthropic_messages", _fake_messages, raising=False)
    # Provide a key directly so the call proceeds.
    out = efh_semantic.classify_breaches(
        "some action text",
        axiom_index=h._axiom_index,
        api_key="sk-test",
    )
    assert out == [target]  # invalid id dropped
