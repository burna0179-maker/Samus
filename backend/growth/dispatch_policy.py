"""Growth B-F dispatch policy — skeleton entries for each growth capability group.

This module owns the dispatch-policy table for the five growth-enrichment
capability groups introduced in Phase B through F:

  B/C  SEO visibility   — seo: geo_format, aio_analyze, aio_probe
  D/E  Social / nurture — outreach: repurpose_blog_post, plan_social_calendar,
                          dispatch_social_calendar, plan_nurture
  F    Proof / referral — crm: generate_case_study, build_proof_wall,
                          referral_code, referral_record, referral_qualify

Each entry declares:
  - ``workcell``   — the service whose ``/work`` endpoint handles the action
  - ``action``     — the action verb sent in ``metadata["action"]``
  - ``flag``       — feature-flag name that must be truthy for the entry to be
                     "live"; when false the entry is DRY_RUN-only / dormant
  - ``dry_run``    — whether the action is currently running in dry-run mode
  - ``notes``      — brief rationale / wiring status

The flags map to ``Settings`` fields or env-var names:

  SAMUS_GROWTH_SEO_ENABLED           — gates the seo visibility group
  SAMUS_GROWTH_SOCIAL_ENABLED        — gates the social / nurture group
  SAMUS_GROWTH_PROOF_ENABLED         — gates the proof / case-study group
  SAMUS_GROWTH_REFERRAL_ENABLED      — gates the referral group

All four default to **OFF** (``False``). The actual dispatch loop lives in
the workcell ``/work`` endpoints (seo/outreach/crm app.py); this module
provides the operator-visible policy table, the ``is_live`` predicate, and
the ``route_growth_action`` helper that validates capability + flag before
delegating to the workcell handler. It does NOT replace the in-process
``if action ==`` branches in the apps — those are the authoritative dispatch
path; this is the companion policy / scheduling surface.

Flag architecture: each group-level flag is read from ``Settings`` (injected
by ``bootstrap_settings`` / env var). Fail-closed: an unrecognised flag or an
import error always returns the action as not-live / dry-run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger("samus.growth.dispatch_policy")

# Lazy import guard — schema_registry lives in the same package; importing it
# at module load is safe, but we guard against circular-import edge cases in
# test environments by using a module-level None sentinel.
_SCHEMA_REGISTRY_MOD = None


def _schema_registry():
    """Return the schema_registry module, importing it on first call."""
    global _SCHEMA_REGISTRY_MOD  # noqa: PLW0603
    if _SCHEMA_REGISTRY_MOD is None:
        from backend.growth import schema_registry as _sr
        _SCHEMA_REGISTRY_MOD = _sr
    return _SCHEMA_REGISTRY_MOD

# ---------------------------------------------------------------------------
# Policy entries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GrowthDispatchEntry:
    """One action's dispatch policy."""

    workcell: str
    action: str
    flag: str          # settings attribute or env-var name (SAMUS_GROWTH_*_ENABLED)
    dry_run: bool      # True = the workcell handler itself runs DRY-RUN mode
    notes: str = ""

    @property
    def schema(self):
        """Return the :class:`~backend.growth.schema_registry.GrowthActionSchema`
        for this entry, or ``None`` if the action is not in the registry.

        Delegates to :func:`~backend.growth.schema_registry.get_schema` so the
        dispatch entry always reflects the canonical schema without duplicating
        the registry data.
        """
        return _schema_registry().get_schema(self.action)


# Canonical growth dispatch table. All entries are default-OFF and
# DRY-RUN to match the Phase B-F implementation posture.
GROWTH_DISPATCH_TABLE: tuple[GrowthDispatchEntry, ...] = (
    # --- Phase B/C: SEO visibility (GEO format + AIO measurement) ----------
    GrowthDispatchEntry(
        workcell="seo",
        action="geo_format",
        flag="SAMUS_GROWTH_SEO_ENABLED",
        dry_run=False,  # pure transform; no external calls; safe to run
        notes="GEO formatting + FAQ schema generation. No LLM when use_llm=False.",
    ),
    GrowthDispatchEntry(
        workcell="seo",
        action="aio_analyze",
        flag="SAMUS_GROWTH_SEO_ENABLED",
        dry_run=False,  # pure analytics over supplied answers; no outbound calls
        notes="AIO SOV analysis over pre-fetched platform answers. No token spend.",
    ),
    GrowthDispatchEntry(
        workcell="seo",
        action="aio_probe",
        flag="SAMUS_GROWTH_SEO_ENABLED",
        dry_run=True,   # live probing is further gated by SAMUS_VISIBILITY_PROBE_ENABLED
        notes="AIO live probe. Inner flag SAMUS_VISIBILITY_PROBE_ENABLED also required.",
    ),
    # --- Phase D/E: Social calendar + nurture ------------------------------
    GrowthDispatchEntry(
        workcell="outreach",
        action="repurpose_blog_post",
        flag="SAMUS_GROWTH_SOCIAL_ENABLED",
        dry_run=False,  # deterministic template transform; no network
        notes="Blog → 6 social asset repurposing. use_llm=False by default.",
    ),
    GrowthDispatchEntry(
        workcell="outreach",
        action="plan_social_calendar",
        flag="SAMUS_GROWTH_SOCIAL_ENABLED",
        dry_run=False,  # pure planning; no dispatch
        notes="60-post monthly social calendar planner. No outward send.",
    ),
    GrowthDispatchEntry(
        workcell="outreach",
        action="dispatch_social_calendar",
        flag="SAMUS_GROWTH_SOCIAL_ENABLED",
        dry_run=True,   # SAMUS_SOCIAL_DRY_RUN gates the actual platform post
        notes="Social calendar dispatcher. Dry-run until SAMUS_SOCIAL_DRY_RUN=false.",
    ),
    GrowthDispatchEntry(
        workcell="outreach",
        action="plan_nurture",
        flag="SAMUS_GROWTH_SOCIAL_ENABLED",
        dry_run=True,   # dry_run=True in the sequence handler by default
        notes="Email nurture sequence planner. Sends are dry-run by default.",
    ),
    # --- Phase F: Proof (case studies + proof wall) ------------------------
    GrowthDispatchEntry(
        workcell="crm",
        action="generate_case_study",
        flag="SAMUS_GROWTH_PROOF_ENABLED",
        dry_run=False,  # local generation only; no outward publish
        notes="Case study generator. Local-ledger only; no outward send.",
    ),
    GrowthDispatchEntry(
        workcell="crm",
        action="build_proof_wall",
        flag="SAMUS_GROWTH_PROOF_ENABLED",
        dry_run=False,  # aggregation over local data
        notes="Proof-wall aggregator. Local-ledger only.",
    ),
    # --- Phase F: Referral (code gen + record + qualify) -------------------
    GrowthDispatchEntry(
        workcell="crm",
        action="referral_code",
        flag="SAMUS_GROWTH_REFERRAL_ENABLED",
        dry_run=False,  # local code generation + ledger write; no payout
        notes="Referral code generator. No payout; local ledger only.",
    ),
    GrowthDispatchEntry(
        workcell="crm",
        action="referral_record",
        flag="SAMUS_GROWTH_REFERRAL_ENABLED",
        dry_run=False,  # local ledger write
        notes="Referral attribution recorder. No payout.",
    ),
    GrowthDispatchEntry(
        workcell="crm",
        action="referral_qualify",
        flag="SAMUS_GROWTH_REFERRAL_ENABLED",
        dry_run=False,  # local qualification check
        notes="Referral qualification check. No payout.",
    ),
)

# Index: action -> entry (for O(1) lookup)
_BY_ACTION: dict[str, GrowthDispatchEntry] = {e.action: e for e in GROWTH_DISPATCH_TABLE}

# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------

def _flag_enabled(flag: str) -> bool:
    """Resolve a SAMUS_GROWTH_*_ENABLED flag from env or Settings.

    Reads from environment directly so a live env change is picked up without
    a settings reload. Falls back to the cached ``Settings`` object for
    consistency with the rest of the stack. Fail-closed: any exception -> False.
    """
    try:
        raw = os.environ.get(flag, "").strip().lower()
        if raw:
            return raw in ("1", "true", "yes", "on", "y")
        # Not in env — check the Settings object.
        from backend.common.config import get_settings
        s = get_settings()
        # Settings doesn't have growth flags as typed fields (they're env-only
        # at this stage). Return False if not in env.
        return False
    except Exception:  # noqa: BLE001 — fail closed
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_entry(action: str) -> GrowthDispatchEntry | None:
    """Return the policy entry for ``action``, or None if not in the table."""
    return _BY_ACTION.get(action)


def is_live(action: str) -> bool:
    """Return True when the growth action's flag is enabled and dry_run is False.

    A ``dry_run=True`` action is never "live" regardless of its flag — it still
    runs, but in dry-run mode only. This lets the operator enable the flag to
    allow the action to run in dry-run and explicitly track the extra step
    (flipping ``dry_run`` to False in the table) to lift it fully.
    """
    entry = _BY_ACTION.get(action)
    if entry is None:
        return False
    if entry.dry_run:
        return False
    return _flag_enabled(entry.flag)


def is_enabled(action: str) -> bool:
    """Return True when the action's flag is enabled (dry_run OR live).

    An action is "enabled" if its group flag is on, regardless of dry_run.
    The workcell handler itself enforces the dry_run semantics.
    """
    entry = _BY_ACTION.get(action)
    if entry is None:
        return False
    return _flag_enabled(entry.flag)


def route_growth_action(
    action: str,
    payload: dict[str, Any],
    *,
    handler_map: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Route a growth action to its handler if the policy allows.

    Returns the handler's result dict, or None when:
      - the action is not in the growth table,
      - its group flag is disabled.

    When ``handler_map`` is supplied it is consulted first (test injection).
    Otherwise the canonical workcell dispatch functions are imported lazily.

    The handler must accept ``(payload: dict) -> dict``. Routing is fail-open
    on import errors — the caller's own ``unknown_action`` branch handles the
    gap — but flag-gating is fail-closed (unrecognised / missing flag -> None).
    """
    entry = _BY_ACTION.get(action)
    if entry is None:
        return None
    if not _flag_enabled(entry.flag):
        _LOG.debug(
            "growth_dispatch_policy: action=%s flag=%s disabled -> skip",
            action, entry.flag,
        )
        return None

    # Lazy import per-action — mirrors the pattern in the workcell /work routes.
    # Both the injected handler_map path and the lazy-import path are wrapped so
    # a raising handler is fail-open (returns None + logs), per the docstring.
    try:
        if handler_map and action in handler_map:
            return handler_map[action](payload)

        if action == "geo_format":
            from backend.seo.geo_format import handle_geo_format
            return handle_geo_format(payload)
        if action == "aio_analyze":
            from backend.visibility.probe import handle_aio_analyze
            return handle_aio_analyze(payload)
        if action == "aio_probe":
            from backend.visibility.probe import handle_aio_probe
            return handle_aio_probe(payload)
        if action == "repurpose_blog_post":
            from backend.social.dispatch import handle_repurpose
            return handle_repurpose(payload)
        if action == "plan_social_calendar":
            from backend.social.dispatch import handle_plan
            return handle_plan(payload)
        if action == "dispatch_social_calendar":
            from backend.social.dispatch import handle_dispatch
            return handle_dispatch(payload)
        if action == "plan_nurture":
            from backend.outreach.sequences import handle_plan_nurture
            return handle_plan_nurture(payload)
        if action == "generate_case_study":
            from backend.proof.generator import handle_generate_case_study
            return handle_generate_case_study(payload)
        if action == "build_proof_wall":
            from backend.proof.generator import handle_build_proof_wall
            return handle_build_proof_wall(payload)
        if action == "referral_code":
            from backend.referral.engine import handle_referral_code
            return handle_referral_code(payload)
        if action == "referral_record":
            from backend.referral.engine import handle_referral_record
            return handle_referral_record(payload)
        if action == "referral_qualify":
            from backend.referral.engine import handle_referral_qualify
            return handle_referral_qualify(payload)
    except Exception as exc:  # noqa: BLE001 — flag-gated; surface as None + log
        _LOG.error(
            "growth_dispatch_policy: action=%s handler raised: %s", action, exc,
        )
        return None

    return None


def policy_summary() -> list[dict[str, Any]]:
    """Return the full policy table as a list of dicts (for /show-config).

    Each dict includes the entry fields plus the resolved ``enabled`` and
    ``live`` predicates based on the current env.
    """
    return [
        {
            "workcell": e.workcell,
            "action": e.action,
            "flag": e.flag,
            "dry_run": e.dry_run,
            "enabled": _flag_enabled(e.flag),
            "live": is_live(e.action),
            "notes": e.notes,
        }
        for e in GROWTH_DISPATCH_TABLE
    ]


def validate_payload(action: str, payload: dict[str, Any]) -> list[str]:
    """Validate *payload* against the schema for *action*.

    Convenience wrapper that chains through the schema registry:

    1. Looks up the schema via :func:`~backend.growth.schema_registry.get_schema`.
    2. Calls :meth:`~backend.growth.schema_registry.GrowthActionSchema.validate`.
    3. Returns the list of missing required fields (empty list = valid).

    When *action* is unknown (not in the registry) returns a single-element
    list ``["<unknown action>"]`` so callers can distinguish "missing fields"
    from "unrecognised action" without an exception.

    Args:
        action:  Growth action verb.
        payload: The caller-supplied input dict.

    Returns:
        List of missing required field names, or ``["<unknown action>"]`` when
        the action is not in the registry.
    """
    schema = _schema_registry().get_schema(action)
    if schema is None:
        return ["<unknown action>"]
    return schema.validate(payload)


__all__ = [
    "GrowthDispatchEntry",
    "GROWTH_DISPATCH_TABLE",
    "get_entry",
    "is_live",
    "is_enabled",
    "route_growth_action",
    "policy_summary",
    "validate_payload",
]
