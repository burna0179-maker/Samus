"""Tests for backend.leadgen.service."""
from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.leadgen.service as svc_mod
    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    return fresh


def test_normalize_domain_strips_scheme_and_www():
    from backend.leadgen.normalizer import normalize_domain

    assert normalize_domain("https://www.Acme.com/") == "acme.com"
    assert normalize_domain("acme.com") == "acme.com"
    assert normalize_domain("  http://www.acme.com/path  ") == "acme.com"


def test_process_lead_end_to_end(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from backend.leadgen.models import LeadRequest
    from backend.leadgen.service import process_lead

    req = LeadRequest(
        company="Acme Co",
        domain="https://www.acme.com/",
        industry="finance",
        employee_count=75,
        annual_revenue_usd=5_000_000,
        geo="US",
        signals=["manual_ops", "compliance_pressure"],
    )
    result = process_lead(req, task_id="t-lead-1")
    assert result.normalized_domain == "acme.com"
    assert result.segment == "smb"
    assert result.tier in ("medium", "high", "priority")
    assert 3 <= len(result.recommendations) <= 5
    assert any("matched_signals" in r or "no ICP" in r for r in result.reasons)
    # audit ledger was written
    audit_path = tmp_path / "audit.jsonl"
    assert audit_path.exists()
    assert audit_path.read_text(encoding="utf-8").strip()


def test_process_lead_cache_hit(tmp_path, monkeypatch):
    fresh = _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from backend.leadgen.models import LeadRequest
    from backend.leadgen.service import process_lead

    req = LeadRequest(
        company="Acme",
        domain="acme.com",
        industry="finance",
        employee_count=50,
        annual_revenue_usd=2_500_000,
        geo="US",
        signals=["manual_ops"],
    )
    a = process_lead(req, task_id="t1")
    b = process_lead(req, task_id="t2")
    assert a.model_dump() == b.model_dump()
    assert fresh.exists("acme.com:acme")
