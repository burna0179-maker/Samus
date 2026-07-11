"""Found bank cash reconciliation — the real cash figure for runway/affordability."""
from __future__ import annotations

import pytest

from backend.finance.found_cash import best_available_cash_usd, read_found_cash

_CSV = (
    "Date,Description,Amount,Category,Receipt,Asset,Card,Note,Tags,Split\n"
    "07/02/2026,ACME,-65.0,Uncategorized,None,N,Business debit 4801,\"\",\"\",No\n"
    "07/02/2026,alex hartman,327.31,Business income,None,N,\"\",\"\",\"\",No\n"
    "03/09/2026,Visa debit,25.0,Personal funding,None,N,\"\",open account,\"\",No\n"
)


def _write(tmp_path, name="hustleforge_llc_activity_report_1.csv", body=_CSV):
    (tmp_path / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_balance_is_running_sum(tmp_path):
    _write(tmp_path)
    fc = read_found_cash(str(tmp_path))
    assert fc is not None
    assert fc.balance_usd == pytest.approx(287.31)  # -65 + 327.31 + 25
    assert fc.txn_count == 3
    assert fc.latest_txn_date == "2026-07-02"


def test_picks_newest_export(tmp_path):
    import os
    import time
    _write(tmp_path, "hustleforge_llc_activity_report_old.csv",
           "Date,Description,Amount\n01/01/2026,x,10.0\n")
    time.sleep(0.02)
    _write(tmp_path, "hustleforge_llc_activity_report_new.csv",
           "Date,Description,Amount\n02/02/2026,y,99.0\n")
    # make 'new' definitively newer by mtime
    os.utime(tmp_path / "hustleforge_llc_activity_report_new.csv", None)
    fc = read_found_cash(str(tmp_path))
    assert fc.source_file.endswith("_new.csv") and fc.balance_usd == pytest.approx(99.0)


def test_no_csv_returns_none(tmp_path):
    assert read_found_cash(str(tmp_path)) is None


def test_best_available_prefers_found_over_stripe(tmp_path, monkeypatch):
    _write(tmp_path)
    monkeypatch.setenv("SAMUS_FOUND_ACTIVITY_DIR", str(tmp_path))
    usd, source = best_available_cash_usd(0.0)   # stripe available = $0
    assert usd == pytest.approx(287.31) and source == "found_bank"


def test_best_available_falls_back_to_stripe_when_no_found(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_FOUND_ACTIVITY_DIR", str(tmp_path))  # empty dir
    usd, source = best_available_cash_usd(50.0)
    assert usd == pytest.approx(50.0) and source == "stripe_available"
