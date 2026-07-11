"""Routing + governance rules for campaign nodes.

Single source of truth for:

* which ``(target_workcell, capability)`` pairs a node may dispatch to — read
  straight from ``backend.common.capabilities.SERVICE_CAPABILITIES`` so the
  campaign layer can never invent a capability a workcell does not expose;
* per node-type defaults (audit severity + baseline approval level);
* which node types are *high-risk* and therefore must never auto-run without an
  approval gate (deliverable §8 — public posting, bulk outreach, paid ads,
  external backlinks, website edits, client-facing reports, mass email/SMS,
  review requests);
* discovery of on-disk template YAML files.

Keeping this declarative and centralized means the executor stays mechanical:
it asks the registry "is this legal / high-risk / where does it default" and
never hard-codes a workcell name.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from backend.common.capabilities import SERVICE_CAPABILITIES

from .models import ApprovalLevel, AuditSeverity


# ``campaigns`` runs several node types in-process (KPI collection, reporting)
# rather than dispatching over HTTP to itself. The executor special-cases this
# workcell; the registry still validates the capability exists.
LOCAL_WORKCELL = "campaigns"


@dataclass(frozen=True)
class NodeTypeSpec:
    """Defaults + risk classification for a campaign node ``type``."""

    high_risk: bool
    default_severity: AuditSeverity
    default_approval: ApprovalLevel


# The node-type vocabulary. ``high_risk`` types perform an outward, hard-to-
# reverse effect (something the public / a client / a search engine sees) and
# are FAIL-CLOSED: the executor refuses to run one whose ``approval_required``
# resolves to ``none``.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    # --- inward / reversible work: safe to auto-run ---
    "artifact_ingest":    NodeTypeSpec(False, AuditSeverity.INFO, ApprovalLevel.NONE),
    "seo_audit":          NodeTypeSpec(False, AuditSeverity.INFO, ApprovalLevel.NONE),
    "audience_discovery": NodeTypeSpec(False, AuditSeverity.INFO, ApprovalLevel.NONE),
    "funnel_plan":        NodeTypeSpec(False, AuditSeverity.NOTICE, ApprovalLevel.OPERATOR),
    "content_generation": NodeTypeSpec(False, AuditSeverity.NOTICE, ApprovalLevel.OPERATOR),
    "seo_geo_content":    NodeTypeSpec(False, AuditSeverity.NOTICE, ApprovalLevel.OPERATOR),
    "metrics_collection": NodeTypeSpec(False, AuditSeverity.INFO, ApprovalLevel.NONE),
    # --- outward / hard-to-reverse: high-risk, approval required ---
    "public_action":      NodeTypeSpec(True, AuditSeverity.CRITICAL, ApprovalLevel.CLIENT),
    "external_outreach":  NodeTypeSpec(True, AuditSeverity.CRITICAL, ApprovalLevel.OPERATOR),
    "paid_ads":           NodeTypeSpec(True, AuditSeverity.CRITICAL, ApprovalLevel.OPERATOR),
    "backlink":           NodeTypeSpec(True, AuditSeverity.CRITICAL, ApprovalLevel.GOVERNANCE),
    "website_edit":       NodeTypeSpec(True, AuditSeverity.HIGH, ApprovalLevel.OPERATOR),
    "mass_message":       NodeTypeSpec(True, AuditSeverity.CRITICAL, ApprovalLevel.CLIENT),
    "review_request":     NodeTypeSpec(True, AuditSeverity.HIGH, ApprovalLevel.OPERATOR),
    # client-facing report — reversible but seen by the client, so gated.
    "reporting":          NodeTypeSpec(False, AuditSeverity.NOTICE, ApprovalLevel.OPERATOR),
}

# Fallback for an unrecognized node type: treat as high-risk (fail-closed —
# an unknown effect is assumed dangerous until the type is classified).
_UNKNOWN_SPEC = NodeTypeSpec(True, AuditSeverity.HIGH, ApprovalLevel.OPERATOR)


def spec_for(node_type: str) -> NodeTypeSpec:
    """Return the defaults/risk spec for ``node_type`` (fail-closed default)."""
    return NODE_TYPE_SPECS.get(node_type, _UNKNOWN_SPEC)


def is_high_risk(node_type: str) -> bool:
    """True iff a node of this type performs an outward, hard-to-reverse effect."""
    return spec_for(node_type).high_risk


def is_valid_pair(target_workcell: str, capability: str) -> bool:
    """True iff ``target_workcell`` exposes ``capability`` (per the mesh matrix)."""
    return capability in SERVICE_CAPABILITIES.get(target_workcell, set())


def known_workcell(target_workcell: str) -> bool:
    return target_workcell in SERVICE_CAPABILITIES


class InvalidRoutingError(ValueError):
    """Raised when a node targets an unknown workcell or capability pair."""


def assert_routable(node_id: str, target_workcell: str, capability: str) -> None:
    """Raise :class:`InvalidRoutingError` if the node's routing is illegal."""
    if not known_workcell(target_workcell):
        raise InvalidRoutingError(
            f"node {node_id!r}: unknown target workcell {target_workcell!r}"
        )
    if not is_valid_pair(target_workcell, capability):
        raise InvalidRoutingError(
            f"node {node_id!r}: workcell {target_workcell!r} does not expose "
            f"capability {capability!r}"
        )


# ---------------------------------------------------------------------------
# Template discovery
# ---------------------------------------------------------------------------

_TEMPLATES_DIR_ENV = "SAMUS_CAMPAIGN_TEMPLATES_DIR"


def templates_dir() -> Path:
    """Directory holding shipped template YAML files.

    Overridable via ``SAMUS_CAMPAIGN_TEMPLATES_DIR``; defaults to the
    ``templates/`` folder shipped alongside this package.
    """
    override = os.getenv(_TEMPLATES_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "templates"


def discover_template_files() -> dict[str, Path]:
    """Map ``<stem>`` -> path for every ``*.yaml`` template on disk."""
    out: dict[str, Path] = {}
    root = templates_dir()
    if not root.exists():
        return out
    for child in sorted(root.iterdir()):
        if child.suffix.lower() in (".yaml", ".yml") and child.is_file():
            out[child.stem] = child
    return out
