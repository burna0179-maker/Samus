"""Morning brief — outreach follow-ups SALES lane.

Covers backend.morning._render_follow_ups: it reads CRM CallState via
crm.service.list_follow_ups_due and renders a 'Follow-ups due' block, degrading
to an omitted section (returns []) on any CRM failure — the same posture the
brief takes for the Stripe pulls.
"""
from __future__ import annotations

import datetime as _dt


def _stub_follow_ups(monkeypatch, follow_list):
    """Patch crm.service.list_follow_ups_due to return a canned FollowUpList."""
    import backend.crm.service as crm_service
    monkeypatch.setattr(crm_service, "list_follow_ups_due",
                        lambda *, today, limit=100: follow_list)


def _fl(items, *, ddb_error=None):
    from backend.crm.models import FollowUpList
    return FollowUpList(follow_ups=items, count=len(items), ddb_error=ddb_error)


def _due(**over):
    from backend.crm.models import FollowUpDue
    base = dict(prospect_id="pr_acme", company="Acme HVAC", phone="555-0100",
                channel="email", subject="Quick question",
                emailed_on="2026-05-20", follow_up_on="2026-05-22",
                days_waiting=2, attempt_count=0)
    base.update(over)
    return FollowUpDue(**base)


_TODAY = _dt.date(2026, 5, 22)


def test_render_follow_ups_lists_due_prospects(monkeypatch):
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    _stub_follow_ups(monkeypatch, _fl([
        _due(prospect_id="pr_a", company="Acme HVAC", days_waiting=2),
        _due(prospect_id="pr_b", company="Bell Roofing", phone="555-0200",
             days_waiting=1, subject="Your website"),
    ]))
    from backend.morning import _render_follow_ups
    text = "\n".join(_render_follow_ups(_TODAY))
    assert "Follow-ups due: 2" in text
    assert "Acme HVAC" in text and "555-0100" in text
    assert "Bell Roofing" in text and "555-0200" in text
    assert "emailed 2d ago" in text
    assert "emailed 1d ago" in text
    assert "Your website" in text


def test_render_follow_ups_empty_when_none_due(monkeypatch):
    _stub_follow_ups(monkeypatch, _fl([]))
    from backend.morning import _render_follow_ups
    assert _render_follow_ups(_TODAY) == []


def test_render_follow_ups_empty_on_ddb_error(monkeypatch):
    """A DynamoDB error -> omitted section, not a broken brief."""
    _stub_follow_ups(monkeypatch, _fl([_due()], ddb_error="ddb_scan_failed: boom"))
    from backend.morning import _render_follow_ups
    assert _render_follow_ups(_TODAY) == []


def test_render_follow_ups_empty_when_crm_raises(monkeypatch):
    """list_follow_ups_due raising (e.g. no AWS creds) degrades gracefully."""
    import backend.crm.service as crm_service

    def _boom(*, today, limit=100):
        raise RuntimeError("no AWS credentials")

    monkeypatch.setattr(crm_service, "list_follow_ups_due", _boom)
    from backend.morning import _render_follow_ups
    assert _render_follow_ups(_TODAY) == []


def test_render_follow_ups_emailed_today_wording(monkeypatch):
    """days_waiting == 0 reads 'emailed today', not 'emailed 0d ago'."""
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    _stub_follow_ups(monkeypatch, _fl([_due(days_waiting=0)]))
    from backend.morning import _render_follow_ups
    text = "\n".join(_render_follow_ups(_TODAY))
    assert "emailed today" in text


def test_render_follow_ups_falls_back_to_prospect_id_without_company(monkeypatch):
    """A follow-up with no resolved company still shows — by prospect_id."""
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    _stub_follow_ups(monkeypatch, _fl([
        _due(prospect_id="pr_orphan", company="", phone=""),
    ]))
    from backend.morning import _render_follow_ups
    text = "\n".join(_render_follow_ups(_TODAY))
    assert "pr_orphan" in text


def test_render_follow_ups_shows_upsell_hint(monkeypatch):
    """A FollowUpDue carrying an upsell renders an ↗ hint line."""
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    _stub_follow_ups(monkeypatch, _fl([
        _due(upsell_name="Workflow System Buildout",
             upsell_pitch='offer Workflow System Buildout ($2,500) — they signalled "workflow"'),
    ]))
    from backend.morning import _render_follow_ups
    text = "\n".join(_render_follow_ups(_TODAY))
    assert "↗" in text
    assert "Workflow System Buildout" in text


def test_render_follow_ups_no_upsell_line_without_pitch(monkeypatch):
    """No upsell pitch -> no ↗ line."""
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    _stub_follow_ups(monkeypatch, _fl([_due(upsell_pitch="")]))
    from backend.morning import _render_follow_ups
    text = "\n".join(_render_follow_ups(_TODAY))
    assert "↗" not in text
