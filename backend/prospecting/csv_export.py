"""CSV export — writes call_list_YYYY-MM-DD.csv (canonical 37-column schema).

Output location: ``<artifact_root>/daily_calls/`` where artifact_root is
``backend.common.storage.root()`` (`SAMUS_ARTIFACT_ROOT` env override or default).
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from backend.common import storage

from .models import ProspectRecord


CSV_COLUMNS: tuple[str, ...] = (
    "prospect_id", "account_id", "company_name", "phone", "website_url",
    "website_status",
    "city", "state", "zipcode", "industry",
    "call_priority", "lead_score", "policy_family", "llm_cost_usd",
    "seo_score", "security_grade",
    "owner_name", "owner_email", "owner_title", "owner_linkedin_url",
    "contact_emails",
    "review_rating", "review_count",
    "business_description", "business_hours", "business_categories",
    "social_facebook", "social_instagram", "social_linkedin",
    "negative_review_snippets",
    "callsheet_issues", "callsheet_offer", "callsheet_pitch",
    "callsheet_opener", "callsheet_voicemail", "callsheet_objections",
    "callsheet_finding",
    # Recycle-pass context (2026-07-03; additive — every consumer reads by
    # column name and tolerates absence in older files).
    "recycled", "prior_touch_summary",
)


# Leading characters a spreadsheet treats as the start of a formula. Scraped /
# enriched fields (company_name, business_description, owner_name, …) are
# attacker-influenced; a cell starting with one of these executes when the
# operator opens the CSV in Excel / Sheets — CWE-1236.
_CSV_FORMULA_TRIGGERS: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def _neutralize_cell(value: str) -> str:
    """Prefix a single quote when ``value`` could be read as a formula.

    A leading ``= + - @``, tab, or CR makes a spreadsheet evaluate the cell;
    the leading ``'`` forces it to stay literal text.
    """
    if value and value[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


def _row(p: ProspectRecord) -> list[str]:
    dumped = p.model_dump()
    out: list[str] = []
    for col in CSV_COLUMNS:
        value = dumped.get(col, "")
        cell = "" if value is None else str(value)
        out.append(_neutralize_cell(cell))
    return out


def write_call_list(
    prospects: list[ProspectRecord],
    *,
    run_date: date | None = None,
) -> Path:
    """Write (MERGE) call_list_YYYY-MM-DD.csv under <artifact_root>/daily_calls/.

    Same-day re-runs used to OVERWRITE the file — last write wins — so a
    second discovery (different verticals, a self-supply replenish, a manual
    recovery run) silently dropped every prospect the first run produced
    (bit hard 2026-07-03: a fresh-vertical run erased the morning's 43-row
    pool mid-production-day). Now: this batch's rows win for their own
    prospect_ids (fresher data), and existing rows for OTHER prospect_ids
    are preserved, so the day's pool only ever grows.
    """
    run_date = run_date or date.today()
    target_dir = storage.root() / "daily_calls"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"call_list_{run_date.isoformat()}.csv"

    new_rows = [_row(p) for p in prospects]
    new_ids = {r[0] for r in new_rows}  # CSV_COLUMNS[0] == prospect_id

    carried: list[list[str]] = []
    if path.is_file():
        try:
            with path.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for existing in reader:
                    pid = (existing.get("prospect_id") or "").strip()
                    if not pid or pid in new_ids:
                        continue
                    carried.append(
                        ["" if existing.get(col) is None else str(existing.get(col, ""))
                         for col in CSV_COLUMNS]
                    )
        except Exception:  # noqa: BLE001 — an unreadable prior file must not
            # block the fresh batch; fall back to write-only (old behaviour).
            carried = []

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for row in carried:
            writer.writerow(row)
        for row in new_rows:
            writer.writerow(row)
    return path
