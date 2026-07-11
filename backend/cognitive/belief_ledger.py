"""Belief ledger — track what Samus believes, how sure it is, and why.

Findings from the reasoning legs (EOD triangulation, audits, LLM syntheses) are
ephemeral: they inform one decision and vanish. The system therefore cannot say
*why* it changed its mind, cannot detect that today's conclusion contradicts
last week's, and cannot age out a stale assumption before it costs money. This
module is the accountability layer — a durable ledger of beliefs, each carrying:

    { claim, confidence, supporting_evidence, counter_evidence,
      last_verified, economic_impact, tier, status }

Confidence is recomputed from the evidence via a Laplace-smoothed support ratio
(prior 0.5, so a claim with no evidence is maximally uncertain). A belief whose
counter-evidence outweighs its support flips to ``contradicted`` — the
"changed its mind" signal, ranked by ``economic_impact`` so the costly
contradictions surface first. :func:`stale_beliefs` flags beliefs whose
``last_verified`` is older than a TTL for re-verification.

Standalone + fail-soft: current state is a JSON document (atomic replace) under
the state root; every mutation also appends to a history ledger. Nothing here
calls an LLM. :func:`record_from_triangulation` is an optional adapter that maps
the triangulation output into beliefs WITHOUT the triangulation module needing to
know this ledger exists.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.common.dates import iso_now
from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.cognitive.belief_ledger")

_BELIEFS_JSON = ("cognitive", "beliefs.json")
_BELIEFS_HISTORY = ("cognitive", "beliefs_history.jsonl")

# A belief with confidence below this, once it has ANY counter-evidence, is
# treated as contradicted (the counter-evidence has overtaken the support).
CONTRADICTION_CONFIDENCE = 0.5
# Default staleness horizon (14 days) — mirrors the karma half-life cadence.
DEFAULT_STALE_SECONDS = 14 * 24 * 3600

STATUS_ACTIVE = "active"
STATUS_CONTRADICTED = "contradicted"

# Precedent-retrieval scoring (Concept 1 — institutional memory). Half-life
# mirrors the codex registry so belief precedent and decision precedent decay
# on the same curve.
_PRECEDENT_RECENCY_HALF_LIFE_DAYS = 180.0
_PRECEDENT_MIN_TOKEN_LEN = 3
_PRECEDENT_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "not",
        "but",
        "are",
        "was",
        "has",
        "had",
        "have",
        "from",
        "into",
        "onto",
        "off",
        "out",
        "all",
        "any",
        "can",
        "may",
        "will",
        "shall",
        "should",
        "would",
        "could",
        "does",
        "did",
        "been",
        "being",
        "its",
        "our",
        "your",
        "their",
        "his",
        "her",
        "him",
        "she",
        "they",
        "them",
        "who",
        "whom",
        "what",
        "which",
        "when",
        "where",
        "why",
        "how",
        "then",
        "than",
        "too",
        "also",
        "such",
        "some",
        "each",
        "one",
        "two",
        "new",
        "old",
        "yes",
        "you",
    }
)
_PRECEDENT_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

__all__ = [
    "Belief",
    "PrecedentMatch",
    "record_belief",
    "add_evidence",
    "verify",
    "get_belief",
    "list_beliefs",
    "contradictions",
    "stale_beliefs",
    "record_from_triangulation",
    "belief_id_for",
    "query_precedent",
    "situation_key_for",
    "link_decision",
    "dependent_decisions",
    "STATUS_ACTIVE",
    "STATUS_CONTRADICTED",
    "CONTRADICTION_CONFIDENCE",
    "DEFAULT_STALE_SECONDS",
    "CONTRADICTION_EMERGENCY_IMPACT_USD",
]

# ADR-0019 severity threshold — a contradicted belief with economic_impact
# at or above this triggers emergency-severity HOTL approval (short TTL,
# per-item, no batch). Below the threshold it enqueues at routine severity.
CONTRADICTION_EMERGENCY_IMPACT_USD = 100.0


@dataclass
class Belief:
    belief_id: str
    claim: str
    confidence: float = 0.5
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)
    counter_evidence: list[dict[str, Any]] = field(default_factory=list)
    last_verified: str = ""
    economic_impact: float = 0.0
    tier: int = 2  # 1 critical .. 3 minor
    status: str = STATUS_ACTIVE
    created_at: str = ""
    updated_at: str = ""
    # Optional situation tag used by :func:`query_precedent` — a stable slug
    # naming the recurring situation this belief speaks to (e.g. "cold-outreach-
    # first-touch", "gmail-oauth-outage"). Beliefs left tagless still match on
    # claim keywords; the tag is an exact-match boost, not a requirement.
    situation_key: str = ""
    # Decision ids (from backend.common.decision_record) that used this belief
    # as evidence. Populated via :func:`link_decision`. Stored belief-side only:
    # decision_record persists on the immutable business_events stream so there
    # is no back-mutation API on that side — the join is one-sided by design.
    depended_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Belief":
        return cls(
            belief_id=str(row.get("belief_id", "")),
            claim=str(row.get("claim", "")),
            confidence=float(row.get("confidence", 0.5) or 0.5),
            supporting_evidence=list(row.get("supporting_evidence") or []),
            counter_evidence=list(row.get("counter_evidence") or []),
            last_verified=str(row.get("last_verified", "")),
            economic_impact=float(row.get("economic_impact", 0.0) or 0.0),
            tier=int(row.get("tier", 2) or 2),
            status=str(row.get("status", STATUS_ACTIVE) or STATUS_ACTIVE),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
            situation_key=str(row.get("situation_key", "") or ""),
            depended_by=list(row.get("depended_by") or []),
        )


@dataclass(frozen=True)
class PrecedentMatch:
    """One scored belief surfaced by :func:`query_precedent`.

    ``matched_on`` is one of ``"situation_key"``, ``"claim"``, or
    ``"claim+situation_key"`` — telling the caller whether the belief was
    recalled by an explicit situation tag or by keyword overlap in the claim.
    Contradicted beliefs are never returned (Concept 1 short-circuit must not
    proceed on a belief that flipped).
    """

    belief_id: str
    claim: str
    confidence: float
    situation_key: str
    last_verified: str
    economic_impact: float
    score: float
    matched_on: str


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def belief_id_for(claim: str) -> str:
    """Stable slug so the same claim upserts rather than duplicating."""
    slug = re.sub(r"[^a-z0-9]+", "-", (claim or "").strip().lower()).strip("-")
    return slug[:80] or "belief"


def _weight(ev: dict[str, Any]) -> float:
    try:
        return max(0.0, float(ev.get("weight", 1.0) or 1.0))
    except (TypeError, ValueError):
        return 1.0


def _recompute_confidence(support: list[dict], counter: list[dict]) -> float:
    """Laplace-smoothed support ratio: (1+Σs)/(2+Σs+Σc). Prior 0.5."""
    s = sum(_weight(e) for e in support)
    c = sum(_weight(e) for e in counter)
    return round((1.0 + s) / (2.0 + s + c), 4)


def _evidence(source: str, detail: str = "", weight: float = 1.0) -> dict[str, Any]:
    return {"source": str(source), "detail": str(detail), "weight": float(weight), "ts": iso_now()}


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# persistence (JSON doc + history ledger)
# ---------------------------------------------------------------------------


def _doc_path() -> Path:
    return state_path(*_BELIEFS_JSON)


def _load() -> dict[str, dict[str, Any]]:
    path = _doc_path()
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        _LOG.warning("belief ledger load failed: %s", exc)
        return {}


def _save(data: dict[str, dict[str, Any]]) -> bool:
    path = _doc_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        _LOG.warning("belief ledger save failed: %s", exc)
        return False


def _append_history(event: dict[str, Any]) -> None:
    try:
        from backend.common.persistence import open_ledger

        open_ledger(
            jsonl_path=state_path(*_BELIEFS_HISTORY),
            collection="belief_history",
        ).append(event)
    except Exception as exc:  # noqa: BLE001 — history is best-effort
        _LOG.warning("belief history append failed: %s", exc)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def record_belief(
    claim: str,
    *,
    belief_id: str | None = None,
    supporting: list[dict] | None = None,
    counter: list[dict] | None = None,
    economic_impact: float = 0.0,
    tier: int = 2,
    situation_key: str = "",
) -> Belief:
    """Insert or update a belief, merging evidence + recomputing confidence.

    Existing evidence is preserved and the new evidence appended, so repeated
    corroboration strengthens (or, via counter-evidence, weakens) the belief.
    ``last_verified`` is refreshed on every call. Never raises.

    ``situation_key`` (optional) tags the belief so :func:`query_precedent`
    can recall it by explicit situation rather than by claim keywords. Left
    blank the belief is still recallable by claim-keyword overlap.
    """
    bid = belief_id or belief_id_for(claim)
    data = _load()
    now = iso_now()
    if bid in data:
        b = Belief.from_dict(data[bid])
        prev_status = b.status
        b.claim = claim or b.claim
        if economic_impact:
            b.economic_impact = float(economic_impact)
        b.tier = int(tier) if tier else b.tier
        if situation_key:
            b.situation_key = str(situation_key)
    else:
        prev_status = STATUS_ACTIVE  # new beliefs default to active pre-recompute
        b = Belief(
            belief_id=bid,
            claim=claim,
            economic_impact=float(economic_impact),
            tier=int(tier),
            created_at=now,
            situation_key=str(situation_key or ""),
        )

    b.supporting_evidence.extend(supporting or [])
    b.counter_evidence.extend(counter or [])
    b.confidence = _recompute_confidence(b.supporting_evidence, b.counter_evidence)
    b.status = (
        STATUS_CONTRADICTED
        if (b.counter_evidence and b.confidence < CONTRADICTION_CONFIDENCE)
        else STATUS_ACTIVE
    )
    b.last_verified = now
    b.updated_at = now

    data[bid] = b.to_dict()
    _save(data)
    _append_history(
        {
            "ts": now,
            "event": "record",
            "belief_id": bid,
            "confidence": b.confidence,
            "status": b.status,
        }
    )

    # Concept 5 — epistemic governance. When a belief transitions from active
    # to contradicted, emit a marker business event and (if downstream
    # decisions depended on it) auto-enqueue an HOTL recheck approval with
    # ADR-0019 severity derived from economic_impact.
    if prev_status == STATUS_ACTIVE and b.status == STATUS_CONTRADICTED:
        _on_contradiction(b)

    return b


def add_evidence(
    belief_id: str,
    *,
    support: dict | None = None,
    counter: dict | None = None,
) -> Belief | None:
    """Append one piece of evidence to an existing belief; recompute. None if unknown."""
    data = _load()
    if belief_id not in data:
        return None
    return record_belief(
        data[belief_id].get("claim", ""),
        belief_id=belief_id,
        supporting=[support] if support else None,
        counter=[counter] if counter else None,
    )


def verify(belief_id: str) -> Belief | None:
    """Refresh ``last_verified`` (the belief was re-checked and still holds)."""
    data = _load()
    if belief_id not in data:
        return None
    b = Belief.from_dict(data[belief_id])
    now = iso_now()
    b.last_verified = now
    b.updated_at = now
    data[belief_id] = b.to_dict()
    _save(data)
    _append_history({"ts": now, "event": "verify", "belief_id": belief_id})
    return b


def get_belief(belief_id: str) -> Belief | None:
    data = _load()
    return Belief.from_dict(data[belief_id]) if belief_id in data else None


def list_beliefs(*, status: str | None = None) -> list[Belief]:
    beliefs = [Belief.from_dict(r) for r in _load().values()]
    if status:
        beliefs = [b for b in beliefs if b.status == status]
    return beliefs


def contradictions() -> list[Belief]:
    """Beliefs whose counter-evidence has overtaken their support, ranked by
    economic_impact (the costliest wrong assumptions first)."""
    out = [b for b in list_beliefs() if b.status == STATUS_CONTRADICTED]
    out.sort(key=lambda b: b.economic_impact, reverse=True)
    return out


def stale_beliefs(max_age_seconds: float = DEFAULT_STALE_SECONDS) -> list[Belief]:
    """Active beliefs whose last_verified is older than the horizon — candidates
    for re-verification. Beliefs without a parseable timestamp count as stale."""
    now = datetime.now(timezone.utc)
    out: list[Belief] = []
    for b in list_beliefs(status=STATUS_ACTIVE):
        dt = _parse_iso(b.last_verified)
        if dt is None or (now - dt).total_seconds() > max_age_seconds:
            out.append(b)
    out.sort(key=lambda b: b.economic_impact, reverse=True)
    return out


def record_from_triangulation(
    result: dict[str, Any],
    *,
    economic_impact: float = 0.0,
) -> list[Belief]:
    """Adapter: fold a triangulation output into beliefs (no triangulation edit).

    Corroborated findings become supported beliefs (one support entry per
    agreeing source, or a single ``triangulation`` source); divergent findings
    become beliefs carrying a counter-evidence marker, so a claim asserted by
    only one leg starts low-confidence rather than trusted.
    """
    out: list[Belief] = []
    for c in result.get("corroborated") or []:
        claim = str(c.get("finding", "")).strip()
        if not claim:
            continue
        sources = c.get("sources") or ["triangulation"]
        support = [_evidence(str(s), detail=claim) for s in sources]
        out.append(
            record_belief(
                claim,
                supporting=support,
                economic_impact=economic_impact,
                tier=int(c.get("tier", 2) or 2),
            )
        )
    for dvg in result.get("divergent") or []:
        claim = str(dvg.get("finding", "")).strip()
        if not claim:
            continue
        out.append(
            record_belief(
                claim,
                supporting=[_evidence(str(dvg.get("source", "unknown")), detail=claim)],
                counter=[_evidence("divergence", detail="only one leg asserted this")],
                economic_impact=economic_impact,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Concept 5 — Epistemic governance (belief-dependency graph)
# ---------------------------------------------------------------------------


def link_decision(belief_id: str, decision_id: str) -> Belief | None:
    """Record that ``decision_id`` used ``belief_id`` as evidence.

    Idempotent: linking the same decision twice is a no-op. Fail-soft: if
    ``belief_id`` is unknown returns ``None`` (matches :func:`add_evidence`
    behaviour) — a missing belief must never break the calling decision path.

    One-sided by design: :mod:`backend.common.decision_record` persists
    decisions as immutable ``decision.made`` business events with no mutation
    API, so the join lives on the belief side only. Callers that need
    "decisions that touched belief X" use :func:`dependent_decisions`; callers
    that need "beliefs cited by decision Y" walk the decision's own
    ``data_used`` list.
    """
    if not belief_id or not decision_id:
        return None
    data = _load()
    if belief_id not in data:
        return None
    b = Belief.from_dict(data[belief_id])
    if decision_id in b.depended_by:
        return b  # idempotent no-op — nothing to persist
    b.depended_by.append(decision_id)
    now = iso_now()
    b.updated_at = now
    data[belief_id] = b.to_dict()
    _save(data)
    _append_history(
        {"ts": now, "event": "link_decision", "belief_id": belief_id, "decision_id": decision_id}
    )
    return b


def dependent_decisions(belief_id: str) -> list[str]:
    """Return the decision ids that cited ``belief_id`` via :func:`link_decision`.

    Read-only accessor over the JSON document. Unknown belief -> empty list
    (never raises). Ordering matches insertion order — the depended_by list is
    append-only under normal flow.
    """
    if not belief_id:
        return []
    data = _load()
    row = data.get(belief_id)
    if not row:
        return []
    return list(row.get("depended_by") or [])


def _on_contradiction(belief: Belief) -> None:
    """Emit contradiction signal + auto-enqueue HOTL recheck when appropriate.

    Called from :func:`record_belief` when a belief transitions from active
    to contradicted. Two effects:

    1. Emits a ``decision.made`` unified business event tagged with
       ``decision_kind: belief.contradicted`` metadata. ``decision.made`` is
       reused because :mod:`backend.common.business_events` validates event
       types against a frozen taxonomy — a new ``belief.contradicted`` event
       type would be rejected. :mod:`backend.common.approvals` uses the same
       pattern for its own approval-lifecycle events.

    2. When ``belief.depended_by`` is non-empty, opens an HOTL approval of
       kind ``recheck_decisions`` via :func:`backend.common.approvals.create_approval`
       with ADR-0019 severity derived from ``economic_impact``: at/above
       :data:`CONTRADICTION_EMERGENCY_IMPACT_USD` -> emergency (short TTL,
       per-item, no batch approval); below -> routine.

    Fail-soft throughout — a broken telemetry sink must never mask the
    contradiction itself (the belief flip is already persisted upstream).
    """
    try:
        from backend.common.business_events import (
            DECISION_MADE,
            emit_business_event,
        )

        emit_business_event(
            DECISION_MADE,
            workcell="cognitive",
            metadata={
                "decision_kind": "belief.contradicted",
                "belief_id": belief.belief_id,
                "claim": belief.claim,
                "confidence": belief.confidence,
                "economic_impact": belief.economic_impact,
                "depended_by": list(belief.depended_by),
            },
        )
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks callers
        _LOG.warning("belief.contradicted event emit failed: %s", exc)

    if not belief.depended_by:
        return

    try:
        from backend.common.approvals import create_approval

        risk_level = (
            "high" if belief.economic_impact >= CONTRADICTION_EMERGENCY_IMPACT_USD else "normal"
        )
        create_approval(
            "recheck_decisions",
            {
                "belief_id": belief.belief_id,
                "claim": belief.claim,
                "confidence": belief.confidence,
                "economic_impact": belief.economic_impact,
                "decisions": list(belief.depended_by),
            },
            risk_level=risk_level,
            ev_usd=belief.economic_impact,
        )
    except Exception as exc:  # noqa: BLE001 — never break the cognitive loop
        _LOG.warning("recheck_decisions approval enqueue failed: %s", exc)


# ---------------------------------------------------------------------------
# Precedent retrieval (Concept 1 — institutional memory)
# ---------------------------------------------------------------------------


def situation_key_for(context: str) -> str:
    """Stable slug for the current situation, mirroring :func:`belief_id_for`.

    Callers use this to attach a `situation_key` to a belief so future
    :func:`query_precedent` calls with the same context surface it via the
    exact-match boost rather than by keyword overlap alone.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (context or "").strip().lower()).strip("-")
    return slug[:80] or "situation"


def _precedent_tokens(text: str) -> list[str]:
    if not text:
        return []
    return [
        m.group(0)
        for m in _PRECEDENT_TOKEN_RE.finditer(text.lower())
        if len(m.group(0)) >= _PRECEDENT_MIN_TOKEN_LEN and m.group(0) not in _PRECEDENT_STOPWORDS
    ]


def _precedent_recency(iso_ts: str, *, now: datetime | None = None) -> float:
    if not iso_ts:
        return 0.5
    dt = _parse_iso(iso_ts)
    if dt is None:
        return 0.5
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    import math

    return math.exp(-age_days / _PRECEDENT_RECENCY_HALF_LIFE_DAYS)


def query_precedent(
    context: str,
    k: int = 5,
    *,
    min_score: float = 0.5,
    _now: datetime | None = None,
) -> list[PrecedentMatch]:
    """Return active beliefs most relevant to the current ``context``.

    Answers "has this happened before, and what did we conclude?" — the
    Concept 1 precedent seam. Scoring is deliberately local-first:

      * exact ``situation_key`` match: +8.0 (dominates keyword scoring).
      * claim-keyword overlap: +2.0 per matched token, log-dampened by claim
        length so short high-precision claims aren't buried under long ones.
      * recency multiplier: exp(-age_days/180) on ``last_verified``.
      * economic-impact tiebreak: costlier beliefs surface first at equal score.

    Contradicted beliefs are excluded — a flipped belief is not a precedent
    the reasoner should short-circuit on. Fail-soft: any error returns [].

    Args:
        context: free-text description of the current situation. May be the
            output of :func:`situation_key_for` or raw natural-language.
        k: top-k cap on returned matches.
        min_score: floor for what counts as a precedent hit. Default 0.5
            filters out spurious single-stopword matches.
    """
    if not context or not context.strip():
        return []
    try:
        query_key = situation_key_for(context)
        query_tokens = _precedent_tokens(context)
        if not query_tokens and not query_key:
            return []
        query_token_set = set(query_tokens)

        matches: list[PrecedentMatch] = []
        for b in list_beliefs(status=STATUS_ACTIVE):
            score = 0.0
            matched_on = ""
            if b.situation_key and b.situation_key == query_key:
                score += 8.0
                matched_on = "situation_key"

            claim_tokens = _precedent_tokens(b.claim)
            if claim_tokens and query_token_set:
                overlap = query_token_set & set(claim_tokens)
                if overlap:
                    import math

                    # Length dampening: divide by log of claim size so a
                    # 30-token essay-claim can't dominate a 4-token slogan-
                    # claim on raw hit count.
                    dampener = 1.0 + math.log1p(len(claim_tokens))
                    score += 2.0 * len(overlap) / dampener
                    matched_on = "claim+situation_key" if matched_on else "claim"

            if score <= 0.0:
                continue
            score *= _precedent_recency(b.last_verified, now=_now)
            # Confidence factor — a low-confidence active belief is still a
            # precedent hit but should not dominate a high-confidence one at
            # the same keyword overlap.
            score *= max(0.25, b.confidence)
            if score < min_score:
                continue
            matches.append(
                PrecedentMatch(
                    belief_id=b.belief_id,
                    claim=b.claim,
                    confidence=b.confidence,
                    situation_key=b.situation_key,
                    last_verified=b.last_verified,
                    economic_impact=b.economic_impact,
                    score=round(score, 4),
                    matched_on=matched_on,
                )
            )

        # Higher score first; tiebreak on economic_impact then belief_id.
        matches.sort(key=lambda m: (-m.score, -m.economic_impact, m.belief_id))
        return matches[: max(0, int(k))]
    except Exception as exc:  # noqa: BLE001 — never break the cognitive loop
        _LOG.warning("query_precedent failed: %s", exc)
        return []
