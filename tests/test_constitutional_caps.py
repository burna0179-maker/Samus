"""Constitutional hard caps (HOTL Tranche 5, deliverable 3).

Covers the four hard operational limits:
  * durable daily counter (backend/common/daily_counter.py)
  * mass-messaging cap in outreach.service.send_message
  * call-volume cap in voice.service.initiate_call
  * infinite-loop retry ceiling in common/worker_base.py
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest


# ---------------------------------------------------------------------------
# daily_counter — durable per-key day tally
# ---------------------------------------------------------------------------


def test_daily_counter_increment_and_count(tmp_path, monkeypatch):
    ledger = tmp_path / "coordination" / "daily_counters.jsonl"
    monkeypatch.setenv("SAMUS_DAILY_COUNTER_PATH", str(ledger))
    from backend.common import daily_counter

    day = _dt.date(2026, 7, 1)
    assert daily_counter.count_today("outreach.sends", today=day) == 0
    assert daily_counter.increment("outreach.sends", today=day) == 1
    assert daily_counter.increment("outreach.sends", today=day) == 2
    assert daily_counter.count_today("outreach.sends", today=day) == 2
    # A different key is independent.
    assert daily_counter.count_today("voice.calls", today=day) == 0
    # A different day is independent.
    assert daily_counter.count_today("outreach.sends", today=_dt.date(2026, 7, 2)) == 0

    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows == [
        {"date": "2026-07-01", "key": "outreach.sends"},
        {"date": "2026-07-01", "key": "outreach.sends"},
    ]


def test_daily_counter_increment_survives_write_error(tmp_path, monkeypatch):
    """A write failure still advances the returned count (cap holds in-process)."""
    ledger = tmp_path / "coordination" / "daily_counters.jsonl"
    monkeypatch.setenv("SAMUS_DAILY_COUNTER_PATH", str(ledger))
    from backend.common import daily_counter

    monkeypatch.setattr(
        daily_counter.Path,
        "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert daily_counter.increment("k") == 1  # no raise, count advanced


# ---------------------------------------------------------------------------
# outreach send cap
# ---------------------------------------------------------------------------


def _email_req(**over):
    from backend.outreach.models import OutreachMessageRequest

    base = dict(
        prospect_id="pr_acme",
        channel="email",
        template_id="cash_engine_initial",
        body="hello",
        to="owner@acme.com",
        subject="Quick question",
        company="Acme HVAC",
        phone="555-0100",
    )
    base.update(over)
    return OutreachMessageRequest(**base)


def _wire_fake_email(monkeypatch, tmp_path, sent_calls):
    import backend.common.email_backend as email_backend
    import backend.outreach.service as svc
    from backend.common.settings import reload_settings

    def _fake_send_email(to, subject, body, **kwargs):
        sent_calls.append(to)
        return {
            "message_id": f"m_{len(sent_calls)}",
            "channel": "email",
            "to": to,
            "ts": "2026-07-01T09:00:00Z",
        }

    monkeypatch.setattr(email_backend, "send_email", _fake_send_email)
    monkeypatch.setattr(svc, "_dispatch_outreach_to_crm", lambda *a, **k: None)
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    # get_settings() is lru_cached — pick up the per-test cap env just set.
    reload_settings()
    return svc


def test_send_cap_blocks_over_ceiling(tmp_path, monkeypatch):
    """The (cap+1)th send is blocked before dispatch and raises SendCapExceeded."""
    monkeypatch.setenv("SAMUS_DAILY_COUNTER_PATH", str(tmp_path / "counters.jsonl"))
    monkeypatch.setenv("SAMUS_MAX_SENDS_PER_DAY", "3")
    monkeypatch.setenv("SAMUS_SEND_RAMP_LEDGER_PATH", str(tmp_path / "ramp.jsonl"))

    sent_calls: list[str] = []
    svc = _wire_fake_email(monkeypatch, tmp_path, sent_calls)
    # No harm signals / no operator-task side effects.
    monkeypatch.setattr(svc, "_check_harm_suppression", lambda req: None)
    monkeypatch.setattr(svc, "_block_capped_send", _raise_capped)

    # First 3 sends succeed and tick the counter.
    for i in range(3):
        out = svc.send_message(_email_req(prospect_id=f"pr_{i}"))
        assert out["channel"] == "email"
    assert len(sent_calls) == 3

    # 4th is blocked before the email backend is touched.
    with pytest.raises(svc.SendCapExceeded) as ei:
        svc.send_message(_email_req(prospect_id="pr_over"))
    assert ei.value.cap == 3
    assert ei.value.sent_today == 3
    assert len(sent_calls) == 3  # backend NOT called on the blocked send


def _raise_capped(req, *, sent_today, cap):
    # Slim stand-in for _block_capped_send that skips CRM/event side effects.
    import backend.outreach.service as svc

    raise svc.SendCapExceeded("capped", sent_today=sent_today, cap=cap)


def test_send_cap_zero_disables(tmp_path, monkeypatch):
    """A non-positive cap disables the ceiling entirely (never blocks)."""
    monkeypatch.setenv("SAMUS_DAILY_COUNTER_PATH", str(tmp_path / "counters.jsonl"))
    monkeypatch.setenv("SAMUS_MAX_SENDS_PER_DAY", "0")
    monkeypatch.setenv("SAMUS_SEND_RAMP_LEDGER_PATH", str(tmp_path / "ramp.jsonl"))

    sent_calls: list[str] = []
    svc = _wire_fake_email(monkeypatch, tmp_path, sent_calls)
    monkeypatch.setattr(svc, "_check_harm_suppression", lambda req: None)

    for i in range(20):
        svc.send_message(_email_req(prospect_id=f"pr_{i}"))
    assert len(sent_calls) == 20  # all went out, cap disabled


def test_send_cap_emits_event_and_operator_task(tmp_path, monkeypatch):
    """A real cap breach emits a decision.made event and files an operator task."""
    monkeypatch.setenv("SAMUS_DAILY_COUNTER_PATH", str(tmp_path / "counters.jsonl"))
    monkeypatch.setenv("SAMUS_MAX_SENDS_PER_DAY", "1")
    monkeypatch.setenv("SAMUS_SEND_RAMP_LEDGER_PATH", str(tmp_path / "ramp.jsonl"))
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "events.jsonl"))

    sent_calls: list[str] = []
    svc = _wire_fake_email(monkeypatch, tmp_path, sent_calls)
    monkeypatch.setattr(svc, "_check_harm_suppression", lambda req: None)

    tasks: list = []
    import backend.crm.service as crm

    monkeypatch.setattr(crm, "create_operator_task", lambda req: tasks.append(req))

    svc.send_message(_email_req(prospect_id="pr_0"))  # 1st ok
    with pytest.raises(svc.SendCapExceeded):
        svc.send_message(_email_req(prospect_id="pr_1"))  # 2nd blocked

    assert len(tasks) == 1
    assert "send cap" in tasks[0].title.lower()

    from backend.common.business_events import DECISION_MADE, read_events

    evs = read_events(event_types=[DECISION_MADE])
    caps = [e for e in evs if (e.get("metadata") or {}).get("decision") == "send_cap_blocked"]
    assert len(caps) == 1
    assert caps[0]["metadata"]["cap"] == 1


# ---------------------------------------------------------------------------
# voice call cap
# ---------------------------------------------------------------------------


def _call_req(**over):
    from backend.voice.models import InitiateCallRequest

    base = dict(
        assistant_id="asst_1",
        phone_number_id="pn_1",
        customer_number="+15550100",
        customer_name="Acme",
        metadata={"prospect_id": "pr_acme", "source": "test"},
    )
    base.update(over)
    return InitiateCallRequest(**base)


def test_call_cap_blocks_over_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DAILY_COUNTER_PATH", str(tmp_path / "counters.jsonl"))
    monkeypatch.setenv("SAMUS_MAX_CALLS_PER_DAY", "2")
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "vaudit.jsonl"))
    monkeypatch.setenv("VAPI_API_KEY", "vk_test")
    from backend.common.settings import reload_settings

    reload_settings()

    import backend.voice.service as vsvc

    class _FakeCall:
        def __init__(self, cid):
            self.id = cid
            self.status = "queued"

    class _FakeClient:
        def __init__(self):
            self.n = 0

        def create_call(self, **kwargs):
            self.n += 1
            return _FakeCall(f"call_{self.n}")

    monkeypatch.setattr(vsvc, "_new_vapi_client", lambda: _FakeClient())

    r1 = vsvc.initiate_call(_call_req())
    r2 = vsvc.initiate_call(_call_req())
    assert r1.call_id and r2.call_id and r1.vapi_error is None

    # 3rd is capped — structured error, no raise, no call_id.
    r3 = vsvc.initiate_call(_call_req())
    assert r3.vapi_error == "call_cap_reached"
    assert r3.call_id == ""


def test_call_cap_does_not_count_degraded(tmp_path, monkeypatch):
    """A degraded dial (no client) must NOT consume cap budget."""
    monkeypatch.setenv("SAMUS_DAILY_COUNTER_PATH", str(tmp_path / "counters.jsonl"))
    monkeypatch.setenv("SAMUS_MAX_CALLS_PER_DAY", "1")
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "vaudit.jsonl"))
    from backend.common.settings import reload_settings

    reload_settings()

    import backend.voice.service as vsvc

    monkeypatch.setattr(vsvc, "_new_vapi_client", lambda: None)  # unconfigured

    out = vsvc.initiate_call(_call_req())
    assert out.vapi_error == "vapi_api_key_unset"

    from backend.common import daily_counter

    assert daily_counter.count_today("voice.calls") == 0  # degraded didn't tick


# ---------------------------------------------------------------------------
# worker retry ceiling
# ---------------------------------------------------------------------------


def test_worker_retry_ceiling_sheds_to_dlq(tmp_path, monkeypatch):
    """A message received >= MAX_HANDLER_ATTEMPTS times is force-shed + deleted."""
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path / "dlq"))
    monkeypatch.setenv("SAMUS_DATA_ROOT", str(tmp_path / "data"))

    from backend.common import worker_base
    from backend.common.queue_contracts import QueueEnvelope

    deleted: list[str] = []

    class _Table:
        def put_item(self, **k):  # noqa: D401
            return None

    class _Runtime:
        def task_state_table(self):
            return _Table()

        def publish_event(self, *a, **k):
            return None

        def delete_message(self, receipt):
            deleted.append(receipt)

    class _FailingWorker(worker_base.BaseSqsWorker):
        service = "seo"

        def handle(self, envelope, *, stop_event=None):
            raise RuntimeError("boom")

    # Bypass HMAC authenticity in-process.
    monkeypatch.setattr(worker_base, "check_message_authenticity", lambda *a, **k: True)

    w = _FailingWorker(_Runtime())
    env = QueueEnvelope(task_id="t1", service="seo", action="work", payload={"x": 1})
    msg = {
        "ReceiptHandle": "rh1",
        "Body": env.model_dump_json(),
        "Attributes": {"ApproximateReceiveCount": str(worker_base.MAX_HANDLER_ATTEMPTS)},
    }
    w._process_message(msg)

    # Deleted (shed) rather than left for another redrive.
    assert deleted == ["rh1"]
    from backend.common import dlq

    pending = dlq.read_pending("seo", limit=10)
    assert any(p.get("task_id") == "t1" for p in pending)
    assert any("retry ceiling" in str(p.get("error", "")) for p in pending)


def test_worker_below_ceiling_leaves_message(tmp_path, monkeypatch):
    """Below the ceiling the message is NOT deleted — normal SQS redrive."""
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path / "dlq"))
    monkeypatch.setenv("SAMUS_DATA_ROOT", str(tmp_path / "data"))

    from backend.common import worker_base
    from backend.common.queue_contracts import QueueEnvelope

    deleted: list[str] = []

    class _Table:
        def put_item(self, **k):
            return None

    class _Runtime:
        def task_state_table(self):
            return _Table()

        def publish_event(self, *a, **k):
            return None

        def delete_message(self, receipt):
            deleted.append(receipt)

    class _FailingWorker(worker_base.BaseSqsWorker):
        service = "seo"

        def handle(self, envelope, *, stop_event=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(worker_base, "check_message_authenticity", lambda *a, **k: True)

    w = _FailingWorker(_Runtime())
    env = QueueEnvelope(task_id="t2", service="seo", action="work", payload={})
    msg = {
        "ReceiptHandle": "rh2",
        "Body": env.model_dump_json(),
        "Attributes": {"ApproximateReceiveCount": "1"},
    }
    w._process_message(msg)
    assert deleted == []  # left on the queue for redrive
