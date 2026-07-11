"""Tests for the canonical ``/intake/form-schema`` endpoint + module.

Two-layer coverage:

  * Unit: ``backend.intake.form_schema`` returns a structurally-valid
    schema with the expected field set + every Literal value mirrored.
  * Endpoint: ``GET /intake/form-schema`` returns it over HTTP, CORS
    headers attach for the static-site origin, and the receiver still
    accepts a body built from the schema's accepted values.

The cross-check tests are the load-bearing ones — they're what protects
the schema from drifting away from ``OnboardingLeadRequest``.
"""

from __future__ import annotations

from typing import Any, get_args


def _fresh_app(monkeypatch, *, allowed_origins=None):
    """Mirror of the helper in test_intake_app.py — keep CORS-aware."""
    if allowed_origins is not None:
        monkeypatch.setenv(
            "SAMUS_INTAKE_ALLOWED_ORIGINS",
            ",".join(allowed_origins) if allowed_origins else "",
        )
    from backend.common.config import reload_settings

    reload_settings()
    from backend.intake.app import create_app

    return create_app()


def _client(monkeypatch, *, allowed_origins=None):
    from fastapi.testclient import TestClient

    return TestClient(_fresh_app(monkeypatch, allowed_origins=allowed_origins))


# ---------------------------------------------------------------------------
# Unit: form_schema module
# ---------------------------------------------------------------------------


def test_get_form_schema_basic_shape():
    from backend.intake.form_schema import SCHEMA_VERSION, get_form_schema

    schema = get_form_schema()
    assert schema.schema_version == SCHEMA_VERSION
    # 8 primary + 5 in the "Social presence (optional)" fieldset.
    assert len(schema.fields) == 13
    assert schema.submit.post_url == "/intake/onboarding"
    assert schema.submit.button_text  # non-empty
    assert schema.submit.success_message
    assert schema.submit.trust_note


def test_schema_field_names_match_request_model():
    """Every model field MUST appear in the schema and vice versa.

    This is the cross-check that catches drift. If someone adds a field
    to OnboardingLeadRequest without touching form_schema.py, this test
    fails with a clear diff.
    """
    from backend.intake.form_schema import get_form_schema
    from backend.intake.models import OnboardingLeadRequest

    schema_field_names = {f.name for f in get_form_schema().fields}
    model_field_names = set(OnboardingLeadRequest.model_fields.keys())
    assert schema_field_names == model_field_names, (
        f"drift: schema={schema_field_names} model={model_field_names}"
    )


def test_schema_required_flags_match_model():
    """Required fields in the schema must match model fields without defaults."""
    from backend.intake.form_schema import get_form_schema
    from backend.intake.models import OnboardingLeadRequest

    model_required = {
        name for name, info in OnboardingLeadRequest.model_fields.items() if info.is_required()
    }
    schema_required = {f.name for f in get_form_schema().fields if f.required}
    assert schema_required == model_required


def test_service_interest_options_match_literal():
    """Checkbox values for service_interest must be exactly the Literal members."""
    from backend.intake.form_schema import get_form_schema
    from backend.intake.models import ServiceInterest

    schema = get_form_schema()
    service = next(f for f in schema.fields if f.name == "service_interest")
    expected = set(get_args(ServiceInterest))
    assert set(service.options) == expected
    # Labels parallel options 1:1
    assert len(service.options) == len(service.option_labels)


def test_monthly_budget_options_match_literal():
    from backend.intake.form_schema import get_form_schema
    from backend.intake.models import MonthlyBudget

    schema = get_form_schema()
    budget = next(f for f in schema.fields if f.name == "monthly_budget")
    expected = set(get_args(MonthlyBudget))
    assert set(budget.options) == expected
    assert len(budget.options) == len(budget.option_labels)


def test_timeline_options_match_literal():
    from backend.intake.form_schema import get_form_schema
    from backend.intake.models import Timeline

    schema = get_form_schema()
    timeline = next(f for f in schema.fields if f.name == "timeline")
    expected = set(get_args(Timeline))
    assert set(timeline.options) == expected
    assert len(timeline.options) == len(timeline.option_labels)


def test_social_cadence_options_match_literal():
    """The Social presence cadence select must mirror the Literal enum."""
    from backend.intake.form_schema import get_form_schema
    from backend.intake.models import SocialCadencePref

    schema = get_form_schema()
    cadence = next(f for f in schema.fields if f.name == "social_cadence_pref")
    expected = set(get_args(SocialCadencePref))
    assert set(cadence.options) == expected
    assert len(cadence.options) == len(cadence.option_labels)


def test_social_fieldset_grouping():
    """All 5 social-hint fields must belong to the Social presence fieldset."""
    from backend.intake.form_schema import FS_SOCIAL_PRESENCE, get_form_schema

    schema = get_form_schema()
    ids = {fs.id for fs in schema.fieldsets}
    assert FS_SOCIAL_PRESENCE in ids

    grouped = {f.name for f in schema.fields if f.fieldset == FS_SOCIAL_PRESENCE}
    assert grouped == {
        "social_facebook",
        "social_instagram",
        "social_linkedin",
        "brand_voice_notes",
        "social_cadence_pref",
    }

    # The primary flow — everything ungrouped — stays untouched.
    primary = {f.name for f in schema.fields if not f.fieldset}
    assert primary == {
        "name",
        "email",
        "company",
        "website_url",
        "service_interest",
        "pain_points",
        "monthly_budget",
        "timeline",
    }


def test_social_fields_are_all_optional():
    """The whole Social presence fieldset must be skippable."""
    from backend.intake.form_schema import FS_SOCIAL_PRESENCE, get_form_schema

    schema = get_form_schema()
    for f in schema.fields:
        if f.fieldset == FS_SOCIAL_PRESENCE:
            assert not f.required, f"social field {f.name!r} must not be required"


def test_schema_response_is_serialisable():
    """JSON-roundtrip — the static site has to be able to parse it."""
    import json
    from backend.intake.form_schema import FormSchemaResponse, get_form_schema

    payload = get_form_schema().model_dump(mode="json")
    roundtripped = FormSchemaResponse.model_validate(json.loads(json.dumps(payload)))
    assert roundtripped == get_form_schema()


# ---------------------------------------------------------------------------
# Endpoint: GET /intake/form-schema
# ---------------------------------------------------------------------------


def test_endpoint_returns_canonical_schema(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/intake/form-schema")
    assert resp.status_code == 200
    body = resp.json()
    assert "schema_version" in body
    assert "fields" in body
    assert "submit" in body
    assert len(body["fields"]) == 13
    # Every field MUST carry a non-empty name + label + kind.
    for f in body["fields"]:
        assert f["name"]
        assert f["label"]
        assert f["kind"] in {"text", "email", "url", "textarea", "checkbox", "select"}
    # v3 introduces field groupings. The endpoint MUST expose fieldsets so
    # sites can render the Social presence group.
    assert isinstance(body.get("fieldsets"), list)
    fieldset_ids = {fs["id"] for fs in body["fieldsets"]}
    # Every field.fieldset MUST reference a declared fieldset id — a
    # dangling reference would silently drop that field on the site.
    for f in body["fields"]:
        if f.get("fieldset"):
            assert f["fieldset"] in fieldset_ids, (
                f"field {f['name']!r} references undeclared fieldset {f['fieldset']!r}"
            )


def test_endpoint_cors_preflight(monkeypatch):
    """OPTIONS preflight for the site origin should succeed."""
    client = _client(monkeypatch, allowed_origins=["https://hustleforge.tech"])
    resp = client.options(
        "/intake/form-schema",
        headers={
            "Origin": "https://hustleforge.tech",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://hustleforge.tech"
    # GET MUST be in the allow-methods header.
    allow_methods = (resp.headers.get("access-control-allow-methods") or "").upper()
    assert "GET" in allow_methods


def test_endpoint_disallows_unlisted_origin(monkeypatch):
    """Preflight from an origin NOT in the allow-list does NOT get CORS headers."""
    client = _client(monkeypatch, allowed_origins=["https://hustleforge.tech"])
    resp = client.options(
        "/intake/form-schema",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    # CORS allow-origin header is omitted (or echoes nothing) for unlisted
    # origins — the response may be 200 (CORS middleware doesn't 4xx the
    # preflight; the browser enforces by checking headers). What matters
    # is that the allow-origin header doesn't permit evil.example.
    assert resp.headers.get("access-control-allow-origin") != "https://evil.example"


# ---------------------------------------------------------------------------
# Round-trip: schema-built body posts cleanly through the receiver
# ---------------------------------------------------------------------------


def _build_body_from_schema() -> dict[str, Any]:
    """Construct a POST body using ONLY accepted values from the schema."""
    from backend.intake.form_schema import get_form_schema

    body: dict[str, Any] = {}
    for f in get_form_schema().fields:
        if f.name == "name":
            body[f.name] = "Jane"
        elif f.name == "email":
            body[f.name] = "jane@acme.com"
        elif f.name == "company":
            body[f.name] = "Acme"
        elif f.name == "website_url":
            body[f.name] = "https://acme.com"
        elif f.name == "service_interest":
            # Pick the first non-empty option as a representative choice.
            body[f.name] = [opt for opt in f.options if opt][:1]
        elif f.name == "pain_points":
            body[f.name] = "Manual follow-up is broken."
        elif f.name == "monthly_budget":
            # First non-empty enum value — empty string is also accepted
            # but using a real value exercises validation more strictly.
            body[f.name] = next(opt for opt in f.options if opt)
        elif f.name == "timeline":
            body[f.name] = next(opt for opt in f.options if opt)
        elif f.name == "social_facebook":
            body[f.name] = "facebook.com/acme"
        elif f.name == "social_instagram":
            body[f.name] = "@acme"
        elif f.name == "social_linkedin":
            body[f.name] = "linkedin.com/company/acme"
        elif f.name == "brand_voice_notes":
            body[f.name] = "Friendly, direct, no jargon."
        elif f.name == "social_cadence_pref":
            body[f.name] = next(opt for opt in f.options if opt)
    return body


def test_body_built_from_schema_validates_against_request_model():
    """A body assembled from the schema's accepted values MUST validate."""
    from backend.intake.models import OnboardingLeadRequest

    body = _build_body_from_schema()
    # If this raises, the schema's accepted-values claim is a lie.
    parsed = OnboardingLeadRequest.model_validate(body)
    assert parsed.name == "Jane"
    # Social-presence fields (v3) MUST round-trip so the campaign engine's
    # bootstrap sees the operator's real handles.
    assert parsed.social_facebook == "facebook.com/acme"
    assert parsed.social_instagram == "@acme"
    assert parsed.social_linkedin == "linkedin.com/company/acme"
    assert parsed.brand_voice_notes == "Friendly, direct, no jargon."
    assert parsed.social_cadence_pref == "light"


# ---------------------------------------------------------------------------
# End-to-end: POST /intake/onboarding lands the social fields on StoredLead
# ---------------------------------------------------------------------------


class _FakeTable:
    def __init__(self):
        self.items: list[dict[str, Any]] = []

    def put_item(self, Item):
        self.items.append(Item)

    def scan(self, **kwargs):
        return {"Items": list(self.items)}


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.intake.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def _patch_table(monkeypatch, table: _FakeTable):
    import backend.intake.service as svc_mod

    monkeypatch.setattr(svc_mod, "_leads_table", lambda: table)


def _audit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_INTAKE_AUDIT_PATH", str(tmp_path / "intake_audit.jsonl"))


def test_form_roundtrip_persists_social_fields(tmp_path, monkeypatch):
    """A schema-built POST lands with the social hints on the DDB row.

    This is the load-bearing test the "form-first" invariant exists for —
    it proves the form's Social presence fieldset actually flows through
    to the persisted lead, not just the pydantic model.
    """
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    client = _client(monkeypatch)

    body = _build_body_from_schema()
    resp = client.post("/intake/onboarding", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"

    assert len(table.items) == 1
    row = table.items[0]
    assert row["social_facebook"] == "facebook.com/acme"
    assert row["social_instagram"] == "@acme"
    assert row["social_linkedin"] == "linkedin.com/company/acme"
    assert row["brand_voice_notes"] == "Friendly, direct, no jargon."
    assert row["social_cadence_pref"] == "light"


def test_form_roundtrip_social_fields_default_when_omitted(tmp_path, monkeypatch):
    """Skipping the Social presence fieldset is legal (all fields optional).

    An old client — or a lead that just doesn't share — must not 422 out.
    Every social field defaults to empty string.
    """
    _reset_idempotency(monkeypatch)
    _audit_to_tmp(monkeypatch, tmp_path)
    table = _FakeTable()
    _patch_table(monkeypatch, table)
    client = _client(monkeypatch)

    # Body missing every social_* / brand_voice_notes field entirely.
    minimal = {
        "name": "Jane",
        "email": "jane@acme.com",
        "company": "",
        "website_url": "",
        "service_interest": [],
        "pain_points": "Manual follow-up is broken.",
        "monthly_budget": "",
        "timeline": "",
    }
    resp = client.post("/intake/onboarding", json=minimal)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"

    row = table.items[0]
    assert row["social_facebook"] == ""
    assert row["social_instagram"] == ""
    assert row["social_linkedin"] == ""
    assert row["brand_voice_notes"] == ""
    assert row["social_cadence_pref"] == ""
