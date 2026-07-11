"""Reconciler: builds ledger + tracker rows from bank_activity idempotently."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _bank_ledger(tmp_path, rows):
    p = tmp_path / "bank_activity.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


def _exec_root(tmp_path):
    """Set up a fake Executive Docs tree with the two managed files
    stubbed with marker pairs so the reconciler can find them."""
    root = tmp_path / "Executive Docs"
    (root / "03_Ownership").mkdir(parents=True)
    (root / "07_Funding").mkdir(parents=True)
    (root / "03_Ownership" / "Capital_Contributions_Ledger.md").write_text(
        "# Ledger\n\n"
        "## Running totals\n\n"
        "<!-- reconciler:running-totals-start -->\n"
        "PLACEHOLDER\n"
        "<!-- reconciler:running-totals-end -->\n\n"
        "## Contributions\n\n"
        "<!-- reconciler:contributions-start -->\n"
        "PLACEHOLDER\n"
        "<!-- reconciler:contributions-end -->\n",
        encoding="utf-8",
    )
    (root / "07_Funding" / "Founder_Funding_Tracker.md").write_text(
        "# Tracker\n\n"
        "## Snapshot\n\n"
        "<!-- reconciler:snapshot-start -->\n"
        "PLACEHOLDER\n"
        "<!-- reconciler:snapshot-end -->\n\n"
        "## History\n\n"
        "<!-- reconciler:history-start -->\n"
        "PLACEHOLDER\n"
        "<!-- reconciler:history-end -->\n",
        encoding="utf-8",
    )
    return root


def _add_scripts_to_path():
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


def test_dry_run_reports_two_entries_from_5_bank_rows(tmp_path):
    _add_scripts_to_path()
    bank = _bank_ledger(
        tmp_path,
        [
            # 4 pre-formation Dec 2025 contributions folded into C-2026-001
            {
                "ts": "2025-12-13T10:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -500.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "aa1",
            },
            {
                "ts": "2025-12-13T11:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -1000.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "aa2",
            },
            {
                "ts": "2025-12-13T12:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -1000.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "aa3",
            },
            {
                "ts": "2025-12-14T08:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -1000.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "aa4",
            },
            # 1 post-formation contribution -> C-2026-002
            {
                "ts": "2026-03-14T08:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -1.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "bb1",
            },
            # A confounder: LLC-side row that should NOT be counted
            {
                "ts": "2026-03-16T08:00:00Z",
                "source": "cash_app_csv",
                "category": "bill",
                "amount_usd": -20.00,
                "raw_description": "HustleForge",
                "external_id": "cc1",
            },
            # Another confounder: personal row that isn't a business_transfer
            {
                "ts": "2026-04-01T08:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "personal",
                "amount_usd": -50.00,
                "raw_description": "SUBWAY 46308",
                "external_id": "dd1",
            },
        ],
    )
    import reconcile_capital_contributions as rc

    result = rc.reconcile(
        bank_ledger=bank,
        exec_root=_exec_root(tmp_path),
        dry_run=True,
    )
    assert result["candidate_bank_rows"] == 5
    assert result["entries_after_folding"] == 2
    assert result["total_usd"] == 3501.0
    assert result["entries"][0]["entry_id"] == "C-2026-001"
    assert result["entries"][0]["amount_usd"] == 3500.0
    assert result["entries"][1]["entry_id"] == "C-2026-002"
    assert result["entries"][1]["amount_usd"] == 1.0


def test_apply_writes_both_files_and_state(tmp_path):
    _add_scripts_to_path()
    bank = _bank_ledger(
        tmp_path,
        [
            {
                "ts": "2025-12-13T10:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -3500.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "aa1",
            },
        ],
    )
    root = _exec_root(tmp_path)
    import reconcile_capital_contributions as rc

    result = rc.reconcile(bank_ledger=bank, exec_root=root, dry_run=False)
    assert result["ledger_changed"] is True
    assert result["tracker_changed"] is True
    ledger_md = (root / "03_Ownership" / "Capital_Contributions_Ledger.md").read_text(
        encoding="utf-8"
    )
    assert "C-2026-001" in ledger_md
    assert "$3,500.00" in ledger_md
    tracker_md = (root / "07_Funding" / "Founder_Funding_Tracker.md").read_text(encoding="utf-8")
    assert "Capital Account balance (Alex)" in tracker_md
    assert "$3,500.00" in tracker_md
    state = json.loads((root / "03_Ownership" / ".state" / "capital_reconcile.json").read_text())
    assert state["external_id_to_entry"]["aa1"] == "C-2026-001"


def test_rerun_is_idempotent(tmp_path):
    _add_scripts_to_path()
    bank = _bank_ledger(
        tmp_path,
        [
            {
                "ts": "2025-12-13T10:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -1000.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "aa1",
            },
        ],
    )
    root = _exec_root(tmp_path)
    import reconcile_capital_contributions as rc

    rc.reconcile(bank_ledger=bank, exec_root=root, dry_run=False)
    r2 = rc.reconcile(bank_ledger=bank, exec_root=root, dry_run=False)
    assert r2["ledger_changed"] is False
    assert r2["tracker_changed"] is False


def test_new_contribution_gets_next_sequential_id(tmp_path):
    _add_scripts_to_path()
    root = _exec_root(tmp_path)
    bank = _bank_ledger(
        tmp_path,
        [
            {
                "ts": "2025-12-13T10:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -3500.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "aa1",
            },
        ],
    )
    import reconcile_capital_contributions as rc

    rc.reconcile(bank_ledger=bank, exec_root=root, dry_run=False)
    # Now add a new bank row and re-reconcile
    with bank.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": "2026-08-01T10:00:00Z",
                    "source": "cash_app_personal_csv",
                    "category": "business_transfer",
                    "amount_usd": -250.00,
                    "raw_description": "CASH_CARD HUSTLEFORGE",
                    "external_id": "bb1",
                }
            )
            + "\n"
        )
    result = rc.reconcile(bank_ledger=bank, exec_root=root, dry_run=False)
    assert result["ledger_changed"] is True
    assert result["entries_after_folding"] == 2
    assert result["entries"][1]["entry_id"] == "C-2026-002"
    assert result["entries"][1]["amount_usd"] == 250.0


def test_duplicate_marker_raises_marker_error(tmp_path):
    """Doc with the literal marker text in prose triggers _MarkerError."""
    _add_scripts_to_path()
    root = _exec_root(tmp_path)
    # Corrupt the ledger by adding the literal marker text a second time
    bad = (
        "# Ledger\n\n"
        "<!-- reconciler:running-totals-start -->\n"
        "PLACEHOLDER\n"
        "<!-- reconciler:running-totals-end -->\n\n"
        # Second occurrence of the same marker on its own line:
        "<!-- reconciler:running-totals-start -->\n"
        "SECOND_ONE\n"
        "<!-- reconciler:running-totals-end -->\n\n"
        "<!-- reconciler:contributions-start -->\n"
        "PLACEHOLDER\n"
        "<!-- reconciler:contributions-end -->\n"
    )
    (root / "03_Ownership" / "Capital_Contributions_Ledger.md").write_text(bad, encoding="utf-8")
    bank = _bank_ledger(
        tmp_path,
        [
            {
                "ts": "2025-12-13T10:00:00Z",
                "source": "cash_app_personal_csv",
                "category": "business_transfer",
                "amount_usd": -100.00,
                "raw_description": "CASH_CARD HUSTLEFORGE",
                "external_id": "aa1",
            },
        ],
    )
    import reconcile_capital_contributions as rc
    import pytest

    with pytest.raises(rc._MarkerError):
        rc.reconcile(bank_ledger=bank, exec_root=root, dry_run=False)


def test_llc_side_rows_excluded(tmp_path):
    """A cash_app_csv (LLC-side) row is NOT a founder contribution."""
    _add_scripts_to_path()
    bank = _bank_ledger(
        tmp_path,
        [
            {
                "ts": "2026-03-16T08:00:00Z",
                "source": "cash_app_csv",
                "category": "bill",
                "amount_usd": -20.00,
                "raw_description": "HustleForge",
                "external_id": "cc1",
            },
            {
                "ts": "2026-07-03T08:00:00Z",
                "source": "cash_app_csv",
                "category": "personal",
                "amount_usd": -1.00,
                "raw_description": "HUSTLEFORGE LLC",
                "external_id": "cc2",
            },
        ],
    )
    root = _exec_root(tmp_path)
    import reconcile_capital_contributions as rc

    result = rc.reconcile(bank_ledger=bank, exec_root=root, dry_run=True)
    assert result["candidate_bank_rows"] == 0
    assert result["entries_after_folding"] == 0
