"""Outreach pre-flight warmth gate (G8): 2 warm + 3 cold → 2 messages, 3 diverted."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.outreach import run_campaign as rc
from backend.outreach.apollo_source import ApolloContact
from backend.prospecting.legitimacy import LegitimacySignal


def _contact(person_id: str, email: str, *, locked: bool = False) -> ApolloContact:
    return ApolloContact(
        person_id=person_id,
        first_name=person_id.upper(),
        name=f"{person_id} Person",
        title="Owner",
        email=email if not locked else f"email_not_unlocked@x.com",
        email_status="verified",
        company=f"{person_id}-co",
        company_domain=f"{person_id}.com",
        industry="hvac",
        city="Yuba City",
    )


@pytest.fixture()
def isolated_artifact_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("DDB_NEEDS_WARM_PATH_TABLE", "")
    from backend.common import config as cfg
    cfg.get_settings.cache_clear()  # type: ignore[attr-defined]
    yield tmp_path
    cfg.get_settings.cache_clear()  # type: ignore[attr-defined]


def test_apply_warmth_gate_diverts_cold_keeps_warm(isolated_artifact_root, monkeypatch):
    warm_ids = {"w1", "w2"}

    def fake_assess_warmth(prospect):
        from backend.prospecting.legitimacy import LegitimacyAssessment
        pid = getattr(prospect, "person_id", "")
        if pid in warm_ids:
            sig = LegitimacySignal(
                kind="public_registry",
                source="ca_sos",
                discovered_at=datetime.now(timezone.utc),
                evidence={},
                confidence="high",
            )
            return LegitimacyAssessment(
                prospect_id=pid, signals=[sig], has_warmth=True,
                assessed_at=datetime.now(timezone.utc),
            )
        return LegitimacyAssessment(
            prospect_id=pid, signals=[], has_warmth=False,
            assessed_at=datetime.now(timezone.utc),
        )

    # Patch INSIDE the legitimacy_check module — _apply_warmth_gate imports
    # assess_warmth + highest_confidence_kind at call time.
    from backend.prospecting import legitimacy_check
    monkeypatch.setattr(legitimacy_check, "assess_warmth", fake_assess_warmth)

    contacts = [
        _contact("w1", "w1@x.com"),
        _contact("c1", "c1@x.com"),
        _contact("w2", "w2@x.com"),
        _contact("c2", "c2@x.com"),
        _contact("c3", "c3@x.com"),
    ]
    kept, diverted = rc._apply_warmth_gate(contacts)
    assert {c.person_id for c in kept} == {"w1", "w2"}
    assert len(diverted) == 3
    assert all(d["reason"] == "no_legitimacy_signal" for d in diverted)
    for c in kept:
        assert c.legitimacy_signal == "public_registry"

    # Each cold contact landed in needs_warm_path.
    from backend.crm.needs_warm_path import list_pending
    pending_ids = {r.prospect_id for r in list_pending()}
    assert {"c1", "c2", "c3"}.issubset(pending_ids)


def test_log_diverted_no_warmth_appends_jsonl(isolated_artifact_root):
    rc._log_diverted_no_warmth([
        {"ts": "now", "prospect_id": "x1", "email": "x@y.com",
         "company": "X", "reason": "no_legitimacy_signal"},
    ])
    path = rc._diverted_no_warmth_path()
    assert path.endswith("outreach_diverted_no_warmth.jsonl")
    with open(path, encoding="utf-8") as fh:
        line = fh.readline()
    assert "x1" in line and "no_legitimacy_signal" in line
