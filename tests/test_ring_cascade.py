"""Ring cascade -- email-exhausted prospects advance to the next channel."""

from __future__ import annotations

import csv
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.cash_engine.ring_cascade import (
    Ring,
    RingTransition,
    cascade_from_email,
    enroll_in_ring,
    is_email_cap_reached,
    select_next_ring,
)
from backend.cash_engine.stages import StageContext, _outreach_stage
from backend.cash_engine.state import CashEngineState
from backend.prospecting.csv_export import CSV_COLUMNS


VALID_STAKE = (
    "Alex picked you because Acme Plumbing has the worst homepage I have "
    "seen and it is costing you real calls every single week."
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_DATA_ROOT", str(tmp_path))


def _prospect(
    prospect_id="pr-1",
    phone="(916) 555-0100",
    owner_email="",
    social_linkedin="",
    social_facebook="",
):
    from backend.crm.models import Prospect

    return Prospect(
        prospect_id=prospect_id,
        company_name="Acme Plumbing",
        website_url="https://acme.test",
        industry="plumbing",
        zipcode="95901",
        phone=phone,
        owner_email=owner_email,
        social_linkedin=social_linkedin,
        social_facebook=social_facebook,
    )


def _opp(opportunity_id="op-1", prospect_id="pr-1", stake=VALID_STAKE):
    from backend.crm.models import Opportunity

    return Opportunity(
        opportunity_id=opportunity_id,
        prospect_id=prospect_id,
        stage="proposal",
        stake_sentence=stake,
    )


class FakeCRM:
    def __init__(self):
        self.artifacts = []
        self.call_states = []
        self.operator_tasks = []

    def get_opportunity(self, oid):
        return _opp(opportunity_id=oid)

    def get_prospect(self, pid):
        return _prospect(prospect_id=pid)

    def get_call_state(self, pid):
        return None

    def upsert_call_state(self, state):
        self.call_states.append(state)
        return True

    def create_artifact(self, req):
        self.artifacts.append(req)
        return SimpleNamespace(artifact_id=f"art-{len(self.artifacts)}", status="created")

    def create_operator_task(self, req):
        self.operator_tasks.append(req)
        return SimpleNamespace(operator_task_id=f"task-{len(self.operator_tasks)}")


# ---------------------------------------------------------------------------
# Ring reachability
# ---------------------------------------------------------------------------


class TestRingReachability:
    def test_voice_reachable_with_phone(self):
        p = _prospect(phone="(916) 555-0100")
        ring = Ring(channel="voice", static_rank=1)
        assert ring.is_reachable(p) is True

    def test_voice_not_reachable_without_phone(self):
        p = _prospect(phone="")
        ring = Ring(channel="voice", static_rank=1)
        assert ring.is_reachable(p) is False

    def test_social_reachable_with_linkedin(self):
        p = _prospect(social_linkedin="https://linkedin.com/in/acme")
        ring = Ring(channel="social_dm", static_rank=4)
        assert ring.is_reachable(p) is True

    def test_social_not_reachable_without_handles(self):
        p = _prospect()
        ring = Ring(channel="social_dm", static_rank=4)
        assert ring.is_reachable(p) is False

    def test_sms_not_reachable_without_consent(self):
        p = _prospect(phone="(916) 555-0100")
        ring = Ring(channel="sms", static_rank=3)
        assert ring.is_reachable(p) is False

    def test_physical_flyer_not_reachable_without_address(self):
        p = _prospect()
        ring = Ring(channel="physical_flyer", static_rank=5)
        assert ring.is_reachable(p) is False


# ---------------------------------------------------------------------------
# Ring selection
# ---------------------------------------------------------------------------


class TestSelectNextRing:
    def test_selects_voice_after_email_exhausted(self):
        p = _prospect(phone="(916) 555-0100")
        o = _opp()
        ring = select_next_ring(p, o, exhausted_channels=("personal_email",))
        assert ring is not None
        assert ring.channel == "voice"

    def test_selects_voicemail_when_voice_also_exhausted(self):
        p = _prospect(phone="(916) 555-0100")
        o = _opp()
        ring = select_next_ring(
            p,
            o,
            exhausted_channels=("personal_email", "voice"),
        )
        assert ring is not None
        assert ring.channel == "voicemail_drop"

    def test_returns_none_when_all_exhausted(self):
        p = _prospect(phone="")
        o = _opp()
        ring = select_next_ring(p, o, exhausted_channels=("personal_email",))
        assert ring is None

    def test_selects_social_when_voice_exhausted_and_handle_present(self):
        p = _prospect(phone="", social_linkedin="https://linkedin.com/in/x")
        o = _opp()
        ring = select_next_ring(
            p,
            o,
            exhausted_channels=("personal_email", "voice", "voicemail_drop"),
        )
        assert ring is not None
        assert ring.channel == "social_dm"


# ---------------------------------------------------------------------------
# Voice enrollment
# ---------------------------------------------------------------------------


class TestVoiceEnrollment:
    def test_appends_to_call_list_csv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        p = _prospect(phone="(916) 555-0100")
        o = _opp()
        crm = FakeCRM()
        ring = Ring(channel="voice", static_rank=1)

        result = enroll_in_ring(
            ring,
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
        )
        assert result["enrolled"] is True
        assert result["channel"] == "voice"

        csv_path = tmp_path / "daily_calls" / f"call_list_{date.today().isoformat()}.csv"
        assert csv_path.exists()
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = list(csv.DictReader(fh))
        assert len(reader) == 1
        assert reader[0]["prospect_id"] == "pr-1"
        assert reader[0]["call_priority"] == "warm"

    def test_deduplicates_existing_prospect(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        csv_dir = tmp_path / "daily_calls"
        csv_dir.mkdir(parents=True)
        csv_path = csv_dir / f"call_list_{date.today().isoformat()}.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            row = {col: "" for col in CSV_COLUMNS}
            row["prospect_id"] = "pr-1"
            row["phone"] = "(916) 555-0100"
            writer.writerow(row)

        p = _prospect()
        o = _opp()
        crm = FakeCRM()
        ring = Ring(channel="voice", static_rank=1)

        result = enroll_in_ring(
            ring,
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
        )
        assert result["enrolled"] is True
        assert result["already_on_list"] is True

        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = list(csv.DictReader(fh))
        assert len(reader) == 1

    def test_upserts_crm_call_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        p = _prospect()
        o = _opp()
        crm = FakeCRM()
        ring = Ring(channel="voice", static_rank=1)

        enroll_in_ring(ring, p, o, stake_sentence=VALID_STAKE, crm=crm)
        assert len(crm.call_states) == 1
        cs = crm.call_states[0]
        assert cs.state == "queued"
        assert "ring_cascade" in cs.last_outcome


# ---------------------------------------------------------------------------
# Voicemail enrollment
# ---------------------------------------------------------------------------


class TestVoicemailEnrollment:
    def test_creates_voicemail_artifact(self):
        p = _prospect()
        o = _opp()
        crm = FakeCRM()
        ring = Ring(channel="voicemail_drop", static_rank=2)

        result = enroll_in_ring(
            ring,
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
        )
        assert result["enrolled"] is True
        assert result["channel"] == "voicemail_drop"
        assert "voicemail_artifact_id" in result
        assert len(crm.artifacts) == 1
        assert crm.artifacts[0].kind == "voicemail"


# ---------------------------------------------------------------------------
# Social enrollment
# ---------------------------------------------------------------------------


class TestSocialEnrollment:
    def test_creates_social_dm_draft(self):
        p = _prospect(social_linkedin="https://linkedin.com/in/acme")
        o = _opp()
        crm = FakeCRM()
        ring = Ring(channel="social_dm", static_rank=4)

        result = enroll_in_ring(
            ring,
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
        )
        assert result["enrolled"] is True
        assert result["channel"] == "social_dm"
        assert result["platform"] == "linkedin"

    def test_returns_not_enrolled_without_handle(self):
        p = _prospect()
        o = _opp()
        crm = FakeCRM()
        ring = Ring(channel="social_dm", static_rank=4)

        result = enroll_in_ring(
            ring,
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
        )
        assert result["enrolled"] is False


# ---------------------------------------------------------------------------
# Email cap detection
# ---------------------------------------------------------------------------


class TestEmailCapReached:
    def test_false_when_under_cap(self, monkeypatch):
        monkeypatch.setenv("SAMUS_MAX_SENDS_PER_DAY", "15")
        with patch("backend.common.daily_counter.count_today", return_value=5):
            assert is_email_cap_reached() is False

    def test_true_when_at_cap(self, monkeypatch):
        monkeypatch.setenv("SAMUS_MAX_SENDS_PER_DAY", "15")
        with patch("backend.common.daily_counter.count_today", return_value=15):
            assert is_email_cap_reached() is True


# ---------------------------------------------------------------------------
# Full cascade
# ---------------------------------------------------------------------------


class TestCascadeFromEmail:
    def test_cascades_to_voice_on_compose_block(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        p = _prospect(phone="(916) 555-0100", owner_email="")
        o = _opp()
        crm = FakeCRM()

        result = cascade_from_email(
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
            reason="email_compose_blocked: no cold-sendable email",
        )
        assert result["cascaded"] is True
        assert result["enrollment"]["channel"] == "voice"
        assert result["transition"]["from_channel"] == "personal_email"
        assert result["transition"]["to_channel"] == "voice"

    def test_cascades_to_voicemail_when_voice_exhausted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        p = _prospect(phone="(916) 555-0100")
        o = _opp()
        crm = FakeCRM()

        result = cascade_from_email(
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
            reason="test",
            exhausted_channels=("personal_email", "voice"),
        )
        assert result["cascaded"] is True
        assert result["enrollment"]["channel"] == "voicemail_drop"

    def test_not_cascaded_when_no_reachable_ring(self):
        p = _prospect(phone="", owner_email="")
        o = _opp()
        crm = FakeCRM()

        result = cascade_from_email(
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
            reason="test",
        )
        assert result["cascaded"] is False
        assert "all_rings_exhausted" in result["reason"]


# ---------------------------------------------------------------------------
# Outreach stage integration
# ---------------------------------------------------------------------------


class TestOutreachStageCascade:
    def _ctx(self, prospect=None, opportunity=None, crm=None, settings=None):
        from backend.common.settings import get_settings

        return StageContext(
            state=CashEngineState(opportunity_id="op-1", prospect_id="pr-1"),
            opportunity=opportunity or _opp(),
            prospect=prospect,
            stake_sentence=VALID_STAKE,
            crm=crm or FakeCRM(),
            settings=settings or get_settings(),
        )

    def test_compose_blocked_cascades_to_voice(self, tmp_path, monkeypatch):
        """A prospect with only a role email (compose-blocked) gets enrolled
        into the voice ring instead of being escalated."""
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        p = _prospect(phone="(916) 555-0100", owner_email="info@acme.test")
        crm = FakeCRM()
        ctx = self._ctx(prospect=p, crm=crm)

        result = _outreach_stage(ctx)

        assert result.ok is True, f"Expected ok=True, got {result}"
        assert result.detail.get("ring_cascade") is True
        assert result.detail.get("channel") == "voice"

    def test_compose_blocked_no_phone_parks(self, monkeypatch):
        """A prospect with no email AND no phone parks (all rings exhausted)."""
        p = _prospect(phone="", owner_email="info@acme.test")
        crm = FakeCRM()
        ctx = self._ctx(prospect=p, crm=crm)

        result = _outreach_stage(ctx)

        assert result.ok is False
        assert result.parked is True
        assert result.park_reason == "all_outreach_rings_exhausted"

    def test_email_cap_reached_cascades(self, tmp_path, monkeypatch):
        """When the daily email cap is reached, outreach cascades to voice."""
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        monkeypatch.setenv("SAMUS_MAX_SENDS_PER_DAY", "15")
        p = _prospect(
            phone="(916) 555-0100",
            owner_email="founder@acme.test",
        )
        crm = FakeCRM()
        ctx = self._ctx(prospect=p, crm=crm)

        with patch(
            "backend.cash_engine.ring_cascade.is_email_cap_reached",
            return_value=True,
        ):
            result = _outreach_stage(ctx)

        assert result.ok is True
        assert result.detail.get("ring_cascade") is True
        assert result.detail.get("ring_cascade_reason") == "email_daily_cap_reached"

    def test_email_ok_no_cascade(self, monkeypatch):
        """When email compose succeeds, no cascade fires."""
        p = _prospect(
            phone="(916) 555-0100",
            owner_email="founder@acme.test",
        )
        crm = FakeCRM()
        ctx = self._ctx(prospect=p, crm=crm)
        ctx.settings = SimpleNamespace(
            cash_engine_live_send_enabled=False,
            ses_from_email="",
            sendgrid_from_email="",
            sendgrid_from_name="Samus",
            sender_postal_address="123 Main",
            unsubscribe_url="https://unsub.test",
        )

        result = _outreach_stage(ctx)

        assert result.ok is True
        assert result.detail.get("ring_cascade") is not True
        assert "outreach_ref" in result.detail


# ---------------------------------------------------------------------------
# Ring transition events
# ---------------------------------------------------------------------------


class TestRingTransitionEvents:
    def test_transition_has_required_fields(self):
        t = RingTransition(
            prospect_id="pr-1",
            opportunity_id="op-1",
            from_channel="personal_email",
            to_channel="voice",
            reason="compose_blocked",
            ev=150.0,
        )
        assert t.from_channel == "personal_email"
        assert t.to_channel == "voice"
        assert t.ts != ""

    def test_cascade_emits_business_event(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        events = []

        def _capture(event_type, **kw):
            events.append({"event_type": event_type, **kw})
            return {"emitted": True}

        monkeypatch.setattr(
            "backend.cash_engine.ring_cascade.emit_business_event",
            _capture,
        )
        p = _prospect(phone="(916) 555-0100")
        o = _opp()
        crm = FakeCRM()

        cascade_from_email(
            p,
            o,
            stake_sentence=VALID_STAKE,
            crm=crm,
            reason="test_event",
        )

        assert len(events) >= 1
        decision_events = [e for e in events if e.get("event_type") == "decision.made"]
        assert len(decision_events) >= 1
        meta = decision_events[0].get("metadata", {})
        assert meta.get("decision_kind") == "ring_cascade_transition"
        assert meta.get("from_channel") == "personal_email"
        assert meta.get("to_channel") == "voice"


# ---------------------------------------------------------------------------
# Worker integration -- cascade does not park the opportunity
# ---------------------------------------------------------------------------


class TestWorkerIntegration:
    def test_compose_blocked_prospect_reaches_dormant(self, tmp_path, monkeypatch):
        """A compose-blocked prospect with a phone cascades to voice and
        the walker continues through deliver -> parks as deal_not_won ->
        goes dormant.  It does NOT get stuck at outreach."""
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))

        from backend.cash_engine.worker import process_job

        p = _prospect(phone="(916) 555-0100", owner_email="info@acme.test")
        crm = FakeCRM()
        crm.get_prospect = lambda pid: p

        job = {
            "payload": {
                "opportunity_id": "op-1",
                "prospect_id": "pr-1",
                "trigger_source": "manual_review",
                "task_id": "ce-test",
            },
        }

        state = process_job(job, crm=crm)
        assert state is not None
        assert "outreach" in state.completed_stages
        assert state.status != "escalated"
