"""Aggregator — runs collectors, dedupes by kind, highest-confidence kind."""
from __future__ import annotations

from datetime import datetime, timezone

from backend.prospecting import legitimacy_check
from backend.prospecting.legitimacy import LegitimacySignal


class _Prospect:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _sig(kind="public_registry", confidence="high"):
    return LegitimacySignal(
        kind=kind, source="x",
        discovered_at=datetime.now(timezone.utc),
        evidence={}, confidence=confidence,
    )


def test_assess_warmth_returns_signals_and_flag(monkeypatch):
    monkeypatch.setattr(legitimacy_check, "lookup_ca_sos", lambda name: _sig())
    monkeypatch.setattr(legitimacy_check, "lookup_chamber_roster", lambda name, city: None)
    monkeypatch.setattr(legitimacy_check, "lookup_prior_inbound", lambda **kw: None)
    p = _Prospect(prospect_id="p1", company_name="Acme", city="Yuba City")
    a = legitimacy_check.assess_warmth(p)
    assert a.has_warmth is True
    assert len(a.signals) == 1
    assert a.prospect_id == "p1"


def test_assess_warmth_cold_when_no_collectors_hit(monkeypatch):
    monkeypatch.setattr(legitimacy_check, "lookup_ca_sos", lambda name: None)
    monkeypatch.setattr(legitimacy_check, "lookup_chamber_roster", lambda name, city: None)
    monkeypatch.setattr(legitimacy_check, "lookup_prior_inbound", lambda **kw: None)
    p = _Prospect(prospect_id="p1", company_name="Acme", city="Yuba City",
                  email="x@y.com")
    a = legitimacy_check.assess_warmth(p)
    assert a.has_warmth is False
    assert a.signals == []


def test_dedupe_by_kind(monkeypatch):
    monkeypatch.setattr(legitimacy_check, "lookup_ca_sos",
                        lambda name: _sig(kind="public_registry"))
    monkeypatch.setattr(legitimacy_check, "lookup_chamber_roster",
                        lambda name, city: _sig(kind="chamber_roster", confidence="medium"))
    monkeypatch.setattr(legitimacy_check, "lookup_prior_inbound",
                        lambda **kw: _sig(kind="public_registry"))  # duplicate kind
    p = _Prospect(company_name="Acme", city="Yuba City",
                  email="x@y.com")
    sigs = legitimacy_check.collect_signals(p)
    kinds = [s.kind for s in sigs]
    assert kinds.count("public_registry") == 1
    assert "chamber_roster" in kinds


def test_highest_confidence_kind_prefers_high():
    sigs = [_sig(kind="chamber_roster", confidence="medium"),
            _sig(kind="public_registry", confidence="high")]
    assert legitimacy_check.highest_confidence_kind(sigs) == "public_registry"


def test_highest_confidence_kind_empty_returns_empty():
    assert legitimacy_check.highest_confidence_kind([]) == ""


def test_collector_exception_is_swallowed(monkeypatch):
    def boom(name):
        raise RuntimeError("ca sos exploded")
    monkeypatch.setattr(legitimacy_check, "lookup_ca_sos", boom)
    monkeypatch.setattr(legitimacy_check, "lookup_chamber_roster", lambda name, city: None)
    monkeypatch.setattr(legitimacy_check, "lookup_prior_inbound", lambda **kw: None)
    p = _Prospect(company_name="Acme", city="X")
    sigs = legitimacy_check.collect_signals(p)
    assert sigs == []
