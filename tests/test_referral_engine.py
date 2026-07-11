"""Tests for backend.referral.engine — codes, trigger, attribution, rewards."""
from __future__ import annotations

import json
from pathlib import Path

from backend.referral.engine import (
    build_link,
    compute_rewards,
    generate_code,
    qualify_referral,
    record_referral,
    should_offer_referral,
)
from backend.referral.models import ReferralProgram

_PROGRAM = ReferralProgram()
_NOW = "2026-06-04T12:00:00+00:00"
_FORBIDDEN = set("01OI")


def test_generate_code_deterministic_and_clean():
    c1 = generate_code("cust_42", _PROGRAM)
    c2 = generate_code("cust_42", _PROGRAM)
    assert c1 == c2  # deterministic
    assert len(c1) == 8
    assert not (set(c1) & _FORBIDDEN)  # no ambiguous chars
    assert generate_code("cust_99", _PROGRAM) != c1  # different customer


def test_build_link():
    link = build_link("cust_42", _PROGRAM)
    assert link.startswith("https://hustleforge.ai/?ref=")
    assert generate_code("cust_42", _PROGRAM) in link


def test_should_offer_referral_on_first_result():
    assert should_offer_referral(["first_result"], already_offered=False) is True
    assert should_offer_referral(["aha_moment"], already_offered=False) is True


def test_should_offer_referral_gated_and_irrelevant():
    assert should_offer_referral(["first_result"], already_offered=True) is False
    assert should_offer_referral(["signup", "billing"], already_offered=False) is False
    assert should_offer_referral([], already_offered=False) is False


def test_record_referral_pending_and_ledger(tmp_path):
    ledger = tmp_path / "ref.jsonl"
    ref = record_referral("ABCD2345", "cust_42", "lead_7", _PROGRAM, now_iso=_NOW, ledger_path=ledger)
    assert ref.status == "pending"
    assert ref.referrer_id == "cust_42" and ref.referee_id == "lead_7"
    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "pending"


def test_self_referral_rejected(tmp_path):
    ref = record_referral("ABCD2345", "cust_42", "cust_42", _PROGRAM, now_iso=_NOW, ledger_path=tmp_path / "r.jsonl")
    assert ref.status == "rejected"


def test_qualify_referral_on_matching_event(tmp_path):
    ref = record_referral("C", "r1", "e1", _PROGRAM, now_iso=_NOW, ledger_path=tmp_path / "r.jsonl")
    out = qualify_referral(ref, "subscription_started", _PROGRAM, now_iso=_NOW, ledger_path=tmp_path / "r.jsonl")
    assert out.status == "qualified"
    assert out.referrer_reward == _PROGRAM.referrer_reward
    assert out.referee_reward == _PROGRAM.referee_reward
    assert out.qualified_at == _NOW


def test_qualify_referral_wrong_event_noop(tmp_path):
    ref = record_referral("C", "r1", "e1", _PROGRAM, now_iso=_NOW, ledger_path=tmp_path / "r.jsonl")
    out = qualify_referral(ref, "opened_email", _PROGRAM, now_iso=_NOW, persist=False)
    assert out.status == "pending"


def test_qualify_referral_rejected_stays_rejected():
    ref = record_referral("C", "r1", "r1", _PROGRAM, now_iso=_NOW, persist=False)  # self -> rejected
    out = qualify_referral(ref, "subscription_started", _PROGRAM, now_iso=_NOW, persist=False)
    assert out.status == "rejected"  # non-pending -> unchanged


def test_compute_rewards():
    ref = record_referral("C", "r1", "e1", _PROGRAM, now_iso=_NOW, persist=False)
    assert compute_rewards(ref, _PROGRAM) == {}  # pending -> nothing owed
    qualify_referral(ref, "subscription_started", _PROGRAM, now_iso=_NOW, persist=False)
    rewards = compute_rewards(ref, _PROGRAM)
    assert rewards["referrer_id"] == "r1"
    assert rewards["referee_id"] == "e1"
    assert rewards["referrer_reward"] == _PROGRAM.referrer_reward
    assert rewards["referee_reward"] == _PROGRAM.referee_reward
