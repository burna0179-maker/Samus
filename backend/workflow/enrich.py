"""Optional, budget-gated LLM enrichment of n8n node parameters.

Deterministic compilation gives every node a working shape with empty
placeholders (channel ids, subjects, message bodies). This pass asks the LLM to
fill the *content* placeholders (a Slack message text, an email subject/body, a
follow-up wait amount) from the customer's intake text — never the structural or
credential fields. It mirrors :mod:`backend.social.repurpose`: one budget-gated
call, tolerant JSON parse, and **fail-soft** (any error leaves the deterministic
defaults untouched). Never raises.
"""

from __future__ import annotations

import logging

from backend.social.repurpose import _tolerant_json
from backend.workflow.models import N8nWorkflow

_LOG = logging.getLogger("samus.workflow.enrich")

_WORKCELL = "outreach"  # reuse the proven per-workcell budget config
_MAX_TOKENS = 900

# Only these content fields may be overwritten by the LLM — structural fields
# (type, url, credentials, channelId, documentId) are never touched.
_CONTENT_FIELDS = {"text", "message", "subject", "content"}


def enrich_workflow(wf: N8nWorkflow, plan, intake_text: str, *, settings=None) -> bool:
    """Fill content placeholders from ``intake_text``. Returns True if anything
    was enriched. Never raises."""
    if settings is not None and not bool(getattr(settings, "workflow_n8n_llm_enrich", False)):
        return False
    if not (intake_text or "").strip():
        return False

    try:
        from backend.common.llm_client import anthropic_messages

        names = ", ".join(
            n.name for n in wf.nodes if any(f in n.parameters for f in _CONTENT_FIELDS)
        )
        if not names:
            return False
        prompt = (
            "You configure the message CONTENT of nodes in an automation workflow. "
            "Return ONLY a JSON object mapping node name -> an object of field=value "
            "for these fields when relevant: text, message, subject, content. Keep "
            "values short, professional, and specific to the business need below. Do "
            "NOT invent channel ids, urls, emails, or credentials.\n\n"
            f"NODES: {names}\n\n"
            f"BUSINESS NEED:\n{intake_text.strip()[:1200]}\n"
        )
        text, _usage = anthropic_messages(
            workcell=_WORKCELL, api_key="unused", prompt=prompt, max_tokens=_MAX_TOKENS
        )
        parsed = _tolerant_json(text)
        if not isinstance(parsed, dict):
            return False

        by_name = {n.name: n for n in wf.nodes}
        changed = False
        for node_name, fields in parsed.items():
            node = by_name.get(node_name)
            if node is None or not isinstance(fields, dict):
                continue
            for field, value in fields.items():
                if (
                    field in _CONTENT_FIELDS
                    and field in node.parameters
                    and isinstance(value, str)
                    and value.strip()
                ):
                    node.parameters[field] = value.strip()
                    changed = True
        return changed
    except Exception as exc:  # noqa: BLE001 — fail-soft to deterministic defaults
        _LOG.info("workflow enrich unavailable (%s)", type(exc).__name__)
        return False


__all__ = ["enrich_workflow"]
