"""MFH (Meaning Failure Handler) — the meaning-anchor sibling of EFH.

Where :mod:`backend.governance.efh_evaluator` is the fail-CLOSED ethical gate
(inviolable axioms; a breach BLOCKS), MFH is the ADVISORY meaning gate. The
meaning anchors (``axioms/meaning_anchors.yaml``) are softer, genuinely
contested axes — ``veto_class: quorum``, not inviolable — so a heuristic
mismatch FLAGS an action for attention rather than vetoing it. The flags ride
on the ``decision.made`` event the caller already emits; nothing is blocked.

Scope for Samus: the commercial workcell's characteristic meaning failure is
the subscription/retainer TREADMILL (recurring bill that maintains a fixed
outcome instead of compounding the client's capacity) and LOCK-IN (value that
evaporates the moment the engagement ends). The heuristic targets exactly
those, mapped to the five signed anchors:

  * capability_over_dependency   — dependency / treadmill language
  * persistence_beyond_relationship — value bounded by the engagement
  * externality_reality          — value that only exists because we measure it
  * substance_over_signal        — signal optimisation with no underlying change
  * reversibility_for_recipient  — switching-cost / sunk-cost lock-in

Design mirrors EFH's layer-1 heuristic deliberately (same ``_flatten_text``
walk, same ``re.search`` pattern map) so the two evaluators read as siblings.
There is intentionally NO semantic layer here yet — meaning classification is
quorum-routed in the wider ecosystem (Major's MFH owns per-axiom weighting),
and Samus only needs the advisory heuristic surfaced on his own review path.

``evaluate`` NEVER raises and NEVER blocks — it returns a small assessment
dict (``{flagged: bool, anchors: [...], notes: str}``) the caller attaches to
its decision record. A malformed action or a missing anchors file degrades to
``flagged=False`` (advisory: silence, not a false alarm).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

_LOG = logging.getLogger("samus.governance.mfh")

_AXIOMS_DIR = Path(__file__).resolve().parents[2] / "axioms"

# Heuristic keyword patterns mapped to meaning-anchor ids. Advisory only: a
# match RAISES a flag for operator/quorum attention, it does not veto. Phrases
# chosen to catch the treadmill / lock-in / vanity-metric failure modes the
# anchors were declared to name (see meaning_anchors.yaml notes).
_ANCHOR_PATTERNS: dict[str, list[str]] = {
    "axiom.meaning.capability_over_dependency": [
        r"\bdependenc", r"\block\s*them\s*in", r"\bkeep\s+them\s+(?:on|paying)",
        r"\btreadmill", r"\bmaintain(?:s|ing)?\s+the\s+status\s+quo",
        r"\brecurring\s+(?:fee|charge|bill)\s+to\s+keep",
    ],
    "axiom.meaning.persistence_beyond_relationship": [
        r"\bonly\s+works\s+while\s+we", r"\bevaporat", r"\bstops?\s+the\s+moment\s+we",
        r"\bceases?\s+when\s+(?:we|the\s+engagement)",
    ],
    "axiom.meaning.externality_reality": [
        r"\bvanity\s+metric", r"\blooks?\s+good\s+on\s+paper",
        r"\bgame\s+the\s+(?:metric|number|kpi)", r"\binflate\s+the\s+(?:metric|number)",
    ],
    "axiom.meaning.substance_over_signal": [
        r"\bappear(?:s|ance)\s+of\s+progress", r"\bmove\s+the\s+number\s+without",
        r"\boptimis[ez]e?\s+for\s+the\s+(?:signal|metric)\s+not",
    ],
    "axiom.meaning.reversibility_for_recipient": [
        r"\bswitching\s+cost", r"\bsunk[\s-]?cost", r"\bhard\s+to\s+(?:leave|cancel|switch)",
        r"\bcancellation\s+(?:penalt|friction)", r"\bcontractual\s+lock",
    ],
}


class MeaningFailureHandler:
    """Advisory meaning-anchor evaluator. Returns an assessment, never a veto."""

    def __init__(self, axioms_dir: Path | None = None) -> None:
        d = Path(axioms_dir) if axioms_dir else _AXIOMS_DIR
        self._anchor_ids: set[str] = set()
        try:
            with (d / "meaning_anchors.yaml").open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            for a in doc.get("meaning_anchors", []):
                aid = a.get("id")
                if aid:
                    self._anchor_ids.add(str(aid))
        except Exception as exc:  # noqa: BLE001 — advisory: absence != alarm
            _LOG.warning("mfh: meaning_anchors.yaml unavailable (%s)", exc)

    def evaluate(self, proposed_action: dict) -> dict:
        """Return an advisory meaning assessment for ``proposed_action``.

        Shape::

            {"flagged": bool, "anchors": [anchor_id, ...], "notes": str}

        ``flagged`` is True when at least one anchor's heuristic matched AND
        that anchor is in the signed registry (so a stale pattern can't flag a
        retired anchor). Never raises; a malformed action -> not flagged.
        """
        try:
            body_text = self._flatten_text(proposed_action)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("mfh flatten failed (advisory pass): %s", exc)
            return {"flagged": False, "anchors": [], "notes": "mfh_flatten_error"}

        flagged_anchors: list[str] = []
        for anchor_id, patterns in _ANCHOR_PATTERNS.items():
            if self._anchor_ids and anchor_id not in self._anchor_ids:
                continue
            if any(re.search(p, body_text, re.IGNORECASE) for p in patterns):
                flagged_anchors.append(anchor_id)

        if not flagged_anchors:
            return {"flagged": False, "anchors": [], "notes": "no meaning-anchor concern"}

        notes = (
            "advisory meaning-anchor concern (not a veto): "
            + ", ".join(a.rsplit(".", 1)[-1] for a in flagged_anchors)
        )
        return {"flagged": True, "anchors": flagged_anchors, "notes": notes}

    @staticmethod
    def _flatten_text(action: dict) -> str:
        parts: list[str] = []

        def _walk(v: Any) -> None:
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, dict):
                for x in v.values():
                    _walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    _walk(x)

        _walk(action)
        return " ".join(parts)


# Module-level singleton — cheap to build, mirrors how callers reach EFH.
_HANDLER: MeaningFailureHandler | None = None


def evaluate_meaning(proposed_action: dict) -> dict:
    """Convenience entry: advisory meaning assessment via the shared handler."""
    global _HANDLER
    if _HANDLER is None:
        _HANDLER = MeaningFailureHandler()
    return _HANDLER.evaluate(proposed_action)


__all__ = ["MeaningFailureHandler", "evaluate_meaning"]
