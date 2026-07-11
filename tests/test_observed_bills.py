"""Observed-bills summarizer — reads intake ledger, cross-refs CODB registry."""
from __future__ import annotations

import json


def _write_ledger(path, rows):
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_empty_ledger_returns_empty_summary(tmp_path, monkeypatch):
    ledger = tmp_path / "empty.jsonl"
    ledger.touch()
    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(ledger))
    # Isolate the bank ledger too so real data doesn't leak into tests.
    _bank_empty = ledger.parent / "_test_bank_empty.jsonl"
    _bank_empty.touch()
    monkeypatch.setenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH", str(_bank_empty))
    from backend.finance.observed_bills import summarize_observed_bills
    summary = summarize_observed_bills(window_days=30)
    assert summary.signals_scanned == 0
    assert summary.total_observed_usd == 0.0
    assert summary.vendors == []


def test_bill_signals_aggregated_per_vendor(tmp_path, monkeypatch):
    ledger = tmp_path / "bills.jsonl"
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_ledger(ledger, [
        {"ts": now_iso, "category": "bill", "vendor": "vapi-voice-calls",
         "amount_usd": 25.50, "bill_signal_kind": "receipt",
         "from_addr_tail": "mail.vapi.ai", "subject_head": "Receipt"},
        {"ts": now_iso, "category": "bill", "vendor": "vapi-voice-calls",
         "amount_usd": 30.00, "bill_signal_kind": "receipt",
         "from_addr_tail": "mail.vapi.ai", "subject_head": "Receipt"},
        {"ts": now_iso, "category": "bill", "vendor": "aws-other",
         "amount_usd": 100.00, "bill_signal_kind": "invoice",
         "from_addr_tail": "aws.amazon.com", "subject_head": "Invoice"},
        {"ts": now_iso, "category": "social",
         "from_addr_tail": "linkedin.com", "subject_head": "Notification"},
    ])
    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(ledger))
    # Isolate the bank ledger too so real data doesn't leak into tests.
    _bank_empty = ledger.parent / "_test_bank_empty.jsonl"
    _bank_empty.touch()
    monkeypatch.setenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH", str(_bank_empty))
    from backend.finance.observed_bills import summarize_observed_bills
    summary = summarize_observed_bills(window_days=30)
    assert summary.signals_scanned == 3           # bill entries only
    vendor_ids = {v.registry_id for v in summary.vendors}
    assert "vapi-voice-calls" in vendor_ids
    assert "aws-other" in vendor_ids
    vapi = next(v for v in summary.vendors if v.registry_id == "vapi-voice-calls")
    assert vapi.signal_count == 2
    assert vapi.total_observed_usd == 55.50
    assert vapi.receipt_count == 2


def test_payment_declined_counted_per_vendor(tmp_path, monkeypatch):
    ledger = tmp_path / "declines.jsonl"
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_ledger(ledger, [
        {"ts": now_iso, "category": "bill", "vendor": "openai-chatgpt-plus",
         "amount_usd": 20.00, "bill_signal_kind": "payment_declined",
         "from_addr_tail": "openai.com", "subject_head": "Declined"},
        {"ts": now_iso, "category": "bill", "vendor": "anthropic-claude-subscription",
         "amount_usd": 20.00, "bill_signal_kind": "receipt",
         "from_addr_tail": "anthropic.com", "subject_head": "Receipt"},
    ])
    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(ledger))
    # Isolate the bank ledger too so real data doesn't leak into tests.
    _bank_empty = ledger.parent / "_test_bank_empty.jsonl"
    _bank_empty.touch()
    monkeypatch.setenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH", str(_bank_empty))
    from backend.finance.observed_bills import summarize_observed_bills
    summary = summarize_observed_bills(window_days=30)
    assert summary.payment_declined_active == 1
    openai = next(v for v in summary.vendors if v.registry_id == "openai-chatgpt-plus")
    assert openai.payment_declined_count == 1


def test_variance_computed_against_registry(tmp_path, monkeypatch):
    ledger = tmp_path / "variance.jsonl"
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # 3 receipts of $50 across the 30-day window = $150 observed monthly.
    _write_ledger(ledger, [
        {"ts": now_iso, "category": "bill", "vendor": "vapi-voice-calls",
         "amount_usd": 50.00, "bill_signal_kind": "receipt",
         "from_addr_tail": "mail.vapi.ai", "subject_head": "Receipt"},
        {"ts": now_iso, "category": "bill", "vendor": "vapi-voice-calls",
         "amount_usd": 50.00, "bill_signal_kind": "receipt",
         "from_addr_tail": "mail.vapi.ai", "subject_head": "Receipt"},
        {"ts": now_iso, "category": "bill", "vendor": "vapi-voice-calls",
         "amount_usd": 50.00, "bill_signal_kind": "receipt",
         "from_addr_tail": "mail.vapi.ai", "subject_head": "Receipt"},
    ])
    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(ledger))
    # Isolate the bank ledger too so real data doesn't leak into tests.
    _bank_empty = ledger.parent / "_test_bank_empty.jsonl"
    _bank_empty.touch()
    monkeypatch.setenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH", str(_bank_empty))
    from backend.finance.observed_bills import summarize_observed_bills
    summary = summarize_observed_bills(window_days=30)
    vapi = next(v for v in summary.vendors if v.registry_id == "vapi-voice-calls")
    # Registry estimate for vapi-voice-calls is $30/mo per codb_registry.yaml
    assert vapi.registry_estimate_usd is not None
    assert vapi.variance_usd is not None
    # Observed = $150/mo, registry = $30/mo → variance ~= +$120/mo
    assert vapi.variance_usd > 100.0


def test_historical_row_without_category_reclassified(tmp_path, monkeypatch):
    """Ledger rows written before the classifier was wired reclassified on read."""
    ledger = tmp_path / "historical.jsonl"
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # No category field — mirrors the 760 replay rows.
    _write_ledger(ledger, [
        {"ts": now_iso, "from_addr_tail": "mail.vapi.ai",
         "subject_head": "Your Vapi invoice for $75.00", "persisted": True},
    ])
    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(ledger))
    # Isolate the bank ledger too so real data doesn't leak into tests.
    _bank_empty = ledger.parent / "_test_bank_empty.jsonl"
    _bank_empty.touch()
    monkeypatch.setenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH", str(_bank_empty))
    from backend.finance.observed_bills import summarize_observed_bills
    summary = summarize_observed_bills(window_days=30)
    # The classifier should recognize this as a Vapi bill from the subject/from
    assert summary.signals_scanned == 1
    assert any(v.registry_id == "vapi-voice-calls" for v in summary.vendors)


def test_briefing_lines_render_stably(tmp_path, monkeypatch):
    ledger = tmp_path / "briefing.jsonl"
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_ledger(ledger, [
        {"ts": now_iso, "category": "bill", "vendor": "vapi-voice-calls",
         "amount_usd": 50.00, "bill_signal_kind": "receipt",
         "from_addr_tail": "mail.vapi.ai", "subject_head": "Receipt"},
    ])
    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(ledger))
    from backend.finance.observed_bills import (
        observed_bills_briefing_lines,
        summarize_observed_bills,
    )
    lines = observed_bills_briefing_lines(summarize_observed_bills(window_days=30))
    assert any("OBSERVED BILLS" in line for line in lines)
    assert any("vapi-voice-calls" in line for line in lines)
