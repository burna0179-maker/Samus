"""Tests for backend.growth.schema_registry.

Covers:
  - All 12 growth actions are present in GROWTH_SCHEMA_REGISTRY.
  - validate() catches missing required fields.
  - validate() passes (returns []) on a fully-populated payload.
  - get_schema() returns None for unknown actions.
  - GrowthActionSchema.validate() is pure (does not mutate payload).
"""

from __future__ import annotations

import pytest

from backend.growth.schema_registry import (
    GROWTH_SCHEMA_REGISTRY,
    get_schema,
)

# ---------------------------------------------------------------------------
# Expected 12 actions
# ---------------------------------------------------------------------------

ALL_ACTIONS = [
    # SEO
    "geo_format",
    "aio_analyze",
    "aio_probe",
    # Social / nurture
    "repurpose_blog_post",
    "plan_social_calendar",
    "dispatch_social_calendar",
    "plan_nurture",
    # Proof
    "generate_case_study",
    "build_proof_wall",
    # Referral
    "referral_code",
    "referral_record",
    "referral_qualify",
]


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


def test_all_12_actions_present():
    """Every expected action must have an entry in the registry."""
    for action in ALL_ACTIONS:
        assert action in GROWTH_SCHEMA_REGISTRY, (
            f"Action {action!r} missing from GROWTH_SCHEMA_REGISTRY"
        )


def test_registry_has_exactly_12_actions():
    assert len(GROWTH_SCHEMA_REGISTRY) == 12


def test_each_entry_action_key_matches_action_field():
    """Registry key must equal the schema's action field."""
    for key, schema in GROWTH_SCHEMA_REGISTRY.items():
        assert schema.action == key, f"Registry key {key!r} != schema.action {schema.action!r}"


def test_all_required_inputs_non_empty():
    """Every action must declare at least one required input."""
    for action, schema in GROWTH_SCHEMA_REGISTRY.items():
        assert schema.required_inputs, f"Action {action!r} has no required_inputs"


def test_all_output_fields_non_empty():
    """Every action must declare at least one output field."""
    for action, schema in GROWTH_SCHEMA_REGISTRY.items():
        assert schema.output_fields, f"Action {action!r} has no output_fields"


# ---------------------------------------------------------------------------
# get_schema helper
# ---------------------------------------------------------------------------


def test_get_schema_returns_entry_for_known_action():
    schema = get_schema("geo_format")
    assert schema is not None
    assert schema.action == "geo_format"


def test_get_schema_returns_none_for_unknown_action():
    assert get_schema("nonexistent_action") is None


def test_get_schema_returns_none_for_empty_string():
    assert get_schema("") is None


# ---------------------------------------------------------------------------
# validate() — missing required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,complete_payload,missing_one",
    [
        (
            "geo_format",
            {"query": "best coffee", "location": "Austin TX"},
            {"query": "best coffee"},  # missing location
        ),
        (
            "aio_analyze",
            {"url": "https://example.com"},
            {},  # missing url
        ),
        (
            "aio_probe",
            {"domain": "example.com"},
            {},  # missing domain
        ),
        (
            "repurpose_blog_post",
            {"content": "Hello world", "platform": "linkedin"},
            {"content": "Hello world"},  # missing platform
        ),
        (
            "plan_social_calendar",
            {"month": "2026-07", "platforms": ["linkedin"]},
            {"month": "2026-07"},  # missing platforms
        ),
        (
            "dispatch_social_calendar",
            {"calendar_id": "cal-001"},
            {},  # missing calendar_id
        ),
        (
            "plan_nurture",
            {"lead_id": "lead-1", "stage": "warm"},
            {"lead_id": "lead-1"},  # missing stage
        ),
        (
            "generate_case_study",
            {"client_id": "c-1", "outcome": "20% revenue lift"},
            {"client_id": "c-1"},  # missing outcome
        ),
        (
            "build_proof_wall",
            {"case_study_ids": ["cs-1", "cs-2"]},
            {},  # missing case_study_ids
        ),
        (
            "referral_code",
            {"referrer_id": "user-99"},
            {},  # missing referrer_id
        ),
        (
            "referral_record",
            {"code": "REF123", "referred_id": "user-42"},
            {"code": "REF123"},  # missing referred_id
        ),
        (
            "referral_qualify",
            {"referral_id": "ref-7"},
            {},  # missing referral_id
        ),
    ],
)
def test_validate_catches_missing_required_field(action, complete_payload, missing_one):
    schema = get_schema(action)
    assert schema is not None

    # Full payload => no missing fields
    assert schema.validate(complete_payload) == [], (
        f"Expected no missing fields for {action!r} with full payload"
    )

    # Payload missing one required field
    missing = schema.validate(missing_one)
    # At least one required field must be reported as missing
    assert len(missing) >= 1, f"Expected missing fields for {action!r} with incomplete payload"
    # Every reported field must actually be in required_inputs
    for f in missing:
        assert f in schema.required_inputs, (
            f"{f!r} reported missing but not in required_inputs for {action!r}"
        )


def test_validate_passes_on_full_payload_geo_format():
    schema = get_schema("geo_format")
    result = schema.validate({"query": "plumber near me", "location": "Denver CO"})
    assert result == []


def test_validate_passes_with_optional_fields_included():
    """Optional fields must not affect validation outcome."""
    schema = get_schema("geo_format")
    result = schema.validate(
        {
            "query": "plumber near me",
            "location": "Denver CO",
            "use_llm": False,
            "faq_count": 5,
            "brand_name": "Plumb Co",
        }
    )
    assert result == []


def test_validate_empty_payload_returns_all_required():
    schema = get_schema("referral_record")
    missing = schema.validate({})
    assert set(missing) == set(schema.required_inputs)


def test_validate_does_not_mutate_payload():
    """validate() must be a pure read operation."""
    schema = get_schema("geo_format")
    payload = {"query": "test"}
    original_keys = set(payload.keys())
    schema.validate(payload)
    assert set(payload.keys()) == original_keys


# ---------------------------------------------------------------------------
# GrowthActionSchema dataclass contract
# ---------------------------------------------------------------------------


def test_schema_is_frozen():
    """GrowthActionSchema must be immutable (frozen dataclass)."""
    schema = get_schema("geo_format")
    with pytest.raises((AttributeError, TypeError)):
        schema.action = "hacked"  # type: ignore[misc]


def test_schema_fields_are_lists():
    for action in ALL_ACTIONS:
        schema = get_schema(action)
        assert isinstance(schema.required_inputs, list)
        assert isinstance(schema.optional_inputs, list)
        assert isinstance(schema.output_fields, list)
