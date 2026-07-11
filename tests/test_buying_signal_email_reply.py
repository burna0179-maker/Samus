"""Email-reply enrollment counterpart to the voice buying-signal route.

Mirrors :mod:`tests.test_buying_signal_route` — same flag, same store,
same idempotency guarantees, but the input is free-text email instead of
a structured Vapi lead_summary.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.common.config as config_mod
import backend.outreach.buying_signal_route as bsr


def _fake_get_settings(*, enabled: bool, threshold: int = 70):
    def _gs():
        return SimpleNamespace(
            outreach_buying_signal_route_enabled=enabled,
            outreach_buying_signal_intent_threshold=threshold,
        )
    _gs.cache_clear = lambda: None
    return _gs


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(enabled=True))
    return tmp_path


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply", [
    "Yes — let's book a call this week.",
    "What's the price for the full buildout?",
    "Sign me up, ready to start.",
    "Sounds great, what are the next steps?",
    "Very interested — send me the quote.",
])
def test_email_reply_classifier_positive(reply):
    qualifies, score, hits = bsr.is_email_reply_buying_signal(reply)
    assert qualifies, f"expected buying signal: {reply} (score={score}, hits={hits})"
    assert score >= 70
    assert hits


@pytest.mark.parametrize("reply", [
    "Not interested, please remove me from your list.",
    "Unsubscribe.",
    "Please stop emailing us.",
    # Strong positive but trumped by hard-no
    "We're not interested, please don't contact us again.",
])
def test_email_reply_classifier_hard_no_vetoes(reply):
    qualifies, score, hits = bsr.is_email_reply_buying_signal(reply)
    assert not qualifies
    assert score == 0
    assert any(h.startswith("hard_no:") for h in hits)


@pytest.mark.parametrize("reply", [
    "",                              # empty
    "ok",                            # too short
    "Thanks for reaching out, we'll think about it.",  # neutral
])
def test_email_reply_classifier_below_threshold(reply):
    qualifies, _score, _hits = bsr.is_email_reply_buying_signal(reply)
    assert not qualifies


# ---------------------------------------------------------------------------
# Enrollment — gating, idempotency, persistence shape
# ---------------------------------------------------------------------------

def test_email_reply_enroll_no_op_when_flag_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(config_mod, "get_settings", _fake_get_settings(enabled=False))
    out = bsr.maybe_enroll_buying_signal_from_email_reply(
        prospect_id="p1",
        reply_text="Yes, ready to start — let's book a call.",
        now_iso="2026-06-30T00:00:00Z",
    )
    assert out["enrolled"] is False
    assert out["reason"] == "route_disabled"
    assert bsr._read() == []


def test_email_reply_enroll_skips_non_signal(armed):
    out = bsr.maybe_enroll_buying_signal_from_email_reply(
        prospect_id="p1", reply_text="Thanks but we're all set.",
        now_iso="2026-06-30T00:00:00Z",
    )
    assert out["enrolled"] is False
    assert out["reason"] == "not_a_buying_signal"
    assert bsr._read() == []


def test_email_reply_enroll_writes_record_with_source_tag(armed):
    out = bsr.maybe_enroll_buying_signal_from_email_reply(
        prospect_id="pr_kelly",
        reply_text="Very interested — let's set up a call to scope the workflow.",
        now_iso="2026-06-30T00:00:00Z",
        email="kelly@example.com",
        company="Kelly Zimmerman, eXp Realty",
    )
    assert out["enrolled"] is True
    assert out["score"] >= 70
    rec = bsr._read()[0]
    assert rec["prospect_id"] == "pr_kelly"
    assert rec["sequence_id"] == "buying_signal"
    assert rec["email"] == "kelly@example.com"
    assert rec["source"] == "email_reply"


def test_email_reply_enroll_is_idempotent(armed):
    kw = dict(
        prospect_id="pr_kelly",
        reply_text="Yes please, let's get on a call.",
        email="kelly@example.com",
        company="Kelly Zimmerman, eXp Realty",
    )
    first = bsr.maybe_enroll_buying_signal_from_email_reply(
        now_iso="2026-06-30T00:00:00Z", **kw)
    assert first["enrolled"] is True
    second = bsr.maybe_enroll_buying_signal_from_email_reply(
        now_iso="2026-06-30T01:00:00Z", **kw)
    assert second["enrolled"] is False
    assert second["reason"] == "already_enrolled"
    assert len(bsr._read()) == 1


def test_email_reply_operator_override_skips_classifier(armed):
    """Operator override enrolls without a classifier hit — used when the
    signal arrived through a non-text channel (verbal confirmation, etc.)."""
    out = bsr.maybe_enroll_buying_signal_from_email_reply(
        prospect_id="pr_kelly",
        reply_text="",  # empty — would normally fail the classifier
        now_iso="2026-06-30T00:00:00Z",
        email="kelly@example.com",
        company="Kelly Zimmerman, eXp Realty",
        operator_override=True,
    )
    assert out["enrolled"] is True
    rec = bsr._read()[0]
    assert rec["source"] == "email_reply_operator"
    assert rec["intent_score"] == 90  # fixed override score


# ---------------------------------------------------------------------------
# Cold-list exclusion lookup
# ---------------------------------------------------------------------------

def test_active_warm_prospect_ids_returns_active_only(armed):
    # Enroll one, then mark a second as completed via direct store edit.
    bsr.maybe_enroll_buying_signal_from_email_reply(
        prospect_id="p_active", reply_text="Yes — book a call please.",
        now_iso="2026-06-30T00:00:00Z", email="a@x.com",
    )
    records = bsr._read()
    records.append({
        "prospect_id": "p_done", "sequence_id": "buying_signal",
        "status": "completed", "started_at": "2026-06-01T00:00:00Z",
        "completed_steps": [1, 2, 3], "events": [],
    })
    bsr._write(records)
    ids = bsr.active_warm_prospect_ids()
    assert ids == {"p_active"}


def test_active_warm_prospect_ids_empty_when_store_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    # No store file created.
    assert bsr.active_warm_prospect_ids() == set()
