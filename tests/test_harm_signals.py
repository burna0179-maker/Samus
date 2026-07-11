"""G7 — three harm collectors: zero on missing data, real count on present,
fail-OPEN on lookup failure."""
from __future__ import annotations

from typing import Any

import pytest

from backend.strategy import harm_signals as hs


class _Store:
    def __init__(
        self, *, artifacts=None, conversations=None, contacts=None,
        opportunity=None, complaints=None,
        raise_on_artifacts=False, raise_on_conversations=False,
        raise_on_contacts=False, raise_on_complaints=False,
        raise_on_opportunity=False,
    ):
        self.artifacts = artifacts or []
        self.conversations = conversations or []
        self.contacts = contacts or []
        self.opportunity_row = opportunity
        self.complaints = complaints or []
        self.raise_on_artifacts = raise_on_artifacts
        self.raise_on_conversations = raise_on_conversations
        self.raise_on_contacts = raise_on_contacts
        self.raise_on_complaints = raise_on_complaints
        self.raise_on_opportunity = raise_on_opportunity

    def artifacts_for_opportunity(self, _: str) -> list[dict[str, Any]]:
        if self.raise_on_artifacts:
            raise RuntimeError("ddb down")
        return list(self.artifacts)

    def conversations_for_prospect(self, _: str) -> list[dict[str, Any]]:
        if self.raise_on_conversations:
            raise RuntimeError("ddb down")
        return list(self.conversations)

    def contacts_for_prospect(self, _: str) -> list[dict[str, Any]]:
        if self.raise_on_contacts:
            raise RuntimeError("ddb down")
        return list(self.contacts)

    def opportunity(self, _: str) -> dict[str, Any] | None:
        if self.raise_on_opportunity:
            raise RuntimeError("ddb down")
        return self.opportunity_row

    def complaint_recipients(self) -> list[str]:
        if self.raise_on_complaints:
            raise RuntimeError("ddb down")
        return list(self.complaints)


# --- retracted_claims_for ---------------------------------------------------

def test_retracted_zero_on_empty_artifacts():
    store = _Store(artifacts=[])
    assert hs.retracted_claims_for("op_x", store=store) == 0


def test_retracted_counts_artifacts_with_retracted_kind():
    store = _Store(artifacts=[
        {"kind": "retracted_claim", "title": "old"},
        {"kind": "proposal", "title": "draft"},
        {"kind": "content_retracted_v2", "title": ""},
    ])
    assert hs.retracted_claims_for("op_x", store=store) == 2


def test_retracted_counts_superseded_marker_in_title():
    store = _Store(artifacts=[
        {"kind": "other", "title": "Superseded: old SEO claim"},
        {"kind": "other", "title": "draft"},
    ])
    assert hs.retracted_claims_for("op_x", store=store) == 1


def test_retracted_fail_open_on_lookup_failure(caplog):
    store = _Store(raise_on_artifacts=True)
    with caplog.at_level("WARNING"):
        assert hs.retracted_claims_for("op_x", store=store) == 0


# --- unsubscribes_for -------------------------------------------------------

def test_unsubscribes_zero_when_opportunity_missing():
    store = _Store(opportunity=None)
    assert hs.unsubscribes_for("op_x", store=store) == 0


def test_unsubscribes_zero_when_no_prospect_id():
    store = _Store(opportunity={"prospect_id": ""})
    assert hs.unsubscribes_for("op_x", store=store) == 0


def test_unsubscribes_counts_unsubscribe_outcome():
    store = _Store(
        opportunity={"prospect_id": "p_1"},
        conversations=[
            {"outcome": "unsubscribe"},
            {"outcome": "booked"},
            {"outcome": "UNSUBSCRIBE"},  # case-insensitive
            {"outcome": ""},
        ],
    )
    assert hs.unsubscribes_for("op_x", store=store) == 2


def test_unsubscribes_fail_open_on_lookup_failure():
    store = _Store(
        opportunity={"prospect_id": "p_1"}, raise_on_conversations=True,
    )
    assert hs.unsubscribes_for("op_x", store=store) == 0


# --- complaints_for ---------------------------------------------------------

def test_complaints_zero_when_no_contacts():
    store = _Store(opportunity={"prospect_id": "p_1"}, contacts=[])
    assert hs.complaints_for("op_x", store=store) == 0


def test_complaints_intersects_emails_case_insensitive():
    store = _Store(
        opportunity={"prospect_id": "p_1"},
        contacts=[
            {"email": "Owner@Example.com"},
            {"email": "ops@example.com"},
            {"email": ""},
        ],
        complaints=["owner@example.com", "stranger@elsewhere.com"],
    )
    assert hs.complaints_for("op_x", store=store) == 1


def test_complaints_counts_each_matching_address():
    store = _Store(
        opportunity={"prospect_id": "p_1"},
        contacts=[
            {"email": "a@x.com"},
            {"email": "b@x.com"},
        ],
        complaints=["a@x.com", "a@x.com", "b@x.com", "c@x.com"],
    )
    # Two a@ + one b@ = 3 matches; c@ not a contact.
    assert hs.complaints_for("op_x", store=store) == 3


def test_complaints_fail_open_on_lookup_failure():
    store = _Store(
        opportunity={"prospect_id": "p_1"},
        contacts=[{"email": "a@x.com"}],
        raise_on_complaints=True,
    )
    assert hs.complaints_for("op_x", store=store) == 0
