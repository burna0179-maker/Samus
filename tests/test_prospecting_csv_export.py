"""CSV exporter writes the canonical 37-column schema."""

from __future__ import annotations

import csv
from datetime import date


def test_csv_round_trip(tmp_path, monkeypatch):
    # Patch both the module attribute and the env var so all storage code paths
    # resolve to the same tmp_path regardless of which mechanism a module uses.
    import backend.common.storage as storage

    monkeypatch.setattr(storage, "_ROOT", tmp_path)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))

    from backend.prospecting import csv_export
    from backend.prospecting.models import ProspectRecord

    p = ProspectRecord(
        prospect_id="pr_t1",
        account_id="acct_t1",
        company_name="Acme Roofing",
        phone="(530) 111-2222",
        website_url="https://acme-roofing.example",
        city="Yuba City",
        state="CA",
        zipcode="95993",
        industry="roofing",
        call_priority="warm",
        lead_score=58,
        seo_score=44,
        callsheet_opener="Hi, this is Test calling Acme Roofing.",
    )
    out = csv_export.write_call_list([p], run_date=date(2026, 5, 14))
    assert out.exists()
    assert out.name == "call_list_2026-05-14.csv"
    assert out.parent.name == "daily_calls"

    with out.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        row = next(reader)

    assert header == list(csv_export.CSV_COLUMNS)
    assert len(row) == len(csv_export.CSV_COLUMNS)
    by_col = dict(zip(header, row))
    assert by_col["prospect_id"] == "pr_t1"
    assert by_col["company_name"] == "Acme Roofing"
    assert by_col["call_priority"] == "warm"
    assert by_col["lead_score"] == "58"


def test_csv_columns_count_and_order():
    from backend.prospecting import csv_export

    expected = (
        "prospect_id",
        "account_id",
        "company_name",
        "phone",
        "website_url",
        "website_status",
        "city",
        "state",
        "zipcode",
        "industry",
        "call_priority",
        "lead_score",
        "policy_family",
        "llm_cost_usd",
        "seo_score",
        "security_grade",
        "owner_name",
        "owner_email",
        "owner_title",
        "owner_linkedin_url",
        "contact_emails",
        "review_rating",
        "review_count",
        "business_description",
        "business_hours",
        "business_categories",
        "social_facebook",
        "social_instagram",
        "social_linkedin",
        "negative_review_snippets",
        "callsheet_issues",
        "callsheet_offer",
        "callsheet_pitch",
        "callsheet_opener",
        "callsheet_voicemail",
        "callsheet_objections",
        "callsheet_finding",
        # Recycle-pass context (2026-07-03): returning prospects carry the
        # follow-up flag + prior-touch digest into every downstream composer.
        "recycled",
        "prior_touch_summary",
    )
    assert csv_export.CSV_COLUMNS == expected
    assert len(csv_export.CSV_COLUMNS) == 39


def test_csv_policy_family_column_round_trips(tmp_path, monkeypatch):
    """The new policy_family column is written and reads back unchanged."""
    import backend.common.storage as storage

    monkeypatch.setattr(storage, "_ROOT", tmp_path)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))

    from backend.prospecting import csv_export
    from backend.prospecting.models import ProspectRecord

    p = ProspectRecord(
        prospect_id="pr_pf1",
        company_name="Policy Co",
        industry="hvac",
        policy_family="fast_quote_mode",
    )
    out = csv_export.write_call_list([p], run_date=date(2026, 5, 20))

    with out.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        row = next(reader)

    assert "policy_family" in header
    by_col = dict(zip(header, row))
    assert by_col["policy_family"] == "fast_quote_mode"
