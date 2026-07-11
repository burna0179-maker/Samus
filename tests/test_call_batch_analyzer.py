"""Tests for backend.voice.call_batch_analyzer — the self-improving post-batch
call auditor.

Covers the DETERMINISTIC core (bucketing, leak/regression guards, per-call
audit, aggregation, cross-batch trend, next-remediation ranking, durable store)
and one end-to-end pass through analyze_batch with an INJECTED fake Vapi client
so no network is touched and read-only-ness is asserted.

Fixtures were probed against the real gatekeeper_detector / BANNED_SELF_NARRATION
before being frozen here, so a green run means the audit agrees with the live
detectors, not with a re-implementation of them.
"""
from __future__ import annotations

import json

import pytest

from backend.voice import call_batch_analyzer as cba
from tests.test_voice_morgan_prompt_hardening import BANNED_SELF_NARRATION

# A real banned self-narration phrase (single source of truth = the prompt guard).
LEAK_PHRASE = BANNED_SELF_NARRATION[0]

# A transcript where a live human (gatekeeper) answers AFTER a machine/hold
# greeting — verified to trip gatekeeper_detector.detect_human_engagement.
GATEKEEPER_TX = "\n".join(
    [
        "User: Thank you for calling Acme Dental, you have reached the front desk, please hold.",
        "AI: Hi, this is Morgan. I'm calling about the website for the business owner.",
        "User: This is Sarah speaking, how can I help you?",
    ]
)

# A clean engaged conversation (no machine greeting, assistant carried it).
ENGAGED_TX = "\n".join(
    [
        "User: Hello, this is Dave.",
        "AI: Hi Dave, quick question about your website — got a sec?",
        "User: Sure, what's up.",
        "AI: Great, I'll keep it short.",
    ]
)


# ---------------------------------------------------------------------------
# Outcome bucketing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ended_reason,expected",
    [
        ("voicemail", "voicemail"),
        ("customer-ended-call", "live_hangup"),
        ("assistant-ended-call", "engaged"),
        ("silence-timed-out", "engaged"),
        ("customer-did-not-answer", "no_answer"),  # ordering trap: NOT live_hangup
        ("no-answer", "no_answer"),
        ("", "other"),
        ("pipeline-error-xyz", "other"),
    ],
)
def test_bucket_mapping(ended_reason: str, expected: str) -> None:
    assert cba._bucket_from_ended_reason(ended_reason) == expected


def test_bucket_ordering_trap_customer_prefix() -> None:
    # 'customer-did-not-answer' contains 'customer-' but must bucket as no_answer,
    # never live_hangup — the exact trap the Watch-DialRun switch guards against.
    assert cba._bucket_from_ended_reason("customer-did-not-answer") == "no_answer"
    assert cba._bucket_from_ended_reason("customer-ended-call") == "live_hangup"


# ---------------------------------------------------------------------------
# Reasoning-leak regression guard
# ---------------------------------------------------------------------------
def test_reasoning_leak_flags_ai_turn() -> None:
    tx = f"User: Hello?\nAI: {LEAK_PHRASE} ... anyway, hi."
    hits = cba.detect_reasoning_leak(tx)
    assert LEAK_PHRASE in hits


def test_reasoning_leak_ignores_prospect_saying_it() -> None:
    # A PROSPECT (User) uttering a banned phrase is NOT Morgan leaking — must be
    # clean. This is the core attribution correctness property.
    tx = f"User: {LEAK_PHRASE}\nAI: Understood, thank you."
    assert cba.detect_reasoning_leak(tx) == []


def test_reasoning_leak_clean_transcript() -> None:
    assert cba.detect_reasoning_leak(ENGAGED_TX) == []


def test_reasoning_leak_empty_banned_is_noop() -> None:
    tx = f"AI: {LEAK_PHRASE}"
    assert cba.detect_reasoning_leak(tx, banned=()) == []


# ---------------------------------------------------------------------------
# Finding-leaked-to-gatekeeper regression guard
# ---------------------------------------------------------------------------
def test_finding_leaked_to_gatekeeper_true() -> None:
    finding = "Your Google listing shows the wrong hours"
    tx = "\n".join(
        [
            "User: Thank you for calling, you have reached the desk, please hold.",
            f"AI: Hi, this is Morgan. {finding} and I wanted to flag it.",
            "User: This is Sarah speaking, how can I help you?",
        ]
    )
    assert cba.detect_finding_leaked_to_gatekeeper(
        tx, finding, was_gatekeeper=True
    ) is True


def test_finding_not_leaked_when_not_gatekeeper() -> None:
    finding = "Your Google listing shows the wrong hours"
    tx = f"AI: Hi, this is Morgan. {finding}."
    # Spoken, but NOT a gatekeeper interaction (owner) => not a leak.
    assert cba.detect_finding_leaked_to_gatekeeper(
        tx, finding, was_gatekeeper=False
    ) is False


def test_finding_leak_empty_finding_is_false() -> None:
    assert cba.detect_finding_leaked_to_gatekeeper(
        GATEKEEPER_TX, "", was_gatekeeper=True
    ) is False
    assert cba.detect_finding_leaked_to_gatekeeper(
        GATEKEEPER_TX, "short", was_gatekeeper=True
    ) is False


# ---------------------------------------------------------------------------
# Per-call audit
# ---------------------------------------------------------------------------
def test_analyze_call_engaged_owner() -> None:
    call = {
        "id": "c1",
        "endedReason": "assistant-ended-call",
        "transcript": ENGAGED_TX,
        "startedAt": "2026-07-02T16:00:00Z",
        "endedAt": "2026-07-02T16:02:00Z",
        "cost": 0.11,
        "phoneNumberId": "num-A",
        "customer": {"name": "Dave's Diner"},
    }
    a = cba.analyze_call(call)
    assert a.outcome_bucket == "engaged"
    assert a.reached_human is True
    assert a.reached_owner is True
    assert a.was_gatekeeper is False
    assert a.reasoning_leak_detected is False
    assert a.company == "Dave's Diner"
    assert a.caller_number_id == "num-A"
    # engaged (not customer-ended) => no time_to_hangup
    assert a.time_to_hangup_s is None


def test_analyze_call_gatekeeper() -> None:
    call = {
        "id": "c2",
        "endedReason": "voicemail",
        "transcript": GATEKEEPER_TX,
        "phoneNumberId": "num-B",
    }
    a = cba.analyze_call(call)
    assert a.was_gatekeeper is True
    assert a.outcome_bucket == "gatekeeper"
    assert a.reached_human is True
    assert a.reached_owner is False  # gatekeeper, not the decision-maker


def test_analyze_call_live_hangup_measures_time() -> None:
    call = {
        "id": "c3",
        "endedReason": "customer-ended-call",
        "transcript": "User: No thanks.\nAI: Okay, bye.",
        "startedAt": "2026-07-02T16:00:00Z",
        "endedAt": "2026-07-02T16:00:12Z",
    }
    a = cba.analyze_call(call)
    assert a.outcome_bucket == "live_hangup"
    assert a.time_to_hangup_s == 12.0


def test_analyze_call_flags_leak() -> None:
    call = {
        "id": "c4",
        "endedReason": "assistant-ended-call",
        "transcript": f"User: Hi.\nAI: {LEAK_PHRASE}",
    }
    a = cba.analyze_call(call)
    assert a.reasoning_leak_detected is True
    assert LEAK_PHRASE in a.reasoning_leak_phrases


def test_analyze_call_malformed_never_raises() -> None:
    a = cba.analyze_call({})
    assert a.outcome_bucket == "other"
    assert a.call_id == ""


# ---------------------------------------------------------------------------
# Aggregation + skew
# ---------------------------------------------------------------------------
def _audit(**kw):
    base = dict(
        call_id="x",
        outcome_bucket="other",
        ended_reason="",
        reached_human=False,
        was_gatekeeper=False,
        reached_owner=False,
        time_to_hangup_s=None,
        reasoning_leak_detected=False,
    )
    base.update(kw)
    return cba.CallAudit(**base)


def test_aggregate_batch_rates_and_counts() -> None:
    audits = [
        _audit(outcome_bucket="engaged", reached_human=True, reached_owner=True,
               caller_number_id="A"),
        _audit(outcome_bucket="gatekeeper", reached_human=True, was_gatekeeper=True,
               caller_number_id="B"),
        _audit(outcome_bucket="live_hangup", time_to_hangup_s=10.0,
               caller_number_id="A"),
        _audit(outcome_bucket="voicemail", caller_number_id="B",
               reasoning_leak_detected=True, reasoning_leak_phrases=["p"]),
    ]
    m = cba.aggregate_batch(audits, batch_id="b1")
    assert m.total_calls == 4
    assert m.engagement_rate == 0.25          # 1 engaged / 4
    assert m.live_contact_rate == 0.5         # 2 reached_human / 4
    assert m.gatekeeper_count == 1
    assert m.reasoning_leak_count == 1
    assert m.reasoning_leak_phrases == {"p": 1}
    assert m.median_time_to_hangup_s == 10.0
    # two numbers, 2 each => perfectly even => skew 0
    assert m.caller_id_skew == 0.0


def test_aggregate_empty_batch() -> None:
    m = cba.aggregate_batch([], batch_id="empty")
    assert m.total_calls == 0
    assert m.engagement_rate == 0.0


def test_distribution_skew() -> None:
    from collections import Counter

    assert cba._distribution_skew(Counter()) == 0.0
    assert cba._distribution_skew(Counter({"A": 5})) == 0.0            # single number
    assert cba._distribution_skew(Counter({"A": 5, "B": 5})) == 0.0    # even
    assert cba._distribution_skew(Counter({"A": 10, "B": 0})) == 1.0   # all on one


# ---------------------------------------------------------------------------
# Cross-batch trend
# ---------------------------------------------------------------------------
def _metrics(**kw) -> cba.BatchMetrics:
    base = dict(batch_id="b", analyzed_ts="2026-07-02T00:00:00Z", total_calls=10)
    base.update(kw)
    return cba.BatchMetrics(**base)


def test_trend_first_batch_flags_leak() -> None:
    cur = _metrics(reasoning_leak_count=2)
    tr = cba.compute_trend(cur, None)
    assert tr.has_prior is False
    assert any("reasoning_leak_count" in r for r in tr.regressions)


def test_trend_improved_and_regressed() -> None:
    prior = _metrics(engagement_rate=0.20, median_time_to_hangup_s=10.0)
    cur = _metrics(engagement_rate=0.35, median_time_to_hangup_s=8.0)
    tr = cba.compute_trend(cur, prior)
    assert tr.has_prior is True
    dirs = {d.metric: d.direction for d in tr.deltas}
    assert dirs["engagement_rate"] == "improved"          # higher is better
    assert dirs["median_time_to_hangup_s"] == "regressed"  # longer is better


def test_trend_hardguard_regresses_even_when_flat() -> None:
    # leak count unchanged at 1 across both batches — still a regression because
    # the absolute hard-guard must be 0.
    prior = _metrics(reasoning_leak_count=1)
    cur = _metrics(reasoning_leak_count=1)
    tr = cba.compute_trend(cur, prior)
    assert any("reasoning_leak_count" in r for r in tr.regressions)


# ---------------------------------------------------------------------------
# Next-remediation ranking
# ---------------------------------------------------------------------------
def test_remediation_hardguard_dominates() -> None:
    cur = _metrics(
        reasoning_leak_count=1,
        reasoning_leak_phrases={"i need to check my protocol": 1},
        live_contact_rate=0.10,   # also weak, but must rank below the hard-guard
    )
    tr = cba.compute_trend(cur, None)
    rems = cba.next_remediations(cur, tr)
    assert rems, "expected at least one remediation"
    assert rems[0].metric == "reasoning_leak_count"
    assert rems[0].severity == 100


def test_remediation_dedup_and_cap() -> None:
    cur = _metrics(
        live_contact_rate=0.10,
        median_time_to_hangup_s=8.0,
        gatekeeper_count=3,
        gatekeeper_handoff_success_rate=0.10,
        caller_id_skew=0.9,
    )
    tr = cba.compute_trend(cur, None)
    rems = cba.next_remediations(cur, tr, limit=3)
    assert len(rems) <= 3
    metrics = [r.metric for r in rems]
    assert len(metrics) == len(set(metrics))  # de-duped by metric
    # sorted by severity descending
    sev = [r.severity for r in rems]
    assert sev == sorted(sev, reverse=True)


# ---------------------------------------------------------------------------
# Durable store round-trip (isolated tmp root)
# ---------------------------------------------------------------------------
def test_store_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cba.storage, "root", lambda: tmp_path)
    m1 = _metrics(batch_id="batchA", engagement_rate=0.2)
    m2 = _metrics(batch_id="batchB", engagement_rate=0.3)
    cba.append_batch(m1)
    cba.append_batch(m2)

    loaded = cba.load_batches()
    assert [b.batch_id for b in loaded] == ["batchA", "batchB"]

    # prior-before-B is A; prior with no filter is the latest (B)
    assert cba.load_prior_batch(before_batch_id="batchB").batch_id == "batchA"
    assert cba.load_prior_batch().batch_id == "batchB"

    # store file is valid JSONL
    store = tmp_path / "voice" / "batch_analyses.jsonl"
    lines = [l for l in store.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["batch_id"] == "batchA"


def test_load_batches_skips_corrupt_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cba.storage, "root", lambda: tmp_path)
    store = tmp_path / "voice" / "batch_analyses.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"batch_id": "ok", "analyzed_ts": "t", "total_calls": 1})
    store.write_text(good + "\n{ this is not json }\n", encoding="utf-8")
    loaded = cba.load_batches()
    assert [b.batch_id for b in loaded] == ["ok"]


# ---------------------------------------------------------------------------
# End-to-end analyze_batch with an INJECTED fake client (no network, read-only)
# ---------------------------------------------------------------------------
class _FakeVapiClient:
    """Records which methods were called so the test can assert read-only."""

    def __init__(self, calls):
        self._calls = calls
        self.mutating_calls = 0

    def list_calls(self, *, limit: int = 10):
        return list(self._calls)

    # Any of these being invoked would be a read-only violation.
    def create_call(self, *a, **k):  # pragma: no cover - must never run
        self.mutating_calls += 1
        raise AssertionError("analyzer must not place calls")

    def _patch(self, *a, **k):  # pragma: no cover - must never run
        self.mutating_calls += 1
        raise AssertionError("analyzer must not PATCH")


def test_analyze_batch_readonly_and_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cba.storage, "root", lambda: tmp_path)

    calls = [
        {"id": "c1", "endedReason": "assistant-ended-call", "transcript": ENGAGED_TX,
         "phoneNumberId": "A", "cost": 0.1},
        {"id": "c2", "endedReason": "voicemail", "transcript": GATEKEEPER_TX,
         "phoneNumberId": "B", "cost": 0.05},
        {"id": "c99", "endedReason": "customer-ended-call",
         "transcript": "User: no.\nAI: bye", "phoneNumberId": "A", "cost": 0.02},
    ]
    fake = _FakeVapiClient(calls)

    result = cba.analyze_batch(client=fake, since_ts=None, dial_run_id=None,
                               persist=True)
    assert fake.mutating_calls == 0
    assert result.metrics.total_calls == 3
    # gatekeeper call reclassified
    assert result.metrics.gatekeeper_count == 1
    # persisted to the isolated store
    store = tmp_path / "voice" / "batch_analyses.jsonl"
    assert store.exists()
    assert len([l for l in store.read_text().splitlines() if l.strip()]) == 1


def test_analyze_batch_scopes_to_dial_run_placed_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cba.storage, "root", lambda: tmp_path)
    # write a dial-run artifact that only PLACED c1
    dial_run = {
        "run_id": "run-xyz",
        "attempts": [
            {"outcome": "initiated", "call_id": "c1",
             "callsheet_finding": "no working website found"},
            {"outcome": "skipped", "call_id": "c2"},
        ],
    }
    art = tmp_path / "dial_run_run-xyz.json"
    art.write_text(json.dumps(dial_run), encoding="utf-8")

    calls = [
        {"id": "c1", "endedReason": "assistant-ended-call", "transcript": ENGAGED_TX,
         "phoneNumberId": "A"},
        {"id": "c2", "endedReason": "voicemail", "transcript": GATEKEEPER_TX,
         "phoneNumberId": "B"},
    ]
    fake = _FakeVapiClient(calls)
    result = cba.analyze_batch(client=fake, dial_run_id=str(art), persist=False)
    # only c1 was placed => batch scoped to 1 call
    assert result.metrics.total_calls == 1
    assert result.batch_id == "run-xyz"


# ---------------------------------------------------------------------------
# Report render smoke — never raises, contains the headline sections
# ---------------------------------------------------------------------------
def test_render_report_smoke() -> None:
    audits = [
        cba.analyze_call({"id": "c1", "endedReason": "assistant-ended-call",
                          "transcript": ENGAGED_TX, "phoneNumberId": "A"}),
    ]
    m = cba.aggregate_batch(audits, batch_id="b1")
    tr = cba.compute_trend(m, None)
    result = cba.BatchResult(
        batch_id="b1", metrics=m, audits=audits, trend=tr,
        remediations=cba.next_remediations(m, tr),
        learning_notes=cba.surface_learning_notes(audits),
    )
    text = cba.render_report(result)
    assert "CALL-BATCH AUDIT: b1" in text
    assert "-- metrics --" in text
    assert "next remediation" in text


# ---------------------------------------------------------------------------
# Autonomous audit — the operator-out-of-the-loop capability
# ---------------------------------------------------------------------------
def test_autonomous_audit_persists_reports_and_dedups(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cba.storage, "root", lambda: tmp_path)
    calls = [
        {"id": "c1", "endedReason": "assistant-ended-call", "transcript": ENGAGED_TX,
         "phoneNumberId": "A"},
        {"id": "c2", "endedReason": "voicemail", "transcript": GATEKEEPER_TX,
         "phoneNumberId": "B"},
    ]
    r1 = cba.autonomous_audit(client=_FakeVapiClient(calls))
    assert r1 is not None and r1.metrics.total_calls == 2
    assert (tmp_path / "voice" / "batch_audits" / "latest.txt").exists()
    store = tmp_path / "voice" / "batch_analyses.jsonl"
    assert len([l for l in store.read_text().splitlines() if l.strip()]) == 1
    # second sweep, same call count -> dedup, no new trend row
    cba.autonomous_audit(client=_FakeVapiClient(calls))
    assert len([l for l in store.read_text().splitlines() if l.strip()]) == 1


def test_autonomous_audit_alerts_on_leak(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cba.storage, "root", lambda: tmp_path)
    calls = [{"id": "c1", "endedReason": "assistant-ended-call",
              "transcript": f"User: hi\nAI: {LEAK_PHRASE}", "phoneNumberId": "A"}]
    r = cba.autonomous_audit(client=_FakeVapiClient(calls))
    assert r.metrics.reasoning_leak_count == 1
    alerts = list((tmp_path / "voice" / "audit_alerts").glob("alert_*.json"))
    assert len(alerts) == 1
    data = json.loads(alerts[0].read_text())
    assert data["reasoning_leak_count"] == 1
    assert data["severity"] == "P0"
    assert data["next_remediation"]  # non-empty
    # actionable: names the offending call so a reader can tell stale from fresh
    assert data["offending_calls"]
    assert data["offending_calls"][0]["call_id"] == "c1"


def test_autonomous_audit_no_alert_when_clean(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cba.storage, "root", lambda: tmp_path)
    calls = [{"id": "c1", "endedReason": "assistant-ended-call",
              "transcript": ENGAGED_TX, "phoneNumberId": "A"}]
    cba.autonomous_audit(client=_FakeVapiClient(calls))
    assert not (tmp_path / "voice" / "audit_alerts").exists() or \
        not list((tmp_path / "voice" / "audit_alerts").glob("alert_*.json"))


def test_autonomous_audit_never_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cba.storage, "root", lambda: tmp_path)

    def _boom(**k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cba, "analyze_batch", _boom)
    assert cba.autonomous_audit(client=None) is None
