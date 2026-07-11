"""Debts loader + portfolio summary math."""
from __future__ import annotations


def test_missing_file_returns_empty_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DEBTS_PATH", str(tmp_path / "nonexistent.yaml"))
    from backend.finance.debts import load_registry
    reg, loaded = load_registry()
    assert loaded is False
    assert reg.debts == []


def test_load_minimal_registry(tmp_path, monkeypatch):
    p = tmp_path / "debts.yaml"
    p.write_text(
        "debts:\n"
        "  - id: D1\n"
        "    creditor: Test\n"
        "    type: mortgage\n"
        "    status: 'OK'\n"
        "    tier: 1\n"
        "    balance_usd: 100\n"
        "    balance_unknown: false\n"
        "    account_holders: [a]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_DEBTS_PATH", str(p))
    from backend.finance.debts import load_registry
    reg, loaded = load_registry()
    assert loaded is True
    assert len(reg.debts) == 1
    assert reg.debts[0].tier == 1


def test_recommended_path_returns_first_recommended(tmp_path, monkeypatch):
    p = tmp_path / "debts.yaml"
    p.write_text(
        "debts:\n"
        "  - id: D1\n"
        "    creditor: X\n"
        "    type: utility\n"
        "    status: ok\n"
        "    tier: 2\n"
        "    balance_usd: 10\n"
        "    resolution_paths:\n"
        "      - {label: 'Path A', recommended: false}\n"
        "      - {label: 'Path B', recommended: true}\n"
        "      - {label: 'Path C', recommended: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_DEBTS_PATH", str(p))
    from backend.finance.debts import load_registry, recommended_path
    reg, _ = load_registry()
    rec = recommended_path(reg.debts[0])
    assert rec is not None
    assert rec.label == "Path B"


def test_recommended_path_none_when_none_flagged(tmp_path, monkeypatch):
    p = tmp_path / "debts.yaml"
    p.write_text(
        "debts:\n"
        "  - id: D1\n"
        "    creditor: X\n"
        "    type: utility\n"
        "    status: ok\n"
        "    tier: 2\n"
        "    balance_usd: 10\n"
        "    resolution_paths:\n"
        "      - {label: 'Only path', recommended: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_DEBTS_PATH", str(p))
    from backend.finance.debts import load_registry, recommended_path
    reg, _ = load_registry()
    assert recommended_path(reg.debts[0]) is None


def test_by_tier_excludes_unknown_from_total(tmp_path, monkeypatch):
    p = tmp_path / "debts.yaml"
    p.write_text(
        "debts:\n"
        "  - {id: A, creditor: x, type: utility, status: ok, tier: 1, balance_usd: 100, balance_unknown: false}\n"
        "  - {id: B, creditor: x, type: utility, status: ok, tier: 1, balance_usd: 50, balance_unknown: false}\n"
        "  - {id: C, creditor: x, type: utility, status: ok, tier: 1, balance_unknown: true}\n"
        "  - {id: D, creditor: x, type: utility, status: ok, tier: 2, balance_usd: 200, balance_unknown: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_DEBTS_PATH", str(p))
    from backend.finance.debts import by_tier, load_registry
    reg, _ = load_registry()
    bins = by_tier(reg.debts)
    by_t = {b.tier: b for b in bins}
    assert by_t[1].debt_count == 3
    assert by_t[1].confirmed_total_usd == 150
    assert by_t[1].unknown_balance_count == 1
    assert by_t[2].debt_count == 1
    assert by_t[2].confirmed_total_usd == 200


def test_summarize_full_pipeline(tmp_path, monkeypatch):
    p = tmp_path / "debts.yaml"
    p.write_text(
        "debts:\n"
        "  - {id: A, creditor: x, type: utility, status: ok, tier: 1, balance_usd: 100, balance_unknown: false,\n"
        "     resolution_paths: [{label: 'Forbear', recommended: true}]}\n"
        "  - {id: B, creditor: x, type: utility, status: ok, tier: 2, balance_unknown: true}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_DEBTS_PATH", str(p))
    from backend.finance.debts import load_registry, summarize
    reg, loaded = load_registry()
    summary = summarize(reg, loaded, "2026-05-15T00:00:00Z")
    assert summary.debt_count == 2
    assert summary.confirmed_total_usd == 100
    assert summary.unknown_balance_count == 1
    assert summary.recommended_path_per_debt == {"A": "Forbear"}
    assert summary.registry_loaded is True
