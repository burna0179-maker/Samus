"""Growth B-F action schema registry.

Maps each of the 12 growth capability actions to its expected input/output
schema — required fields, optional fields, and output fields. The registry
is the authoritative source for payload validation before dispatch.

Flag architecture: the registry itself is always importable (it is pure data).
The dispatcher and scheduler are responsible for checking their own flags
before invoking registry-validated payloads.

Usage::

    from backend.growth.schema_registry import get_schema, GROWTH_SCHEMA_REGISTRY

    schema = get_schema("geo_format")
    missing = schema.validate({"query": "...", "location": "..."})   # -> []
    missing = schema.validate({"query": "..."})                      # -> ["location"]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Schema dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrowthActionSchema:
    """Input/output schema for a single growth action.

    Attributes:
        action:          Action verb (matches the dispatch table).
        required_inputs: Fields the caller MUST supply.
        optional_inputs: Fields the caller MAY supply (no effect on validation).
        output_fields:   Fields the handler guarantees in the response dict.
    """

    action: str
    required_inputs: list[str] = field(default_factory=list)
    optional_inputs: list[str] = field(default_factory=list)
    output_fields: list[str] = field(default_factory=list)

    def validate(self, payload: dict[str, Any]) -> list[str]:
        """Return a list of required fields missing from *payload*.

        An empty list means the payload satisfies the schema. The check is
        purely structural (field presence); type validation is left to the
        workcell handler.

        Args:
            payload: The incoming action payload dict.

        Returns:
            List of missing required field names (empty on success).
        """
        return [f for f in self.required_inputs if f not in payload]


# ---------------------------------------------------------------------------
# Registry — 12 actions covering Phase B/C/D/E/F
# ---------------------------------------------------------------------------

GROWTH_SCHEMA_REGISTRY: dict[str, GrowthActionSchema] = {
    # ------------------------------------------------------------------
    # Phase B/C: SEO visibility (GEO format + AIO measurement)
    # ------------------------------------------------------------------
    "geo_format": GrowthActionSchema(
        action="geo_format",
        required_inputs=["query", "location"],
        optional_inputs=["use_llm", "faq_count", "brand_name"],
        output_fields=["geo_block", "faq_schema", "word_count"],
    ),
    "aio_analyze": GrowthActionSchema(
        action="aio_analyze",
        required_inputs=["url"],
        optional_inputs=["platform_answers", "brand_name", "competitors"],
        output_fields=["sov_score", "mention_count", "platform_breakdown"],
    ),
    "aio_probe": GrowthActionSchema(
        action="aio_probe",
        required_inputs=["domain"],
        optional_inputs=["platforms", "timeout_seconds"],
        output_fields=["probe_results", "brand_mentions", "timestamp"],
    ),
    # ------------------------------------------------------------------
    # Phase D/E: Social calendar + nurture sequences
    # ------------------------------------------------------------------
    "repurpose_blog_post": GrowthActionSchema(
        action="repurpose_blog_post",
        required_inputs=["content", "platform"],
        optional_inputs=["tone", "hashtags", "use_llm", "brand_voice"],
        output_fields=["assets", "platform", "asset_count"],
    ),
    "plan_social_calendar": GrowthActionSchema(
        action="plan_social_calendar",
        required_inputs=["month", "platforms"],
        optional_inputs=["brand_name", "topics", "post_frequency", "use_llm"],
        output_fields=["calendar", "post_count", "platforms"],
    ),
    "dispatch_social_calendar": GrowthActionSchema(
        action="dispatch_social_calendar",
        required_inputs=["calendar_id"],
        optional_inputs=["dry_run", "platform_tokens"],
        output_fields=["dispatched_count", "failed_count", "dry_run"],
    ),
    "plan_nurture": GrowthActionSchema(
        action="plan_nurture",
        required_inputs=["lead_id", "stage"],
        optional_inputs=["sequence_length", "tone", "use_llm", "dry_run"],
        output_fields=["sequence", "email_count", "lead_id", "stage"],
    ),
    # ------------------------------------------------------------------
    # Phase F: Proof (case studies + proof wall)
    # ------------------------------------------------------------------
    "generate_case_study": GrowthActionSchema(
        action="generate_case_study",
        required_inputs=["client_id", "outcome"],
        optional_inputs=["use_llm", "format", "include_metrics"],
        output_fields=["case_study_id", "title", "body", "client_id"],
    ),
    "build_proof_wall": GrowthActionSchema(
        action="build_proof_wall",
        required_inputs=["case_study_ids"],
        optional_inputs=["layout", "brand_name", "sort_by"],
        output_fields=["proof_wall_id", "case_study_count", "html_fragment"],
    ),
    # ------------------------------------------------------------------
    # Phase F: Referral (code gen + record + qualify)
    # ------------------------------------------------------------------
    "referral_code": GrowthActionSchema(
        action="referral_code",
        required_inputs=["referrer_id"],
        optional_inputs=["campaign_id", "expiry_days", "max_uses"],
        output_fields=["code", "referrer_id", "created_at"],
    ),
    "referral_record": GrowthActionSchema(
        action="referral_record",
        required_inputs=["code", "referred_id"],
        optional_inputs=["source", "metadata"],
        output_fields=["attribution_id", "code", "referred_id", "recorded_at"],
    ),
    "referral_qualify": GrowthActionSchema(
        action="referral_qualify",
        required_inputs=["referral_id"],
        optional_inputs=["qualification_rules"],
        output_fields=["qualified", "referral_id", "reason"],
    ),
}


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def get_schema(action: str) -> GrowthActionSchema | None:
    """Return the schema for *action*, or None if the action is unknown.

    Args:
        action: The growth action verb (e.g. ``"geo_format"``).

    Returns:
        :class:`GrowthActionSchema` when found, ``None`` otherwise.
    """
    return GROWTH_SCHEMA_REGISTRY.get(action)


__all__ = [
    "GrowthActionSchema",
    "GROWTH_SCHEMA_REGISTRY",
    "get_schema",
]
