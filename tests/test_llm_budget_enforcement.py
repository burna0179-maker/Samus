"""HOTL Tranche 1 enforcement hooks on the LLM budget store.

Covers ``set_quota_override`` (TTL-expiring, clamped to [0.25x, 2.0x] base)
and ``freeze_nonessential`` (stack-wide TTL freeze that essential workcells
survive). Same tmp-JSON-backend pattern as tests/test_llm_budget.py.
"""
from __future__ import annotations

from backend.common import llm_budget as mod
from backend.common.llm_budget import (
    ESSENTIAL_WORKCELLS,
    LlmBudgetStore,
)


class _Clock:
    def __init__(self, t: float = 1_750_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _store(tmp_path, clock: _Clock | None = None, **overrides) -> LlmBudgetStore:
    kwargs = dict(
        base_token_budget=100_000,
        ema_alpha=0.5,
        floor_pct=0.10,
        ddb_table=None,
        json_path=str(tmp_path / "budget.json"),
        now_func=clock,
    )
    kwargs.update(overrides)
    return LlmBudgetStore(**kwargs)


# ---------------------------------------------------------------------------
# set_quota_override
# ---------------------------------------------------------------------------

def test_override_supersedes_adaptive_quota(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.set_quota_override("prospecting", 40_000, ttl_seconds=3600)
    d = s.can_spend("prospecting", est_tokens=1)
    assert d.quota == 40_000


def test_override_clamped_to_min_quarter_of_base(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.set_quota_override("prospecting", 1, ttl_seconds=3600)
    d = s.can_spend("prospecting", est_tokens=1)
    assert d.quota == 25_000  # 0.25 * 100_000


def test_override_clamped_to_max_double_base(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.set_quota_override("prospecting", 10_000_000, ttl_seconds=3600)
    d = s.can_spend("prospecting", est_tokens=1)
    assert d.quota == 200_000  # 2.0 * 100_000


def test_override_expires_after_ttl(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.set_quota_override("prospecting", 40_000, ttl_seconds=60)
    assert s.can_spend("prospecting", est_tokens=1).quota == 40_000
    clock.t += 61
    # expired -> back to the adaptive quota (base, since no signal yet)
    assert s.can_spend("prospecting", est_tokens=1).quota == 100_000


def test_override_denies_spend_beyond_reduced_quota(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.set_quota_override("prospecting", 25_000, ttl_seconds=3600)
    d = s.can_spend("prospecting", est_tokens=30_000)
    assert d.allowed is False
    assert "budget_exceeded" in (d.reason or "")


def test_clear_quota_override(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.set_quota_override("prospecting", 40_000, ttl_seconds=3600)
    s.clear_quota_override("prospecting")
    assert s.can_spend("prospecting", est_tokens=1).quota == 100_000


def test_override_persists_across_store_instances(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.set_quota_override("prospecting", 40_000, ttl_seconds=3600)
    s2 = _store(tmp_path, clock)
    assert s2.can_spend("prospecting", est_tokens=1).quota == 40_000


def test_override_ttl_clamped_to_24h(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.set_quota_override("prospecting", 40_000, ttl_seconds=10 * 24 * 3600)
    clock.t += 24 * 3600 + 1
    assert s.can_spend("prospecting", est_tokens=1).quota == 100_000


# ---------------------------------------------------------------------------
# freeze_nonessential
# ---------------------------------------------------------------------------

def test_freeze_denies_nonessential_workcell(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    until = s.freeze_nonessential(ttl_seconds=600)
    assert until
    d = s.can_spend("prospecting", est_tokens=1)
    assert d.allowed is False
    assert (d.reason or "").startswith("nonessential_frozen_until_")


def test_freeze_spares_essential_workcells(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.freeze_nonessential(ttl_seconds=600)
    for wc in sorted(ESSENTIAL_WORKCELLS):
        assert s.can_spend(wc, est_tokens=1).allowed is True


def test_freeze_lifts_after_ttl(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.freeze_nonessential(ttl_seconds=60)
    assert s.can_spend("prospecting", est_tokens=1).allowed is False
    clock.t += 61
    assert s.can_spend("prospecting", est_tokens=1).allowed is True


def test_freeze_persists_across_store_instances(tmp_path):
    clock = _Clock()
    s = _store(tmp_path, clock)
    s.freeze_nonessential(ttl_seconds=600)
    s2 = _store(tmp_path, clock)
    assert s2.can_spend("prospecting", est_tokens=1).allowed is False


def test_unfrozen_store_reports_none(tmp_path):
    s = _store(tmp_path, _Clock())
    assert s.nonessential_frozen_until() is None


# ---------------------------------------------------------------------------
# module-level wrappers hit the process store
# ---------------------------------------------------------------------------

def test_module_level_wrappers(tmp_path, monkeypatch):
    clock = _Clock()
    s = _store(tmp_path, clock)
    monkeypatch.setattr(mod, "get_store", lambda: s)
    mod.set_quota_override("prospecting", 40_000, 3600)
    assert s.can_spend("prospecting", est_tokens=1).quota == 40_000
    mod.freeze_nonessential(600)
    assert s.can_spend("prospecting", est_tokens=1).allowed is False


def test_legacy_row_without_new_fields_loads(tmp_path):
    """Pre-HOTL JSON rows (missing new fields) must still load."""
    import json

    path = tmp_path / "budget.json"
    path.write_text(json.dumps({
        "prospecting": {
            "bucket_day": "2020-01-01",
            "total_tokens_today": 5,
            "efficiency_ema": 0.9,
        }
    }), encoding="utf-8")
    s = _store(tmp_path, _Clock())
    b = s.snapshot("prospecting")
    assert b.quota_override == 0
    assert b.freeze_nonessential_until == ""
