"""Liabilities loader + per-lender balance math."""

from __future__ import annotations


def test_load_default_registry_validates():
    from backend.finance.liabilities import load_registry

    reg = load_registry()
    # PDF-seeded: 4 lenders, 24 loan entries, 0 repayments
    assert len(reg.lenders) == 4
    assert len(reg.loans) == 24
    assert reg.repayments == []
    ids = {l.id for l in reg.lenders}
    assert ids == {"sample-lender", "sample-customer", "cristina-chomina", "ori-may"}


def test_load_registry_respects_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "liab.yaml"
    custom.write_text(
        "lenders:\n"
        "  - id: a\n"
        "    name: A\n"
        "    relationship: friend\n"
        "    notes: ''\n"
        "loans:\n"
        "  - {lender_id: a, date: 2026-01-01, amount_usd: 100, memo: ''}\n"
        "repayments: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_LIABILITIES_PATH", str(custom))
    from backend.finance.liabilities import load_registry

    reg = load_registry()
    assert len(reg.lenders) == 1
    assert reg.loans[0].amount_usd == 100


def test_per_lender_balances_camille_total_572(monkeypatch):
    """Verify the real PDF math for Sample Lender."""
    monkeypatch.delenv("SAMUS_LIABILITIES_PATH", raising=False)
    from backend.finance.liabilities import load_registry, per_lender_balances

    balances = per_lender_balances(load_registry())
    by_id = {b.lender_id: b for b in balances}
    # 45+150+20+20+30+75+72+60+85+15 = 572
    assert by_id["sample-lender"].outstanding_usd == 572.0
    assert by_id["sample-lender"].loan_count == 10
    assert by_id["sample-lender"].repayment_count == 0


def test_per_lender_balances_harmony_total_740(monkeypatch):
    monkeypatch.delenv("SAMUS_LIABILITIES_PATH", raising=False)
    from backend.finance.liabilities import load_registry, per_lender_balances

    balances = per_lender_balances(load_registry())
    by_id = {b.lender_id: b for b in balances}
    # 40+40+30+75+90+125+90+250 = 740
    assert by_id["sample-customer"].outstanding_usd == 740.0
    assert by_id["sample-customer"].loan_count == 8


def test_per_lender_balances_sorted_desc(monkeypatch):
    monkeypatch.delenv("SAMUS_LIABILITIES_PATH", raising=False)
    from backend.finance.liabilities import load_registry, per_lender_balances

    balances = per_lender_balances(load_registry())
    outs = [b.outstanding_usd for b in balances]
    assert outs == sorted(outs, reverse=True)
    # Harmony (740) should be first; Cristina (75) should be last.
    assert balances[0].lender_id == "sample-customer"
    assert balances[-1].lender_id == "cristina-chomina"


def test_summarize_aggregates_to_1642(monkeypatch):
    monkeypatch.delenv("SAMUS_LIABILITIES_PATH", raising=False)
    from backend.finance.liabilities import load_registry, summarize

    summary = summarize(load_registry(), "2026-05-15T00:00:00Z")
    # 572 + 740 + 75 + 255 = 1642
    assert summary.total_outstanding_usd == 1642.0
    assert summary.total_loans_received_usd == 1642.0
    assert summary.total_repayments_made_usd == 0.0
    assert len(summary.by_lender) == 4


def test_repayment_reduces_outstanding(tmp_path, monkeypatch):
    custom = tmp_path / "liab.yaml"
    custom.write_text(
        "lenders:\n"
        "  - {id: a, name: A, relationship: friend, notes: ''}\n"
        "loans:\n"
        "  - {lender_id: a, date: 2026-01-01, amount_usd: 100, memo: ''}\n"
        "  - {lender_id: a, date: 2026-02-01, amount_usd: 50, memo: ''}\n"
        "repayments:\n"
        "  - {lender_id: a, date: 2026-03-01, amount_usd: 30, memo: 'partial'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_LIABILITIES_PATH", str(custom))
    from backend.finance.liabilities import load_registry, summarize

    s = summarize(load_registry(), "2026-05-15T00:00:00Z")
    assert s.total_loans_received_usd == 150
    assert s.total_repayments_made_usd == 30
    assert s.total_outstanding_usd == 120


def test_unknown_lender_id_surfaces_as_unknown(tmp_path, monkeypatch):
    custom = tmp_path / "liab.yaml"
    custom.write_text(
        "lenders: []\n"
        "loans:\n"
        "  - {lender_id: typo, date: 2026-01-01, amount_usd: 50, memo: ''}\n"
        "repayments: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_LIABILITIES_PATH", str(custom))
    from backend.finance.liabilities import load_registry, per_lender_balances

    balances = per_lender_balances(load_registry())
    assert len(balances) == 1
    assert balances[0].lender_name == "unknown:typo"
    assert balances[0].outstanding_usd == 50
