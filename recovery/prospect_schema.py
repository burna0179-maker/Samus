#!/usr/bin/env python3
"""
Prospect + CRM schema — DynamoDB table definitions for v1.7 prospect lane
Source: ChatGPT recovery chat 08 (samus_prospects table spec)

Canonical relationship:
- [EXPANDS §6 data plane] adds 7-table CRM (accounts / contacts / prospects / opportunities
                          / activities / artifacts / operator_tasks)
- [EXPANDS canonical_v1 Annex_SchemaCatalog] new agent-specific persistence
- Reference: memory project_samus_crm_design (7-table CRM, never ran)
"""

from __future__ import annotations

from typing import Any, Dict


SAMUS_PROSPECTS_TABLE = {
    "TableName": "samus_prospects",
    "PrimaryKey": "prospect_id",
    "GlobalSecondaryIndexes": [
        {"name": "by_zipcode", "key": "zipcode"},
        {"name": "by_status", "key": "status"},
        {"name": "by_priority", "key": "call_priority"},
    ],
    "Fields": {
        "prospect_id": "str (PK)",
        "company_name": "str",
        "zipcode": "str",
        "city": "str",
        "state": "str",
        "phone": "str",
        "website_url": "str",
        "industry": "str",
        "source": "str  # google_places | manual | import",
        "seo_score": "float",
        "lead_score": "float",
        "status": "str  # discovered|qualified|audited|call_ready|called|interested|proposal_sent|closed_won|closed_lost",
        "call_priority": "str  # hot|warm|low",
        "last_crawled_at": "iso8601",
        "notes": "str",
        "assigned_to": "str | None",
        "attempt_count": "int",
        "created_at": "iso8601",
        "updated_at": "iso8601",
    },
}


# ----- 7-table CRM (from memory: project_samus_crm_design) -----

ACCOUNTS_TABLE = {
    "TableName": "samus_accounts",
    "PrimaryKey": "account_id",
    "Fields": {
        "account_id": "str (PK)",
        "name": "str",
        "industry": "str",
        "size_tier": "str  # smb|mid|enterprise",
        "annual_revenue_est": "float",
        "primary_contact_id": "str",
        "stage": "str  # cold|warm|engaged|customer|churned",
        "created_at": "iso8601",
    },
}

CONTACTS_TABLE = {
    "TableName": "samus_contacts",
    "PrimaryKey": "contact_id",
    "Fields": {
        "contact_id": "str (PK)",
        "account_id": "str (FK → accounts)",
        "name": "str",
        "role": "str",
        "email": "str",
        "phone": "str",
        "linkedin": "str",
        "preferred_channel": "str  # email|phone|sms|linkedin",
        "do_not_contact": "bool",
    },
}

OPPORTUNITIES_TABLE = {
    "TableName": "samus_opportunities",
    "PrimaryKey": "opportunity_id",
    "Fields": {
        "opportunity_id": "str (PK)",
        "account_id": "str (FK)",
        "stage": "str  # new|qualified|proposal|negotiation|closed_won|closed_lost",
        "deal_size_est": "float",
        "close_probability": "float",
        "next_step": "str",
        "expected_close": "iso8601",
        "stake_sentence": "str  # operator-authored line; outreach refuses while empty",
        "stake_sentence_authored_by": "str  # operator id/username",
        "stake_sentence_authored_at": "iso8601  # set when stake_sentence is written",
    },
}

ACTIVITIES_TABLE = {
    "TableName": "samus_activities",
    "PrimaryKey": "activity_id",
    "Fields": {
        "activity_id": "str (PK)",
        "account_id": "str (FK)",
        "contact_id": "str (FK | None)",
        "kind": "str  # call|email|sms|meeting|note",
        "outcome": "str  # connected|no_answer|voicemail|interested|not_interested",
        "duration_sec": "int",
        "transcript_ref": "str | None",
        "ts": "iso8601",
    },
}

ARTIFACTS_TABLE = {
    "TableName": "samus_artifacts",
    "PrimaryKey": "artifact_id",
    "Fields": {
        "artifact_id": "str (PK)",
        "kind": "str  # CALL_SHEET|FULFILLMENT_PLAN|PROPOSAL|SEO_AUDIT|CONTENT",
        "owner_id": "str  # prospect_id | account_id | opportunity_id",
        "data": "json",
        "created_at": "iso8601",
    },
}

OPERATOR_TASKS_TABLE = {
    "TableName": "samus_operator_tasks",
    "PrimaryKey": "task_id",
    "Fields": {
        "task_id": "str (PK)",
        "kind": "str  # call|review|sign|deliver",
        "assignee": "str",
        "due_at": "iso8601",
        "status": "str  # open|in_progress|done|skipped",
        "ref": "str  # related entity id",
    },
}


ALL_TABLES: Dict[str, Dict[str, Any]] = {
    "samus_prospects": SAMUS_PROSPECTS_TABLE,
    "samus_accounts": ACCOUNTS_TABLE,
    "samus_contacts": CONTACTS_TABLE,
    "samus_opportunities": OPPORTUNITIES_TABLE,
    "samus_activities": ACTIVITIES_TABLE,
    "samus_artifacts": ARTIFACTS_TABLE,
    "samus_operator_tasks": OPERATOR_TASKS_TABLE,
}


# ----- New SEO scoring inputs added for local-business sales (chat 08) -----
LOCAL_SEO_SCORE_INPUTS = (
    "title_missing", "meta_missing", "h1_missing",
    "no_location_pages", "weak_local_schema", "poor_mobile_ux",
    "no_cta", "no_booking_or_contact_flow", "outdated_design",
    "no_ssl_or_mixed_content", "slow_page", "no_service_area_coverage",
    "no_gbp_consistency", "niche_fit_score", "revenue_likelihood",
    "phone_present_and_callable",
)
