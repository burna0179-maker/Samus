"""Cold-list exclusion — prospects in an active warm sequence (buying_signal
enrollment, or a hand-logged warm outcome) must NOT appear in tomorrow's
morning_call_list, but MUST be surfaced in the EXCLUDED footer so the
operator knows they were intentionally held back.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import backend.common.config as config_mod
import backend.outreach.buying_signal_route as bsr


def _prospect(pid: str, name: str, **overrides):
    from backend.prospecting.models import ProspectRecord
    base = dict(
        prospect_id=pid,
        company_name=name,
        phone="(555) 111-2222",
        website_url=f"https://{pid}.example/",
        city="Yuba City", state="CA", zipcode="95993",
        industry="finance",
        call_priority="hot",
        lead_score=80,
        seo_score=20,
        callsheet_offer="x", callsheet_pitch="y",
        callsheet_opener="z", callsheet_voicemail="w",
        callsheet_objections="a — b",
    )
    base.update(overrides)
    return ProspectRecord(**base)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    """Point the buying_signal store + settings at a fresh tmp dir."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))

    def _gs():
        return SimpleNamespace(
            outreach_buying_signal_route_enabled=True,
            outreach_buying_signal_intent_threshold=70,
        )
    _gs.cache_clear = lambda: None
    monkeypatch.setattr(config_mod, "get_settings", _gs)
    return tmp_path


def test_warm_enrolled_prospect_excluded_from_call_list(isolated_store):
    from backend.prospecting.text_export import render_morning_call_list

    cold = _prospect("pr_cold", "Cold Co")
    warm = _prospect("pr_warm", "Warm Co")
    # Enroll warm into buying_signal — should drop it from the cold list.
    bsr.maybe_enroll_buying_signal_from_email_reply(
        prospect_id="pr_warm",
        reply_text="Yes — please send the quote.",
        now_iso="2026-06-30T00:00:00Z",
        email="warm@example.com", company="Warm Co",
    )

    out = render_morning_call_list([cold, warm], run_date=date(2026, 6, 30))
    # Cold appears in the numbered list; warm does not.
    assert "#1  Cold Co" in out
    assert "#2  Warm Co" not in out
    # Header reflects the cold count, not the input count.
    assert "1 prospects ready" in out
    # EXCLUDED footer surfaces warm with the enrollment reason.
    assert "EXCLUDED FROM COLD LIST — 1 warm prospect(s)" in out
    assert "Warm Co" in out
    assert "buying_signal enrollment active" in out


def test_warm_outcome_excludes_even_without_enrollment(isolated_store):
    """A hand-logged warm outcome (booked, interested, quote_pending, etc.)
    excludes the prospect even when there's no buying_signal enrollment yet."""
    from backend.prospecting.text_export import render_morning_call_list

    p_cold = _prospect("pr_a", "Cold A")
    p_booked = _prospect("pr_b", "Booked B", last_contact_outcome="booked")
    p_quoting = _prospect("pr_c", "Quoting C",
                          last_contact_outcome="quote_pending")

    out = render_morning_call_list([p_cold, p_booked, p_quoting],
                                   run_date=date(2026, 6, 30))
    assert "#1  Cold A" in out
    assert "#2" not in out  # only one cold prospect remains
    assert "EXCLUDED FROM COLD LIST — 2 warm prospect(s)" in out
    assert "outcome=booked" in out
    assert "outcome=quote_pending" in out


def test_no_excluded_footer_when_all_cold(isolated_store):
    from backend.prospecting.text_export import render_morning_call_list
    p = _prospect("pr_a", "Cold A")
    out = render_morning_call_list([p], run_date=date(2026, 6, 30))
    assert "EXCLUDED FROM COLD LIST" not in out


def test_store_read_failure_falls_back_to_cold_only(monkeypatch):
    """If the buying_signal store can't be read, every prospect stays on
    the cold list — fail-OPEN so a storage fault doesn't drop work."""
    import backend.prospecting.text_export as te

    def _boom():
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(bsr, "active_warm_prospect_ids", _boom)
    p = _prospect("pr_a", "Cold A")
    out = te.render_morning_call_list([p], run_date=date(2026, 6, 30))
    assert "#1  Cold A" in out
    assert "EXCLUDED FROM COLD LIST" not in out
