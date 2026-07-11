"""Economic task arbiter — bids, ranking, collectors, ledger, channel bandit."""
from __future__ import annotations

import json

import pytest

from backend.strategy import arbiter
from backend.strategy.arbiter import (
    ArbitrationResult,
    WorkBid,
    arbitrate_daily,
    collect_campaign_bids,
    collect_crm_task_bids,
    collect_follow_up_bids,
    collect_prospect_bids,
    collect_reengagement_bids,
    collect_seo_bids,
    latest_arbitration,
    rank_bids,
)
from backend.strategy.portfolio_manager import (
    CHANNELS,
    get_channel_bandit_stats,
    reset_bandit,
    select_best_channel,
    update_channel_bandit,
)


def _bid(**over) -> WorkBid:
    base = dict(
        action="email_prospect",
        target_id="p_1",
        ev_usd=1000.0,
        probability=0.5,
        urgency=1.0,
        cost_usd=0.5,
        time_estimate_hrs=1.0,
        source_workcell="outreach",
        channel="email",
    )
    base.update(over)
    return WorkBid(**base)


# ---------------------------------------------------------------------------
# WorkBid priority + ranking
# ---------------------------------------------------------------------------

def test_bid_priority_matches_hand_computed_formula():
    # (1000 * 0.5 * 1.0) / (1.0 * 0.5) = 1000
    assert _bid().priority == pytest.approx(1000.0)


def test_rank_bids_orders_by_priority_desc():
    low = _bid(target_id="low", ev_usd=100.0)          # 100
    mid = _bid(target_id="mid", ev_usd=500.0)          # 500
    high = _bid(target_id="high", ev_usd=5000.0)       # 5000
    ranked = rank_bids([mid, low, high])
    assert [b.target_id for b in ranked] == ["high", "mid", "low"]


def test_rank_bids_tie_breaks_on_ev():
    # Same priority (both 1000), a has bigger EV via cheaper cost inverse.
    a = _bid(target_id="a", ev_usd=2000.0, cost_usd=1.0)   # 2000*0.5/1 = 1000
    b = _bid(target_id="b", ev_usd=1000.0, cost_usd=0.5)   # 1000*0.5/0.5 = 1000
    ranked = rank_bids([b, a])
    assert [x.target_id for x in ranked] == ["a", "b"]


def test_bid_to_record_includes_priority():
    rec = _bid().to_record()
    assert rec["priority"] == pytest.approx(1000.0)
    assert rec["action"] == "email_prospect"


# ---------------------------------------------------------------------------
# Collectors degrade to empty when data sources are absent
# ---------------------------------------------------------------------------

def test_collectors_degrade_to_empty_without_data_sources(monkeypatch, tmp_path):
    # No AWS in tests -> CRM scans error out -> empty lists, never raises.
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))  # empty reengagement ledger
    assert collect_crm_task_bids() == []
    assert collect_follow_up_bids() == []
    assert collect_seo_bids() == []
    assert collect_reengagement_bids() == []
    # Slice C collectors also degrade to empty when their sources fault.
    assert collect_prospect_bids() == []


def test_collect_reengagement_bids_reads_queued_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    from backend.common.dates import iso_now
    ledger = tmp_path / "crm" / "reengagement_queued.jsonl"
    ledger.parent.mkdir(parents=True)
    rows = [
        {"ts": iso_now(), "prospect_id": "p_re1", "opportunity_id": "op_9", "task_id": "t1"},
        {"ts": iso_now(), "prospect_id": "p_re1", "opportunity_id": "op_9", "task_id": "t2"},  # dup pid
        {"ts": "2020-01-01T00:00:00Z", "prospect_id": "p_old", "opportunity_id": "", "task_id": "t3"},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    bids = collect_reengagement_bids(window_days=7)
    assert len(bids) == 1  # deduped by prospect, old row outside window
    bid = bids[0]
    assert bid.target_id == "p_re1"
    assert bid.action == "reengagement_touch"
    assert bid.channel == "retention"
    assert bid.metadata["opportunity_id"] == "op_9"


# ---------------------------------------------------------------------------
# arbitrate_daily — ranked queue + ledger + decision records
# ---------------------------------------------------------------------------

@pytest.fixture()
def _arb_ledger(monkeypatch, tmp_path):
    path = tmp_path / "arbitration.jsonl"
    monkeypatch.setenv("SAMUS_ARBITRATION_LOG", str(path))
    return path


def test_arbitrate_daily_with_injected_bids(_arb_ledger):
    bids = [
        _bid(target_id="small", ev_usd=100.0),
        _bid(target_id="big", ev_usd=9000.0),
    ]
    # These tests exercise pure ranking mechanics — cash posture is tested
    # separately in the Slice C section below. Opt out of affordability
    # gating here so ordering is independent of the test environment's
    # finance stub.
    result = arbitrate_daily(bids, day="2026-07-05", apply_affordability=False)
    assert isinstance(result, ArbitrationResult)
    assert result.bid_count == 2
    assert [r["target_id"] for r in result.ranked] == ["big", "small"]
    assert [r["rank"] for r in result.ranked] == [1, 2]
    # DecisionRecord-style rationale on every ranked bid.
    decision = result.ranked[0]["decision"]
    assert decision["priority"] == pytest.approx(9000.0)
    assert "expected_outcome" in decision and "why" in decision
    # Persisted to the JSONL ledger.
    assert _arb_ledger.exists()
    row = json.loads(_arb_ledger.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["kind"] == "arbitration"
    assert row["bid_count"] == 2


def test_latest_arbitration_reads_back_last_run(_arb_ledger):
    arbitrate_daily([_bid(target_id="first")], day="2026-07-04", apply_affordability=False)
    arbitrate_daily([_bid(target_id="second")], day="2026-07-05", apply_affordability=False)
    latest = latest_arbitration()
    assert latest is not None
    assert latest["day"] == "2026-07-05"
    assert latest["ranked"][0]["target_id"] == "second"


def test_latest_arbitration_none_when_no_history(_arb_ledger):
    assert latest_arbitration() is None


def test_arbitrate_daily_emits_decision_made_via_shim(_arb_ledger, monkeypatch):
    emitted: list[tuple[str, dict]] = []

    def fake_emit(event_type, **kwargs):
        emitted.append((event_type, kwargs))
        return {}

    monkeypatch.setattr(arbiter, "emit_business_event", fake_emit)
    arbitrate_daily([_bid()], day="2026-07-05", apply_affordability=False)
    assert len(emitted) == 1
    event_type, kwargs = emitted[0]
    assert event_type == "decision.made"
    assert kwargs["workcell"] == "strategy"
    assert kwargs["metadata"]["decision_kind"] == "daily_arbitration"
    assert kwargs["metadata"]["top_target"] == "p_1"


def test_arbitrate_daily_empty_sources_yields_empty_queue(_arb_ledger, monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    result = arbitrate_daily(day="2026-07-05", apply_affordability=False)   # collectors all degrade
    assert result.bid_count == 0
    assert result.ranked == []


def test_arbitrate_daily_persist_false_skips_ledger(_arb_ledger):
    arbitrate_daily(
        [_bid()], day="2026-07-05", persist=False, apply_affordability=False,
    )
    assert not _arb_ledger.exists()


# ---------------------------------------------------------------------------
# Channel bandit (parallel arm dimension: channel::<name>)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_bandit():
    reset_bandit()
    yield
    reset_bandit()


def test_channel_arms_namespaced_no_schema_change():
    update_channel_bandit("email", 1.0)
    from backend.strategy.portfolio_manager import get_bandit_stats
    stats = get_bandit_stats()
    assert "channel::email" in stats
    assert stats["channel::email"]["trials"] == 1
    assert stats["channel::email"]["wins"] == pytest.approx(1.0)


def test_select_best_channel_explores_unseen_channels_first():
    # Two channels tried; the two never-tried ones score inf -> one of them wins.
    update_channel_bandit("email", 1.0)
    update_channel_bandit("call", 0.0)
    assert select_best_channel() in ("seo", "retention")


def test_select_best_channel_exploits_the_winner_once_all_tried():
    # Enough trials everywhere that UCB1's exploration bonus can no longer
    # outweigh email's perfect mean reward.
    for _ in range(10):
        for channel in CHANNELS:
            update_channel_bandit(channel, 0.0)
    for _ in range(10):
        update_channel_bandit("email", 1.0)
    assert select_best_channel() == "email"


def test_select_best_channel_respects_candidate_narrowing():
    for channel in CHANNELS:
        update_channel_bandit(channel, 0.0)
    for _ in range(20):
        update_channel_bandit("call", 1.0)
    # "call" excluded (e.g. outside business hours) -> next best of the rest.
    assert select_best_channel(("email", "seo", "retention")) != "call"


def test_get_channel_bandit_stats_only_channel_arms_bare_keys():
    update_channel_bandit("seo", 0.5)
    from backend.strategy.portfolio_manager import update_bandit
    update_bandit("plumbing", 1.0)  # flat industry arm must not leak in
    stats = get_channel_bandit_stats()
    assert set(stats) == {"seo"}
    assert stats["seo"]["wins"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Slice C — prospect + campaign collectors + affordability gating
# ---------------------------------------------------------------------------

class _StubOpportunity:
    """Duck-typed opportunity for the CRM stub — just what priority_score reads."""

    def __init__(self, *, opportunity_id, prospect_id, stage, deal_size_usd,
                 close_probability, updated_at, industry="tech"):
        self.opportunity_id = opportunity_id
        self.prospect_id = prospect_id
        self.stage = stage
        self.deal_size_usd = deal_size_usd
        self.close_probability = close_probability
        self.updated_at = updated_at
        self.industry = industry
        self.seo_score = 0  # kept out of the way

    def model_dump(self) -> dict:
        return {
            "opportunity_id": self.opportunity_id,
            "prospect_id": self.prospect_id,
            "stage": self.stage,
            "deal_size_usd": self.deal_size_usd,
            "close_probability": self.close_probability,
            "updated_at": self.updated_at,
            "industry": self.industry,
        }


class _StubOpportunityList:
    def __init__(self, rows):
        self.opportunities = rows


def test_collect_prospect_bids_skips_closed_and_uses_priority_score(monkeypatch):
    """Every non-closed opportunity becomes one 'advance_prospect' bid."""
    from backend.crm import service as crm_service

    hot = _StubOpportunity(
        opportunity_id="op1", prospect_id="p1", stage="proposal",
        deal_size_usd=5000.0, close_probability=0.5,
        updated_at="2026-07-05T00:00:00+00:00",
    )
    lost = _StubOpportunity(
        opportunity_id="op2", prospect_id="p2", stage="closed_lost",
        deal_size_usd=1000.0, close_probability=0.0,
        updated_at="2026-07-05T00:00:00+00:00",
    )
    monkeypatch.setattr(
        crm_service, "list_opportunities",
        lambda **_: _StubOpportunityList([hot, lost]),
    )

    bids = collect_prospect_bids()
    assert len(bids) == 1
    bid = bids[0]
    assert bid.action == "advance_prospect"
    assert bid.target_id == "p1"
    assert bid.channel == "email"
    assert bid.source_workcell == "crm"
    assert bid.ev_usd == pytest.approx(5000.0)


def test_collect_campaign_bids_only_eligible_and_carries_cost_tier(monkeypatch):
    """Campaign bids carry their own cost_tier in metadata for gating."""
    from backend.cash_engine import campaign_portfolio as cp

    class _StubCampaign:
        def __init__(self, cid, kind, priority, cost_tier, est_cost_usd, cap, eligible):
            self.campaign_id = cid
            self.kind = kind
            self.priority = priority
            self.cost_tier = cost_tier
            self.est_cost_usd = est_cost_usd
            self.default_cap = cap
            self.monitor_cost = 1.0
            self._eligible = eligible

        def is_eligible(self):
            return self._eligible

    class _StubDeps:
        def __init__(self, campaigns):
            self.campaigns = campaigns

    monkeypatch.setattr(
        cp, "default_portfolio_deps",
        lambda **_: _StubDeps([
            _StubCampaign("voice_free", "voice", 1.2, "free", 0.0, 5, True),
            _StubCampaign("email_low",  "email", 1.0, "low",  0.5, 10, True),
            _StubCampaign("ineligible", "email", 0.5, "low",  0.5, 10, False),  # skipped
        ]),
    )

    bids = collect_campaign_bids()
    ids = [b.target_id for b in bids]
    assert set(ids) == {"voice_free", "email_low"}
    tiers = {b.target_id: b.metadata["cost_tier"] for b in bids}
    assert tiers == {"voice_free": "free", "email_low": "low"}
    # Voice bids also enrich probability upward from the email baseline.
    voice_bid = next(b for b in bids if b.target_id == "voice_free")
    assert voice_bid.probability >= 0.08


def test_arbitrate_daily_gates_paid_bids_under_conserve_posture(
    _arb_ledger, monkeypatch,
):
    """Under 'conserve' posture, paid-tier bids move to held_by_affordability."""
    from backend.cash_engine import affordability as afford_mod

    class _Afford:
        posture = "conserve"
        allowed_tiers = frozenset({"free"})
        intensity_factor = 0.3
        marketing_budget_usd = 0.0
        headroom_usd = 0.0
        available_cash_usd = 0.0
        source = "test"

        def to_dict(self):
            return {
                "posture": self.posture,
                "allowed_tiers": sorted(self.allowed_tiers),
                "intensity_factor": self.intensity_factor,
                "marketing_budget_usd": self.marketing_budget_usd,
                "headroom_usd": self.headroom_usd,
                "available_cash_usd": self.available_cash_usd,
                "source": self.source,
            }

    monkeypatch.setattr(afford_mod, "assess_affordability", lambda: _Afford())

    seo_bid = _bid(target_id="seo_only", channel="seo")           # tier=free -> active
    email_bid = _bid(target_id="email_low", channel="email")      # tier=low -> held
    call_bid = _bid(target_id="call_paid", channel="call")        # tier=paid -> held

    result = arbitrate_daily(
        [seo_bid, email_bid, call_bid], day="2026-07-06",
    )
    assert result.bid_count == 1
    assert [r["target_id"] for r in result.ranked] == ["seo_only"]
    held_ids = [r["target_id"] for r in result.held_by_affordability]
    assert set(held_ids) == {"email_low", "call_paid"}
    # Every held bid carries the reason on its decision record.
    for held in result.held_by_affordability:
        assert "held_reason" in held["decision"]
    # Posture is surfaced.
    assert result.affordability["posture"] == "conserve"


def test_arbitrate_daily_permissive_when_affordability_read_fails(
    _arb_ledger, monkeypatch,
):
    """A finance outage must NOT turn arbitration into a blanket block."""
    from backend.cash_engine import affordability as afford_mod

    def _boom():
        raise RuntimeError("finance transport wedged")

    monkeypatch.setattr(afford_mod, "assess_affordability", _boom)

    result = arbitrate_daily(
        [_bid(target_id="only", channel="call")], day="2026-07-06",
    )
    # Fail-open: the bid stays active; nothing held.
    assert result.bid_count == 1
    assert result.held_by_affordability == []
    assert result.affordability is None


def test_arbitrate_daily_apply_affordability_false_skips_gating(_arb_ledger):
    """Explicit opt-out preserves the pre-Slice-C behavior for internal callers."""
    result = arbitrate_daily(
        [_bid(target_id="only", channel="call")],
        day="2026-07-06",
        apply_affordability=False,
    )
    assert result.bid_count == 1
    assert result.affordability is None
    assert result.held_by_affordability == []
