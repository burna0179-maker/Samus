"""Prospecting worker action router (ProspectingWorker.handle).

The worker class wraps BaseSqsWorker and is exercised by the worker_base
suite; the interesting business surface is the action router, which must
route the SQS-queue path to the SAME action set the /work HTTP endpoint
serves. Before this wiring the queue path only handled "discover" and
ValueError'd every intelligence action.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from backend.prospecting import worker as p_worker


@dataclass
class _FakeEnvelope:
    """Minimal stand-in for the worker_base envelope shape."""

    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = "t-worker-1"


def _new_worker():
    """Construct a ProspectingWorker without a live AWS runtime."""
    return p_worker.ProspectingWorker.__new__(p_worker.ProspectingWorker)


# ── intelligence actions now route on the queue path ────────────────────────


def test_handle_analyze_business():
    w = _new_worker()
    result = w.handle(_FakeEnvelope(
        action="analyze_business",
        payload={"website_url": "https://acme.example", "review_count": 3,
                 "rating": 4.7},
    ))
    assert set(result) == {"signals", "scores", "products", "pitch_angle"}
    assert result["signals"]["has_website"] is True


def test_handle_score_deal():
    w = _new_worker()
    result = w.handle(_FakeEnvelope(
        action="score_deal",
        payload={"intel": {"scores": {"website": 80}, "pitch_angle": "trust_gap"}},
    ))
    assert "tier" in result
    assert "probability" in result


def test_handle_generate_dynamic_script():
    w = _new_worker()
    result = w.handle(_FakeEnvelope(
        action="generate_dynamic_script",
        payload={"company_name": "Acme Co",
                 "intel": {"pitch_angle": "trust_gap"}},
    ))
    assert result["opener"]
    assert result["pitch_angle"] == "trust_gap"


def test_handle_generate_dynamic_script_with_pivot():
    w = _new_worker()
    result = w.handle(_FakeEnvelope(
        action="generate_dynamic_script_with_pivot",
        payload={"company_name": "Acme Co",
                 "intel": {"pitch_angle": "trust_gap"}},
    ))
    assert result["opener"]
    assert "pivot" in result


def test_handle_generate_dynamic_script_missing_company_raises():
    w = _new_worker()
    with pytest.raises(ValueError, match="company_name"):
        w.handle(_FakeEnvelope(
            action="generate_dynamic_script",
            payload={"intel": {"pitch_angle": "trust_gap"}},
        ))


def test_handle_generate_dynamic_script_missing_intel_raises():
    w = _new_worker()
    with pytest.raises(ValueError, match="intel"):
        w.handle(_FakeEnvelope(
            action="generate_dynamic_script",
            payload={"company_name": "Acme Co"},
        ))


def test_handle_unknown_action_raises():
    w = _new_worker()
    with pytest.raises(ValueError, match="unknown_action"):
        w.handle(_FakeEnvelope(action="not_a_real_action", payload={}))


# ── discover path still works (regression) ──────────────────────────────────


def test_handle_discover_routes_to_process_discovery(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_process(req, *, task_id=None):
        captured["task_id"] = task_id
        captured["campaign"] = req.campaign_name
        from backend.prospecting.models import DiscoveryResult
        return DiscoveryResult(
            campaign_name=req.campaign_name, prospect_count=0,
            csv_path="/tmp/x.csv", prospects=[], cache_hit=False,
        )

    monkeypatch.setattr(p_worker, "process_discovery", _fake_process)

    w = _new_worker()
    result = w.handle(_FakeEnvelope(
        action="discover",
        payload={"campaign_name": "wkr", "zipcodes": ["95993"]},
    ))
    assert result["campaign_name"] == "wkr"
    assert captured["task_id"] == "t-worker-1"


def test_handle_build_call_sheet_alias_routes_to_discover(monkeypatch):
    """build_call_sheet is an alias for the discover pipeline (matches /work)."""
    def _fake_process(req, *, task_id=None):
        from backend.prospecting.models import DiscoveryResult
        return DiscoveryResult(
            campaign_name=req.campaign_name, prospect_count=0,
            csv_path="/tmp/x.csv", prospects=[], cache_hit=False,
        )

    monkeypatch.setattr(p_worker, "process_discovery", _fake_process)

    w = _new_worker()
    result = w.handle(_FakeEnvelope(
        action="build_call_sheet",
        payload={"campaign_name": "alias", "zipcodes": ["95993"]},
    ))
    assert result["campaign_name"] == "alias"
