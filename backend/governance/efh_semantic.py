"""EFH semantic classifier — LLM layer for the Ethical Failure Handler.

This is layer 2 of :class:`backend.governance.efh_evaluator.EthicalFailureHandler`.
It reads a proposed action's flattened text against the inviolable-axiom
catalogue and returns the axiom ids it judges to be breached. It is **additive
only**: the EFH treats the result as breaches to ADD on top of the deterministic
keyword heuristic, never as a clearance — so this layer can only ever make the
gate stricter.

Design constraints (doctrine):
  * Routes through the single Anthropic entrypoint
    (:func:`backend.common.llm_client.anthropic_messages`) — no second LLM path,
    so the per-workcell token budget and global $-cap both get a vote.
  * Cheap model (Haiku floor) — a binary axiom classifier does not need a
    frontier model, and keeping it on the floor keeps the global cap healthy.
  * Fail-closed for the GATE, fail-open for THIS CALL: any error (missing key,
    budget deny, network, parse) returns ``[]`` so the heuristic floor stands.
    The caller (EFH) is what's load-bearing; this is a best-effort enricher.
  * Structured JSON output, defensively parsed — the model is instructed to
    return a strict shape, but a malformed reply degrades to ``[]`` rather than
    raising into the gate.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

_LOG = logging.getLogger("samus.governance.efh_semantic")

_WORKCELL = "efh_semantic"
# Bound the classifier output — it only ever returns a short JSON list of ids.
_MAX_TOKENS = 512
# Cap how much action text we feed the classifier (defence against a huge body
# blowing the budget; the heuristic already scanned the full text).
_MAX_BODY_CHARS = 8000


def _build_system(axiom_index: dict[str, dict[str, Any]]) -> str:
    """Render the axiom catalogue into a system prompt for the classifier."""
    lines = [
        "You are an ethics classifier for an autonomous commercial agent.",
        "You are given the text of a PROPOSED ACTION and a catalogue of "
        "INVIOLABLE AXIOMS. Decide which axioms, if any, the proposed action "
        "would BREACH if carried out.",
        "",
        "Rules:",
        "- Only flag an axiom when the action would genuinely breach it. When "
        "in doubt, do NOT flag — a separate deterministic check already runs.",
        "- Judge intent and effect, not just keywords (e.g. paraphrased "
        "manipulation, indirect data extraction, disguised concealment).",
        "- Return STRICT JSON only, no prose: "
        '{\"breached\": [\"<axiom_id>\", ...]}. Use the exact axiom ids below. '
        "An empty list means no breach.",
        "",
        "INVIOLABLE AXIOMS:",
    ]
    for axiom_id, axiom in axiom_index.items():
        desc = ""
        if isinstance(axiom, dict):
            desc = str(
                axiom.get("description")
                or axiom.get("statement")
                or axiom.get("summary")
                or ""
            ).strip()
        lines.append(f"- {axiom_id}: {desc}" if desc else f"- {axiom_id}")
    return "\n".join(lines)


def _parse_breached(text: str, valid_ids: set[str]) -> list[str]:
    """Defensively extract the ``breached`` id list from the model reply."""
    if not text:
        return []
    # Prefer a clean JSON object; fall back to the first {...} span.
    candidate = text.strip()
    if not candidate.startswith("{"):
        m = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not m:
            return []
        candidate = m.group(0)
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("breached")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        sid = str(item).strip()
        if sid in valid_ids and sid not in out:
            out.append(sid)
    return out


def classify_breaches(
    body_text: str,
    *,
    axiom_index: dict[str, dict[str, Any]],
    api_key: str | None = None,
) -> list[str]:
    """Return the axiom ids the LLM classifier flags as breached.

    Always returns a list. Returns ``[]`` on any failure (no key, budget deny,
    network, parse) so the EFH's deterministic heuristic remains the gate.
    """
    if not body_text or not body_text.strip():
        return []
    if not axiom_index:
        return []

    valid_ids = set(axiom_index)
    system = _build_system(axiom_index)
    snippet = body_text[:_MAX_BODY_CHARS]
    prompt = (
        "PROPOSED ACTION (verbatim text to evaluate):\n"
        "<<<\n" + snippet + "\n>>>\n\n"
        "Return strict JSON: {\"breached\": [...]}."
    )

    try:
        from backend.common.llm_client import anthropic_messages

        text, _usage = anthropic_messages(
            workcell=_WORKCELL,
            api_key="unused",
            prompt=prompt,
            system=system,
            max_tokens=_MAX_TOKENS,
            cache_system=True,  # axiom catalogue is stable -> cache the prefix
            security_label="governance",
        )
    except Exception as exc:  # noqa: BLE001 — fail-open; the heuristic is the gate
        _LOG.warning("efh_semantic classifier call failed: %s", exc)
        return []

    return _parse_breached(text, valid_ids)


__all__ = ["classify_breaches"]
