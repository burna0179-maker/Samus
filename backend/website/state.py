"""WebsiteBuildState — the durable per-order state machine for the build walk.

Once an order is opened the orchestrator walks it through an ordered sequence
of stages. The walk is durable: each stage's outcome is persisted under the
state root keyed by ``order_id``, so re-processing the same order (an SQS
redelivery, a resumed walk-through, an autonomous retry) is idempotent —
already-completed stages are skipped.

This module owns only the state shape + its load/save and the two control
surfaces that make supervised-now == autonomous-later:

  * ``completed_stages`` — what has actually run (idempotency).
  * ``approved_stages``  — what the operator has signed off to run. In
    supervised mode the orchestrator runs a stage only if it is approved; in
    autonomous mode approval is auto-granted. This is the human-in-the-loop
    seam, and it is *data*, not a code branch — so lifting supervision is a
    flag flip, never a code change.

Stage logic lives in :mod:`backend.website.stages`; orchestration in
:mod:`backend.website.service`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.common.dates import iso_now
from backend.common.state_paths import state_path

from .models import WebsiteOrder

_LOG = logging.getLogger("samus.website.state")

# The ordered build walk. ``done`` is the terminal state after settle.
#   brief         — confirm/assemble the build spec (parks if pages missing)
#   generate      — taste-governed copy/SEO generation (the "frontend codegen")
#   provision     — create (or adopt) the Wix site; capture site_id
#   business_info — set name/contact/address/hours via Site Properties
#   content       — populate pages into the template's CMS collections
#   media         — AI visuals (Gemini image / Veo video) -> Wix Media -> CMS refs
#   seo           — per-page meta/slugs/business description
#   qa            — read back content + compile a preview checklist for sign-off
#   publish       — go live (OUTWARD: needs website_live_publish_enabled)
#   deliver       — hand the live URL to the customer, record delivery (OUTWARD)
#   settle        — record the financial event (invoice / barter repayment)
STAGE_SEQUENCE: tuple[str, ...] = (
    "brief", "generate", "provision", "business_info", "content",
    "media", "seo", "qa", "publish", "deliver", "settle",
)
# Stages that take an irreversible, outward action — gated additionally by
# ``website_live_publish_enabled`` so the build runs end-to-end (drafting the
# site, populating content on an unpublished site) without ever going live or
# contacting the customer until the operator deliberately allows it.
OUTWARD_STAGES: frozenset[str] = frozenset({"publish", "deliver"})
DONE = "done"

_STATE_DIR = "website"
_STATE_SUBDIR = "state"


class WebsiteBuildState(BaseModel):
    """Durable state of one WebsiteOrder moving through the build walk."""

    model_config = ConfigDict(extra="ignore")

    order_id: str
    customer_name: str = ""
    # The order is persisted alongside its state so a resumed/redelivered walk
    # has the brief without a second lookup.
    order: WebsiteOrder | None = None

    # The stage the orchestrator will run next.
    stage: str = "brief"
    # running           — mid-walk, ready for the next approved stage
    # awaiting_approval — supervised: the next stage needs operator sign-off
    # parked            — a stage needs an unseeded credential / unchosen input
    # escalated         — a Codex gate blocked a handoff; operator must resolve
    # done              — settled; the build is complete
    status: str = "running"
    completed_stages: list[str] = Field(default_factory=list)
    # Operator sign-offs (the supervised gate). Autonomous mode fills this
    # implicitly via the orchestrator; it is never written from a request body.
    approved_stages: list[str] = Field(default_factory=list)

    # Per-stage outputs.
    site_id: str = ""            # the provisioned Wix metaSiteId
    site_url: str = ""           # the live URL (set at publish/deliver)
    content_item_ids: list[str] = Field(default_factory=list)
    preview_checklist: dict[str, Any] | None = None
    settlement_ref: str = ""     # liabilities repayment marker / invoice id
    # Taste-governed generation report (set at the generate stage): the resolved
    # TasteProfile, the fail-closed copy audit, and which path produced it.
    generation_report: dict[str, Any] | None = None
    # SEO package (JSON-LD + per-page meta) built at the seo stage, and the
    # live SEO+security audit result captured at the deliver gate.
    seo_package: dict[str, Any] | None = None
    live_audit: dict[str, Any] | None = None
    # AI media generation report (set at the media stage): planned/generated
    # assets + Wix Media upload refs. No raw bytes — refs + metadata only.
    media_report: dict[str, Any] | None = None

    # Set when status == "escalated": {stage, violated_rule_id, reason, ...}.
    escalation: dict[str, Any] | None = None
    # Set when status == "parked": {stage, reason}.
    park: dict[str, Any] | None = None

    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def log(self, event: str, **detail: Any) -> None:
        """Append a timestamped event to the order's history."""
        self.history.append({"ts": iso_now(), "event": event, **detail})


def _state_file(order_id: str) -> Path:
    # order_id is a server-minted wb_... id, but be defensive against traversal
    # in a persisted key all the same.
    safe = order_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return state_path(_STATE_DIR, _STATE_SUBDIR, f"{safe}.json")


def load_state(order_id: str) -> WebsiteBuildState | None:
    """Load the persisted state for an order, or None if absent."""
    path = _state_file(order_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WebsiteBuildState.model_validate(data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _LOG.error("website state load failed (%s): %s", path, exc)
        return None


def save_state(state: WebsiteBuildState) -> bool:
    """Persist state atomically-ish (write temp then replace). Best-effort."""
    if not state.created_at:
        state.created_at = iso_now()
    state.updated_at = iso_now()
    path = _state_file(state.order_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        _LOG.error("website state save failed (%s): %s", path, exc)
        return False


__all__ = [
    "STAGE_SEQUENCE",
    "OUTWARD_STAGES",
    "DONE",
    "WebsiteBuildState",
    "load_state",
    "save_state",
]
