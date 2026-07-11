"""Bank activity ingester — CSV parser + ledger idempotency + classification."""
from __future__ import annotations

import json


def _write_csv(path, rows):
    header = "Date,Description,Amount,Category,Receipt,Asset,Card,Note,Tags,Split\n"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(header)
        for r in rows:
            fh.write(",".join(f'"{v}"' if "," in str(v) else str(v) for v in r) + "\n")


def test_csv_parse_and_vendor_match(tmp_path):
    csv_p = tmp_path / "activity.csv"
    _write_csv(csv_p, [
        ("07/02/2026", "APOLLO.IO", "-65.0", "Uncategorized", "None", "N",
         "Business debit 4801", "", "", "No"),
        ("06/09/2026", "Anthropic", "-92.06", "Software and subscriptions",
         "None", "N", "Business debit 4801", "", "", "No"),
        ("05/21/2026", "VAPI API", "-10.0", "Software and subscriptions",
         "None", "N", "Business debit 4801", "", "", "No"),
    ])
    from backend.finance.bank_activity import parse_cash_app_csv
    txns = parse_cash_app_csv(csv_p)
    assert len(txns) == 3
    apollo = next(t for t in txns if "APOLLO" in t.raw_description)
    assert apollo.category == "bill"
    assert apollo.vendor_registry_id == "apollo-basic"
    assert apollo.bill_signal_kind == "receipt"
    assert apollo.amount_usd == -65.0


def test_revenue_and_transfer_classified(tmp_path):
    csv_p = tmp_path / "activity.csv"
    _write_csv(csv_p, [
        ("07/02/2026", "alex hartman", "220.0", "Business income", "None", "N",
         "", "", "", "No"),
        ("06/12/2026", "Team payments to Primary", "33.2", "Personal", "None", "N",
         "", "", "", "No"),
        ("06/12/2026", "Cash App", "-3.86", "Personal", "None", "N",
         "Business debit 4801", "", "", "No"),
    ])
    from backend.finance.bank_activity import parse_cash_app_csv
    txns = parse_cash_app_csv(csv_p)
    cats = {t.raw_description: t.category for t in txns}
    assert cats["alex hartman"] == "revenue"
    assert cats["Team payments to Primary"] == "transfer"
    assert cats["Cash App"] == "personal"


def test_ledger_idempotent_on_reingest(tmp_path):
    csv_p = tmp_path / "activity.csv"
    ledger_p = tmp_path / "bank_activity.jsonl"
    _write_csv(csv_p, [
        ("07/02/2026", "APOLLO.IO", "-65.0", "Uncategorized", "None", "N",
         "Business debit 4801", "", "", "No"),
    ])
    from backend.finance.bank_activity import (
        append_transactions,
        parse_cash_app_csv,
    )
    txns = parse_cash_app_csv(csv_p)
    a1, d1 = append_transactions(txns, path=ledger_p)
    a2, d2 = append_transactions(txns, path=ledger_p)
    assert a1 == 1 and d1 == 0
    assert a2 == 0 and d2 == 1


def test_bank_data_supersedes_gmail_estimate(tmp_path, monkeypatch):
    """When both Gmail and bank have data for a vendor, bank wins."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Gmail ledger: claims vapi charged $50 x3 = $150 total
    gmail = tmp_path / "gmail.jsonl"
    with gmail.open("w", encoding="utf-8") as fh:
        for _ in range(3):
            fh.write(json.dumps({
                "ts": now_iso, "category": "bill",
                "vendor": "vapi-voice-calls", "amount_usd": 50.00,
                "bill_signal_kind": "receipt",
                "from_addr_tail": "mail.vapi.ai", "subject_head": "Receipt",
            }) + "\n")

    # Bank ledger: shows vapi actually only charged $10 (bank is truth)
    bank = tmp_path / "bank.jsonl"
    with bank.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": now_iso, "source": "cash_app_csv",
            "external_id": "abc123",
            "amount_usd": -10.00, "raw_description": "VAPI API",
            "vendor_registry_id": "vapi-voice-calls", "category": "bill",
            "bill_signal_kind": "receipt", "card_ref": "Business debit 4801",
            "bank_category": "Software and subscriptions", "note": "",
        }) + "\n")

    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(gmail))
    monkeypatch.setenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH", str(bank))
    from backend.finance.observed_bills import summarize_observed_bills
    summary = summarize_observed_bills(window_days=30)
    vapi = next(v for v in summary.vendors if v.registry_id == "vapi-voice-calls")
    # Bank preference wins: observed = $10 (bank), not $150 (gmail).
    assert vapi.total_observed_usd == 10.0


def test_bank_revenue_transfers_land_in_summary(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    bank = tmp_path / "bank.jsonl"
    with bank.open("w", encoding="utf-8") as fh:
        for row in [
            {"ts": now_iso, "source": "cash_app_csv", "external_id": "r1",
             "amount_usd": 220.00, "raw_description": "alex hartman",
             "vendor_registry_id": "", "category": "revenue",
             "bill_signal_kind": "", "card_ref": "", "bank_category": "Business income", "note": ""},
            {"ts": now_iso, "source": "cash_app_csv", "external_id": "t1",
             "amount_usd": 33.20, "raw_description": "Team payments to Primary",
             "vendor_registry_id": "", "category": "transfer",
             "bill_signal_kind": "", "card_ref": "", "bank_category": "Personal", "note": ""},
        ]:
            fh.write(json.dumps(row) + "\n")

    gmail = tmp_path / "gmail.jsonl"
    gmail.touch()
    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(gmail))
    monkeypatch.setenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH", str(bank))
    from backend.finance.observed_bills import summarize_observed_bills
    summary = summarize_observed_bills(window_days=30)
    assert summary.bank_txn_count == 2
    assert summary.bank_revenue_usd == 220.0
    assert summary.bank_transfer_usd == 33.20
    # Net = 220 + 33.20 + 0 (personal) - 0 (spend) = 253.20
    assert abs(summary.bank_net_usd - 253.20) < 0.01


def test_personal_csv_parses_and_classifies_business_signals(tmp_path):
    """Personal Cash App CSV has different columns; business signals extracted."""
    csv_p = tmp_path / "personal.csv"
    with csv_p.open("w", encoding="utf-8") as fh:
        fh.write('"Date","Transaction ID","Transaction Type","Currency","Amount",'
                 '"Fee","Net Amount","Asset Type","Asset Price","Asset Amount",'
                 '"Status","Notes","Name of sender/receiver","Account"\n')
        # Business revenue (P2P received with business note)
        fh.write('"2026-06-01 08:00:00 PDT","","P2P","USD","$50.00","","$50.00","","","","COMPLETE","website maintenance","Sample Customer","Cash Balance"\n')
        # Personal spending
        fh.write('"2026-06-05 10:00:00 PDT","","Cash Card","USD","-$12.77","","-$12.77","","","","COMPLETE","SUBWAY 46308","","Cash Balance"\n')
        # LLC funding
        fh.write('"2025-12-13 08:00:00 PDT","","Cash Card","USD","-$1000.00","","-$1000.00","","","","COMPLETE","HUSTLEFORGE","","Cash Balance"\n')
        # Borrow (personal, but tracked for founder cash-health)
        fh.write('"2026-07-01 08:00:00 PDT","","Borrow","USD","-$5.25","","-$5.25","","","","COMPLETE","Borrowing in Cash App","","Cash Balance"\n')
        # P2P sent to friend (personal)
        fh.write('"2026-07-05 10:00:00 PDT","","P2P","USD","-$10.00","","-$10.00","","","","COMPLETE","fob","Sample Customer","Cash Balance"\n')

    from backend.finance.bank_activity import parse_cash_app_personal_csv
    txns = parse_cash_app_personal_csv(csv_p)
    assert len(txns) == 5

    by_cat = {}
    for t in txns:
        by_cat.setdefault(t.category, []).append(t)

    # Business revenue detected
    revenue = by_cat.get("revenue", [])
    assert len(revenue) == 1
    assert revenue[0].amount_usd == 50.0
    assert "website" in revenue[0].note.lower()

    # LLC funding detected as business_transfer
    biz_transfer = by_cat.get("business_transfer", [])
    assert len(biz_transfer) == 1
    assert biz_transfer[0].amount_usd == -1000.0

    # Personal rows include the borrow, subway, and P2P outbound
    personal = by_cat.get("personal", [])
    assert len(personal) == 3


def test_auto_detect_routes_to_correct_parser(tmp_path):
    """parse_activity_csv_auto picks the right parser from header line."""
    # Business format
    biz_p = tmp_path / "biz.csv"
    biz_p.write_text(
        "Date,Description,Amount,Category,Receipt,Asset,Card,Note,Tags,Split\n"
        '07/02/2026,APOLLO.IO,-65.0,Uncategorized,None,N,"",""," ",No\n',
        encoding="utf-8",
    )
    # Personal format
    per_p = tmp_path / "per.csv"
    per_p.write_text(
        '"Date","Transaction ID","Transaction Type","Currency","Amount",'
        '"Fee","Net Amount","Asset Type","Asset Price","Asset Amount",'
        '"Status","Notes","Name of sender/receiver","Account"\n'
        '"2026-06-01 08:00:00 PDT","","P2P","USD","$50.00","","$50.00","","","","COMPLETE","website","Harmony","Cash Balance"\n',
        encoding="utf-8",
    )
    from backend.finance.bank_activity import parse_activity_csv_auto
    shape_a, _ = parse_activity_csv_auto(biz_p)
    shape_b, _ = parse_activity_csv_auto(per_p)
    assert shape_a == "business"
    assert shape_b == "personal"


def test_founder_cash_health_populated_from_personal_rows(tmp_path, monkeypatch):
    """Personal-source rows drive founder_borrow / deposits / cash_card fields."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    bank = tmp_path / "bank.jsonl"
    with bank.open("w", encoding="utf-8") as fh:
        # Borrow rows drive founder_borrow_usd
        fh.write(json.dumps({
            "ts": now_iso, "source": "cash_app_personal_csv",
            "external_id": "b1", "amount_usd": -5.25,
            "raw_description": "Borrow", "vendor_registry_id": "",
            "category": "personal", "bill_signal_kind": "",
            "card_ref": "Cash Balance", "bank_category": "Borrow", "note": "",
        }) + "\n")
        fh.write(json.dumps({
            "ts": now_iso, "source": "cash_app_personal_csv",
            "external_id": "d1", "amount_usd": 100.00,
            "raw_description": "Deposit", "vendor_registry_id": "",
            "category": "personal", "bill_signal_kind": "",
            "card_ref": "Cash Balance", "bank_category": "Deposits", "note": "",
        }) + "\n")

    gmail = tmp_path / "gmail.jsonl"
    gmail.touch()
    monkeypatch.setenv("SAMUS_GMAIL_INBOX_LEDGER_PATH", str(gmail))
    monkeypatch.setenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH", str(bank))
    from backend.finance.observed_bills import summarize_observed_bills
    summary = summarize_observed_bills(window_days=30)
    assert summary.founder_borrow_usd == -5.25
    assert summary.founder_deposits_usd == 100.00


def test_external_id_stable_across_reparse(tmp_path):
    from backend.finance.bank_activity import parse_cash_app_csv
    csv_p = tmp_path / "activity.csv"
    _write_csv(csv_p, [
        ("07/02/2026", "APOLLO.IO", "-65.0", "Uncategorized", "None", "N",
         "Business debit 4801", "", "", "No"),
    ])
    txns_a = parse_cash_app_csv(csv_p)
    txns_b = parse_cash_app_csv(csv_p)
    assert txns_a[0].external_id == txns_b[0].external_id
