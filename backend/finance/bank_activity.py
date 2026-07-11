"""Bank activity — authoritative account transaction stream.

Ingests transaction data from the account into a canonical schema that
:mod:`observed_bills` and CODB reasoning already know how to consume.

Supported sources:
  - "cash_app_csv"           — Cash App LLC Business activity export
                                 (columns: Date, Description, Amount, Category,
                                 Receipt, Asset, Card, Note, Tags, Split)
  - "cash_app_personal_csv"  — Cash App personal account export
                                 (columns: Date, Transaction ID, Transaction Type,
                                 Currency, Amount, Fee, Net Amount, Notes,
                                 Name of sender/receiver, Account, ...)
  - "mercury_api"            — future Mercury Bank API transactions
                                 (implemented alongside as parse_mercury_transactions)

The personal CSV path is essential because business signals leak into
the personal account: revenue from clients (P2P received with "invoice"
/ "website" / business-note markers), and initial LLC funding
(Cash Card charged with "HUSTLEFORGE" merchant name). Non-business
transactions are still ingested but flagged category=personal so
observed_bills excludes them from CODB variance math.

Why authoritative: Gmail bill signals are the vendor's SIDE of the
transaction ("we billed you $X"). Bank activity is the ACCOUNT'S side
("we charged you $X, it went through"). Where the two disagree the
bank is truth — the vendor may have billed but the charge failed, or
the vendor may charge under a different name than the email.

Canonical schema (matches the shape the future Mercury API integration
will emit, so the CSV path is the transition — same downstream consumer):

    {
      "ts": ISO-8601,
      "source": "cash_app_csv" | "mercury_api" | ...,
      "external_id": stable per-row id (idempotency key across replays),
      "amount_usd": float (positive = money in, negative = money out),
      "raw_description": exactly what the bank showed,
      "vendor_registry_id": mapped codb_registry.yaml id (or "" if unmatched),
      "category": "bill" | "revenue" | "transfer" | "personal" | "other",
      "bill_signal_kind": "receipt" | "payment_declined" | ...  (bills only),
      "card_ref": "Business debit 4801" | "Visa debit 4088" | "",
      "bank_category": raw category string from the CSV/API,
      "note": free-form note from the CSV/API,
    }

Read-only. Ingestion is APPEND-ONLY to a JSONL ledger, keyed on
``external_id`` so re-running an ingest of the same CSV is idempotent.
Never mutates codb_registry.yaml; variance surfaces as a signal for
reasoning, not an auto-edit.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_LOG = logging.getLogger("samus.finance.bank_activity")

_LEDGER_PATH_DEFAULT = "/opt/samus/data/finance/bank_activity.jsonl"


def ledger_path() -> Path:
    return Path(os.getenv("SAMUS_BANK_ACTIVITY_LEDGER_PATH") or _LEDGER_PATH_DEFAULT)


# --- Vendor mapping — bank descriptions -> codb_registry.yaml ids ---------
#
# Bank descriptions are terser than email From/Subject and have a merchant
# processor's fingerprint on them (e.g. "GOOGLE*CLOUD KXJRG3" instead of
# "no-reply@cloud.google.com"). Explicit substring -> registry_id table
# keeps the mapping obvious and edit-friendly.
_VENDOR_SUBSTRING_MAP: list[tuple[str, str]] = [
    ("ANTHROPIC", "anthropic-claude-subscription"),
    ("GOOGLE*CLOUD", "gcp-cloud-run"),
    ("GOOGLE CLOUD", "gcp-cloud-run"),
    ("APOLLO.IO", "apollo-basic"),
    ("APOLLO", "apollo-basic"),
    ("WORDPRESS", "wordpress-com-domain"),
    ("WP*WORDPRESS", "wordpress-com-domain"),
    ("VAPI", "vapi-voice-calls"),
    ("TWILIO", "twilio-telephony"),
    ("STRIPE", "stripe-fees"),
    ("SENDGRID", "sendgrid-email"),
    ("OPENAI", "openai-api-samus-inference"),
    ("XFINITY", "xfinity-internet-cable"),
    ("XFINITY MOBILE", "xfinity-internet-cable"),
    ("RACE COMM", "race-internet"),
    ("PG&E", "pge-energy"),
    ("PGE", "pge-energy"),
    ("SPOTIFY", "spotify-premium"),
]

# Descriptions that classify the row as revenue rather than a bill.
_REVENUE_HINTS = ("alex hartman",)

# Descriptions that classify as internal transfer / owner draw / personal.
# These aren't CODB, but they ARE cash flow the reasoner should be aware of.
_TRANSFER_HINTS = ("visa debit", "team payments", "taxes to primary")
_PERSONAL_HINTS = ("cash app", "hustleforge llc")  # merchant-services fees


@dataclass
class BankTransaction:
    ts: str  # ISO-8601 (UTC-inferred at parse time)
    source: str  # "cash_app_csv" | "mercury_api" | ...
    external_id: str  # stable per-row hash (idempotency key)
    amount_usd: float  # positive = in, negative = out
    raw_description: str
    vendor_registry_id: str = ""  # "" if unmatched
    category: str = "other"  # bill | revenue | transfer | personal | other
    bill_signal_kind: str = ""  # receipt | payment_declined | ...
    card_ref: str = ""
    bank_category: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "source": self.source,
            "external_id": self.external_id,
            "amount_usd": round(self.amount_usd, 2),
            "raw_description": self.raw_description,
            "vendor_registry_id": self.vendor_registry_id,
            "category": self.category,
            "bill_signal_kind": self.bill_signal_kind,
            "card_ref": self.card_ref,
            "bank_category": self.bank_category,
            "note": self.note,
        }


def _map_vendor(description: str) -> str:
    """Match a bank description against the vendor substring table."""
    desc_upper = (description or "").upper()
    for needle, registry_id in _VENDOR_SUBSTRING_MAP:
        if needle in desc_upper:
            return registry_id
    return ""


def _classify_bank_row(
    description: str,
    amount: float,
    bank_category: str = "",
) -> tuple[str, str, str]:
    """Return (category, vendor_registry_id, bill_signal_kind).

    Priority:
      1. Revenue hints (positive amount from owner or a labelled income row)
      2. Explicit bank_category=Business income
      3. Vendor match -> bill (with sign determining signal_kind)
      4. Transfer / personal hints
      5. fallback: other
    """
    desc = (description or "").lower()
    bcat = (bank_category or "").lower()

    # (1) Explicit revenue signals — positive amount from a known income label.
    if amount > 0 and (
        "income" in bcat
        or "personal funding" in bcat
        or any(hint in desc for hint in _REVENUE_HINTS)
    ):
        return "revenue", "", ""

    vendor = _map_vendor(description)

    # (2) Vendor-matched spend → bill
    if vendor:
        kind = "receipt" if amount < 0 else "refund"
        return "bill", vendor, kind

    # (3) Transfers (owner distributions, tax withholding)
    if any(hint in desc for hint in _TRANSFER_HINTS):
        return "transfer", "", ""

    # (4) Personal / passthrough (Cash App merchant fee, misc)
    if any(hint in desc for hint in _PERSONAL_HINTS):
        return "personal", "", ""

    # (5) Uncategorized outbound spend — fall through as an unmatched bill so
    # it still surfaces in observed_bills' unmatched vendor count.
    if amount < 0:
        return "bill", "", "receipt"

    return "other", "", ""


def _parse_date(raw: str) -> str:
    """Parse M/D/Y (US-format Cash App CSV) → ISO-8601 UTC midnight."""
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    # Unparseable — use current time so the row still lands (defensive).
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _external_id(source: str, ts: str, description: str, amount: float) -> str:
    """Stable per-row hash for idempotent re-ingest."""
    seed = f"{source}|{ts}|{description.strip()}|{amount:.2f}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


# --- Personal-CSV business-signal detection -------------------------------
#
# The personal Cash App account carries some business signals: revenue
# from clients (P2P received flagged with invoice / website / business
# keywords in the note), and the initial LLC funding (Cash Card charged
# with "HUSTLEFORGE" merchant text). Everything else is genuinely
# personal — Cash Card at gas stations, food, subs; borrows for daily
# expense; P2P to friends and family.

_BUSINESS_NOTE_KEYWORDS = (
    "invoice",
    "website maintenance",
    "premium website",
    "email invoice",
    "consulting",
    "freelance",
    "client",
    "contract work",
    "web dev",
)
_BUSINESS_MERCHANT_KEYWORDS = (
    "HUSTLEFORGE",
    "TWISTED DRAGON",
)


def _classify_personal_row(
    txn_type: str,
    amount: float,
    notes: str,
    sender: str,
) -> tuple[str, str, str, bool]:
    """Classify one personal-CSV row.

    Returns (category, vendor_registry_id, bill_signal_kind, is_business).

    Priority:
      1. Merchant text or note text names a business we own or client work → business
      2. P2P with positive amount and business-note keyword → business revenue
      3. Otherwise → personal (still ingested for founder cash-health visibility)
    """
    notes_l = (notes or "").lower()
    notes_u = (notes or "").upper()
    sender_l = (sender or "").lower()

    # (1) Merchant text names a business we own (HUSTLEFORGE = LLC funding,
    # TWISTED DRAGON = a prior business of Alex's).
    for kw in _BUSINESS_MERCHANT_KEYWORDS:
        if kw in notes_u:
            # A charge (negative amount) TO a business we own is funding /
            # capital contribution; a credit (positive) is a distribution.
            kind = "capital_contribution" if amount < 0 else "distribution"
            return "business_transfer", "", kind, True

    # (2) P2P with a business-note keyword — either invoicing IN or OUT.
    if txn_type == "P2P" and any(kw in notes_l for kw in _BUSINESS_NOTE_KEYWORDS):
        if amount > 0:
            return "revenue", "", "p2p_invoice_paid", True
        else:
            return "bill", "", "p2p_contractor_paid", True

    # (3) Everything else is personal. We still ingest for founder cash-flow
    # visibility, but observed_bills' CODB logic already excludes category
    # in {"personal", "transfer", "other"} from the vendor variance math.
    return "personal", "", "", False


def _parse_amount_string(raw: str) -> float:
    """Parse a "$1,234.56" or "-$5.25" formatted amount into a float."""
    if not raw:
        return 0.0
    s = raw.strip().replace("$", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_datetime_string(raw: str) -> str:
    """Parse '2026-07-24 08:00:00 PDT' into ISO-8601. Best effort."""
    if not raw:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Strip trailing timezone abbreviation (Python doesn't parse PDT/PST/EDT well).
    core = re.sub(r"\s+[A-Z]{2,4}$", "", raw.strip())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(core, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_cash_app_personal_csv(path: Path) -> list[BankTransaction]:
    """Parse the Cash App PERSONAL account CSV export.

    Different columns from the Business export — Transaction Type carries
    the row semantics (Cash Card, P2P, Borrow, Deposits, Withdrawal, ...),
    Amount is a "$X.XX" formatted string with an optional leading "-".

    Business signals surface as category=revenue / bill / business_transfer.
    Everything else lands as category=personal (still ingested so founder
    cash-flow is visible — borrow/deposit signals matter for runway
    reasoning even when they don't hit CODB directly).
    """
    if not path.exists():
        raise FileNotFoundError(f"bank_activity: CSV not found: {path}")
    txns: list[BankTransaction] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                raw_date = row.get("Date", "").strip()
                txn_type = row.get("Transaction Type", "").strip()
                amount_raw = row.get("Amount", "").strip()
                notes = row.get("Notes", "").strip()
                sender = row.get("Name of sender/receiver", "").strip()
                account = row.get("Account", "").strip()
                txn_id = row.get("Transaction ID", "").strip()
                if not raw_date or not amount_raw:
                    continue
                amount = _parse_amount_string(amount_raw)
                ts = _parse_datetime_string(raw_date)

                # Compose a description that carries the semantic bits the
                # classifier reads (merchant name from Notes, transaction
                # type, sender for P2P). Rich enough for later reasoning.
                if txn_type == "P2P" and sender:
                    description = f"P2P {sender}: {notes}".strip()
                elif txn_type == "Cash Card" and notes:
                    description = f"CASH_CARD {notes}"
                elif txn_type:
                    description = f"{txn_type}: {notes}".strip(": ")
                else:
                    description = notes or "unknown"

                category, vendor, kind, _is_biz = _classify_personal_row(
                    txn_type,
                    amount,
                    notes,
                    sender,
                )
                # Personal-CSV rows always land — is_biz decides how they're
                # bucketed downstream (business signals into observed_bills
                # variance math; personal into founder-cash-health).
                txns.append(
                    BankTransaction(
                        ts=ts,
                        source="cash_app_personal_csv",
                        external_id=_external_id(
                            "cash_app_personal_csv",
                            ts,
                            description + "|" + (txn_id or ""),
                            amount,
                        ),
                        amount_usd=amount,
                        raw_description=description,
                        vendor_registry_id=vendor,
                        category=category,
                        bill_signal_kind=kind,
                        card_ref=account,
                        bank_category=txn_type,  # transaction type as the "category"
                        note=notes,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("bank_activity: personal-CSV row skipped: %s", exc)
    return txns


def parse_activity_csv_auto(path: Path) -> tuple[str, list[BankTransaction]]:
    """Auto-detect CSV shape and route to the right parser.

    Returns (shape_label, transactions). Shape labels: "business" or "personal".
    Detection is header-based (the two Cash App formats have distinct
    signature columns).
    """
    if not path.exists():
        raise FileNotFoundError(f"bank_activity: CSV not found: {path}")
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        header_line = fh.readline().strip()
    if "Transaction Type" in header_line and "Name of sender/receiver" in header_line:
        return "personal", parse_cash_app_personal_csv(path)
    return "business", parse_cash_app_csv(path)


# --- CSV parser (Cash App Business format) --------------------------------


def parse_cash_app_csv(path: Path) -> list[BankTransaction]:
    """Parse a Cash App Business activity CSV export.

    Expected columns (per 2026-07 export):
        Date, Description, Amount, Category, Receipt, Asset, Card, Note, Tags, Split

    Never raises on a bad row — a malformed line is skipped + logged so
    the good rows still land.
    """
    if not path.exists():
        raise FileNotFoundError(f"bank_activity: CSV not found: {path}")
    txns: list[BankTransaction] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                raw_date = row.get("Date", "").strip()
                description = row.get("Description", "").strip()
                amount_raw = row.get("Amount", "0").strip()
                if not raw_date or not description:
                    continue
                try:
                    amount = float(amount_raw)
                except ValueError:
                    _LOG.warning(
                        "bank_activity: unparseable amount %r in %s", amount_raw, description
                    )
                    continue
                ts = _parse_date(raw_date)
                bank_category = row.get("Category", "").strip()
                card_ref = row.get("Card", "").strip()
                note = row.get("Note", "").strip()

                category, vendor, kind = _classify_bank_row(
                    description,
                    amount,
                    bank_category,
                )
                txns.append(
                    BankTransaction(
                        ts=ts,
                        source="cash_app_csv",
                        external_id=_external_id("cash_app_csv", ts, description, amount),
                        amount_usd=amount,
                        raw_description=description,
                        vendor_registry_id=vendor,
                        category=category,
                        bill_signal_kind=kind,
                        card_ref=card_ref,
                        bank_category=bank_category,
                        note=note,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one bad row must not kill the ingest
                _LOG.warning("bank_activity: row skipped: %s", exc)
    return txns


# --- Ledger writer --------------------------------------------------------


def _load_existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = rec.get("external_id")
                if eid:
                    ids.add(eid)
    except OSError:
        pass
    return ids


def append_transactions(
    transactions: Iterable[BankTransaction],
    *,
    path: Path | None = None,
) -> tuple[int, int]:
    """Append transactions to the ledger, deduped by external_id.

    Returns (appended_count, duplicate_count). Never raises.
    """
    p = path or ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_existing_ids(p)
    appended = 0
    duplicates = 0
    with p.open("a", encoding="utf-8") as fh:
        for txn in transactions:
            if txn.external_id in existing:
                duplicates += 1
                continue
            fh.write(json.dumps(txn.to_dict()) + "\n")
            existing.add(txn.external_id)
            appended += 1
    return appended, duplicates


# --- Read side — consumed by observed_bills -------------------------------


def load_transactions(
    *,
    since: datetime | None = None,
    path: Path | None = None,
) -> list[BankTransaction]:
    """Read all ledger rows with ts >= since (or all if since is None)."""
    p = path or ledger_path()
    if not p.exists():
        return []
    out: list[BankTransaction] = []
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                ts_raw = rec.get("ts", "")
                try:
                    row_ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if row_ts < since:
                        continue
                except (ValueError, AttributeError):
                    pass
            out.append(
                BankTransaction(
                    ts=rec.get("ts", ""),
                    source=rec.get("source", ""),
                    external_id=rec.get("external_id", ""),
                    amount_usd=float(rec.get("amount_usd", 0.0)),
                    raw_description=rec.get("raw_description", ""),
                    vendor_registry_id=rec.get("vendor_registry_id", ""),
                    category=rec.get("category", "other"),
                    bill_signal_kind=rec.get("bill_signal_kind", ""),
                    card_ref=rec.get("card_ref", ""),
                    bank_category=rec.get("bank_category", ""),
                    note=rec.get("note", ""),
                )
            )
    return out


# --- CLI entry point ------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=os.getenv("SAMUS_LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description="Ingest a Cash App CSV into bank_activity ledger")
    parser.add_argument("csv_path", help="Path to Cash App activity CSV")
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse and print summary without appending"
    )
    args = parser.parse_args(argv)

    csv_path = Path(args.csv_path).expanduser()
    shape, txns = parse_activity_csv_auto(csv_path)

    # Print summary
    from collections import Counter

    cats = Counter(t.category for t in txns)
    vendors = Counter(t.vendor_registry_id for t in txns if t.vendor_registry_id)
    codb_spend = sum(t.amount_usd for t in txns if t.amount_usd < 0 and t.category == "bill")
    revenue = sum(t.amount_usd for t in txns if t.category == "revenue")
    business_transfer = sum(t.amount_usd for t in txns if t.category == "business_transfer")
    personal_spend = sum(
        t.amount_usd for t in txns if t.amount_usd < 0 and t.category == "personal"
    )

    print(f"Parsed {len(txns)} transactions from {csv_path.name} (shape={shape})")
    print(f"  Categories: {dict(cats)}")
    print(f"  Vendors matched: {len(vendors)} ({dict(vendors)})")
    print(f"  CODB spend:      ${codb_spend:,.2f}")
    print(f"  Business revenue: ${revenue:,.2f}")
    if business_transfer:
        print(f"  Owner<->LLC transfers: ${business_transfer:+,.2f} (funding + distributions)")
    if personal_spend:
        print(f"  Personal spend:  ${personal_spend:,.2f}")
    print(f"  Business net (revenue - CODB): ${revenue + codb_spend:,.2f}")

    if args.dry_run:
        print("DRY RUN — nothing written.")
        return 0

    appended, duplicates = append_transactions(txns)
    print(f"Appended {appended} new rows, skipped {duplicates} duplicates.")
    print(f"Ledger: {ledger_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BankTransaction",
    "parse_cash_app_csv",
    "append_transactions",
    "load_transactions",
    "ledger_path",
]
