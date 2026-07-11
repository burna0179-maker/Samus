"""Guidance ingestion + effectiveness pipeline.

Implements the Guidance Ingestion Process and Guidance Effectiveness Tracking
sections of the operator's Strategic Daily Intelligence and Guidance Cycle
directive. This is the consumer that was MISSING from the strategic loop: the
day-start briefing sends Samus's intelligence package to OpenAI, OpenAI returns
strategic guidance, and THIS module turns that guidance into durable, trackable,
score-able recommendations instead of letting it print to stdout and vanish.

Flow::

    OpenAI guidance payload
            │
            ▼
    ingest_guidance()  ── parse → classify → assess → tier → owner → action plan
            │              (zero extra LLM calls; pure rule-based bookkeeping)
            ▼
    GuidanceLedger  ── append-only, latest-wins over recommendation_id
            │              (jsonl on host, Firestore on Cloud Run via open_ledger)
            ▼
    accept() / reject() / start() / complete() / abandon()   ── status lifecycle
            │
            ▼
    record_outcome()  ── effectiveness: outcome, impact, success_score, lessons
            │
            ▼
    effectiveness_summary()  ── aggregate for the End-of-Day Operational Review

Design properties, mirrored from the proposal-promoter ledger conventions:

  * **Append-only, latest-wins.** A status change appends a NEW row carrying the
    same ``recommendation_id`` with a fresh ``updated_ts``; the historical row is
    never rewritten. ``all_latest()`` reduces history to current state.
  * **Backend-portable.** Persistence goes through ``open_ledger`` so the same
    call site is a JSONL file on the host/tests and a Firestore collection on
    Cloud Run (where the container FS is ephemeral).
  * **Fail-safe.** Parsing tolerates structured JSON, a ``{recommendations:[…]}``
    envelope, a JSON string (optionally fenced in markdown), or free-form prose.
    A malformed item degrades to a best-effort record rather than raising.
  * **Wire-not-arm.** Ingestion CLASSIFIES and ASSESSES and writes an
    ``acceptance_hint`` — but it does NOT auto-accept or execute anything.
    Acceptance and execution are deliberate downstream transitions. This module
    touches no effector, no send path, no cash engine.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any, Iterable, List, Optional

from backend.common.dates import iso_now
from backend.common.persistence import open_ledger
from backend.common.state_paths import state_path

from .guidance_models import (
    DEFAULT_OWNER_BY_CATEGORY,
    GuidanceCategory,
    GuidanceRecord,
    GuidanceStatus,
    GuidanceTier,
    KIND,
    coerce_level,
)

log = logging.getLogger(__name__)

# Where the durable guidance ledger lives (jsonl backend) + its Firestore
# collection name (firestore backend). Mirrors the proposals-ledger convention.
_LEDGER_JSONL = ("cognition", "guidance_ledger.jsonl")
_LEDGER_COLLECTION = "cognition_guidance"

# Sentinel distinguishing "caller did not supply this field" from "caller
# explicitly passed None" in _transition(). Without it, an unset optional and a
# deliberate null are indistinguishable and a field can never be cleared.
_UNSET = object()

# Dedup-on-ingest kill-switch. The day-start briefing, the EOD review, the CODB
# reasoner, and the gameplan-corroboration path all re-emit the SAME
# recommendation every cycle (e.g. the "Scale CODB: SendGrid $20/mo" scaler, or
# a corroborated "runway critically low" finding). Because ingest_guidance mints
# a fresh uuid4 per item per run, every re-emission became a NEW `proposed`
# record that never merged — clogging the ledger with hundreds of textual
# duplicates that compact() (which dedups by recommendation_id, not text) can
# never drain. When enabled (default), an item whose normalized text already
# matches an OPEN (non-terminal) record is skipped rather than re-proposed; the
# existing open record already tracks it. Terminal (rejected/abandoned/
# completed) records do NOT suppress a re-proposal — a closed item may legitimately
# resurface. Set SAMUS_GUIDANCE_DEDUP_ON_INGEST=0 to restore the old behaviour.
_ENV_DEDUP = "SAMUS_GUIDANCE_DEDUP_ON_INGEST"


def _dedup_enabled() -> bool:
    return os.getenv(_ENV_DEDUP, "1").strip().lower() not in ("0", "false", "no", "off")


def _norm_rec_text(text: str) -> str:
    """Normalize a recommendation for duplicate detection.

    Case-fold, collapse internal whitespace, and strip surrounding whitespace +
    trailing sentence punctuation so trivially-reworded re-emissions
    ("Runway 0.0 days." vs "runway 0.0 days") collapse to one key. Deliberately
    conservative: it does NOT stem or fuzzy-match, so distinct recommendations
    are never merged.
    """
    return re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .;:!")


# ---------------------------------------------------------------------------
# Payload parsing — tolerant of structured JSON AND free-form prose.
# ---------------------------------------------------------------------------
def _strip_md_fence(text: str) -> str:
    """Strip a leading ```json / ``` fence and trailing ``` if present.

    Tolerates language tags with hyphens/word chars (```json-5), leading
    whitespace before the opening fence, and a closing fence with trailing
    whitespace.
    """
    t = text.strip()
    if t.startswith("```"):
        # drop the opening fence + optional language tag (word chars or hyphen)
        t = re.sub(r"^```[\w-]*[ \t]*\r?\n?", "", t)
        # drop a closing fence anchored to end-of-string (allow trailing ws)
        t = re.sub(r"\r?\n?```\s*$", "", t)
    return t.strip()


def _coerce_items(payload: Any) -> List[dict]:
    """Normalize an OpenAI guidance payload into a list of item dicts.

    Accepts, in order of preference:
      * a list of dicts (already structured)
      * a dict with a ``recommendations`` / ``items`` / ``guidance`` list
      * a JSON string (optionally markdown-fenced) of either of the above
      * free-form prose -> heuristic split into numbered items

    Never raises; the prose fallback always yields at least one item when the
    text is non-empty.
    """
    if payload is None:
        return []

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)] or _items_from_prose_list(payload)

    if isinstance(payload, dict):
        for key in ("recommendations", "items", "guidance"):
            if key in payload:
                val = payload.get(key)
                if isinstance(val, list):
                    # tolerate a mix of dicts and plain strings (don't drop strings)
                    return _items_from_prose_list(val)
                # envelope key present but not a list — recurse so a nested dict
                # or JSON string still yields items instead of total silent loss
                return _coerce_items(val)
        # a single recommendation dict
        if any(k in payload for k in ("recommendation", "text", "action", "title")):
            return [payload]
        return []

    if isinstance(payload, str):
        text = _strip_md_fence(payload)
        if not text:
            return []
        # try JSON first
        try:
            parsed = json.loads(text)
            return _coerce_items(parsed)
        except json.JSONDecodeError:
            pass
        return _items_from_prose(text)

    return []


def _items_from_prose_list(items: Iterable[Any]) -> List[dict]:
    """Map a list of non-dict entries (e.g. plain strings) to item dicts."""
    out: List[dict] = []
    for entry in items:
        if isinstance(entry, dict):
            out.append(entry)
        elif entry is not None:
            out.append({"recommendation": str(entry)})
    return out


_NUM_HEADER = re.compile(r"(?m)^\s*(?:\d+[\.\)]|[-*•])\s+")


def _items_from_prose(text: str) -> List[dict]:
    """Heuristically split free-form guidance into one item per numbered point.

    Splits on lines beginning with ``1.`` / ``2)`` / ``- `` / ``* ``. When no
    such markers exist, the whole blob becomes a single item so nothing is lost.
    """
    # Split keeping content after each marker.
    parts = _NUM_HEADER.split(text)
    # The first part is any preamble before the first marker — drop if blank.
    chunks = [p.strip() for p in parts if p and p.strip()]
    if len(chunks) <= 1:
        return [{"recommendation": text.strip()}]
    items: List[dict] = []
    for chunk in chunks:
        # Use a leading ALL-CAPS or "Label:" prefix as the category hint.
        category_hint = ""
        m = re.match(r"^([A-Z][A-Z _/]{2,40}):\s*(.*)$", chunk, flags=re.DOTALL)
        if m:
            category_hint = m.group(1).strip()
            chunk = m.group(2).strip()
        items.append({"recommendation": chunk, "category": category_hint})
    return items


# ---------------------------------------------------------------------------
# Classification / assessment — directive steps 1-6 (rule-based, zero LLM cost).
# ---------------------------------------------------------------------------
def _derive_tier(expected_impact: str, risk_level: str) -> int:
    """Map (impact, risk) -> tier per the prioritization framework.

    High impact is always TIER 1. Medium impact is TIER 2, escalated to TIER 1
    when paired with high risk (urgent enough to surface immediately). Low impact
    is TIER 3 unless the risk is high (a low-upside, high-downside item still
    deserves attention -> TIER 2).
    """
    if expected_impact == "high":
        return GuidanceTier.CRITICAL.value
    if expected_impact == "medium":
        return (
            GuidanceTier.CRITICAL.value if risk_level == "high" else GuidanceTier.HIGH_VALUE.value
        )
    # low impact
    return (
        GuidanceTier.HIGH_VALUE.value if risk_level == "high" else GuidanceTier.INFORMATIONAL.value
    )


def _acceptance_hint(feasibility: str, risk_level: str) -> str:
    """Advisory accept/reject/review hint (NOT an auto-action).

    * feasible + not-high-risk            -> "accept"
    * infeasible (low feasibility)        -> "reject"
    * otherwise (high risk, or unclear)   -> "review" (operator decides)
    """
    if feasibility == "low":
        return "reject"
    if risk_level == "high" and feasibility != "high":
        return "review"
    return "accept"


def _extract_action_plan(item: dict) -> List[str]:
    """Pull concrete action steps off the item (several common key spellings)."""
    for key in ("action_plan", "action_steps", "actions", "steps", "plan"):
        val = item.get(key)
        if isinstance(val, list):
            return [str(s).strip() for s in val if str(s).strip()]
        if isinstance(val, str) and val.strip():
            # split a single string on newlines / semicolons
            parts = re.split(r"[\n;]+", val)
            return [p.strip() for p in parts if p.strip()]
    return []


def _clean_text(*candidates: Any) -> str:
    """First truthy candidate coerced to a clean string.

    Tolerant of type drift in the LLM payload: a list is joined (not str()'d
    into ``"['a','b']"``); a dict/other is JSON-dumped; bools/0 are skipped by
    the truthiness check so they don't become the literal "False"/"0".
    """
    for c in candidates:
        if c is None or c is False or c == "" or c == [] or c == {}:
            continue
        if isinstance(c, str):
            s = c.strip()
        elif isinstance(c, list):
            s = "; ".join(str(x).strip() for x in c if str(x).strip())
        elif isinstance(c, dict):
            try:
                s = json.dumps(c, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                s = str(c)
        else:
            s = str(c).strip()
        if s:
            return s
    return ""


def _build_record(briefing_id: str, item: dict) -> GuidanceRecord:
    """Turn one raw guidance item into a fully-classified GuidanceRecord."""
    recommendation = _clean_text(
        item.get("recommendation"),
        item.get("text"),
        item.get("action"),
        item.get("title"),
    )
    rationale = _clean_text(item.get("rationale"), item.get("why"), item.get("reason"))

    category = GuidanceCategory.coerce(item.get("category") or item.get("goal") or item.get("type"))
    feasibility = coerce_level(item.get("feasibility"), default="medium")
    expected_impact = coerce_level(
        item.get("expected_impact") or item.get("impact"), default="medium"
    )
    risk_level = coerce_level(item.get("risk_level") or item.get("risk"), default="medium")

    tier = _derive_tier(expected_impact, risk_level)
    # OpenAI sometimes returns a pipe/comma/slash-delimited owner list
    # ("seo|outreach|strategy"); take the first concrete workcell so ownership
    # is a single routable target. Fall back to the category's default owner.
    raw_owner = str(item.get("owner") or item.get("suggested_owner") or "").strip()
    # first NON-EMPTY segment (a leading delimiter like "|outreach" must not
    # yield "" and silently fall through to the category default)
    owner = next((p.strip() for p in re.split(r"[|,/]", raw_owner) if p.strip()), "")
    if not owner:
        owner = DEFAULT_OWNER_BY_CATEGORY.get(category, "operator")
    action_plan = _extract_action_plan(item)
    hint = _acceptance_hint(feasibility, risk_level)

    now = iso_now()
    return GuidanceRecord(
        recommendation_id=uuid.uuid4().hex,
        briefing_id=briefing_id,
        ts=now,
        updated_ts=now,
        recommendation=recommendation,
        rationale=rationale,
        category=category.value,
        tier=tier,
        feasibility=feasibility,
        expected_impact=expected_impact,
        risk_level=risk_level,
        action_plan=action_plan,
        owner=owner,
        acceptance_hint=hint,
        status=GuidanceStatus.PROPOSED.value,
        source_question=(str(item.get("source_question")) if item.get("source_question") else None),
    )


# ---------------------------------------------------------------------------
# The ledger — append-only, latest-wins over recommendation_id.
# ---------------------------------------------------------------------------
class GuidanceLedger:
    """Durable, backend-portable store of guidance records.

    ``ledger`` is injectable for tests (any object honoring the ``append`` /
    ``scan`` contract). Default = ``open_ledger`` selecting jsonl or Firestore
    from ``SAMUS_LEDGER_BACKEND``.
    """

    def __init__(self, *, ledger: Optional[Any] = None) -> None:
        self._ledger = ledger

    # ---- backend resolution -------------------------------------------
    def _resolve(self):
        if self._ledger is not None:
            return self._ledger
        self._ledger = open_ledger(
            jsonl_path=state_path(*_LEDGER_JSONL),
            collection=_LEDGER_COLLECTION,
        )
        return self._ledger

    # ---- writes --------------------------------------------------------
    def append(self, record: GuidanceRecord) -> GuidanceRecord:
        """Append a record (new or a status-flipped copy). Never raises."""
        try:
            self._resolve().append(record.to_dict())
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            log.warning("guidance ledger append failed for %s: %s", record.recommendation_id, exc)
        return record

    # ---- reads ---------------------------------------------------------
    def _scan_rows(self) -> List[dict]:
        try:
            rows = self._resolve().scan()
        except Exception as exc:  # noqa: BLE001
            log.warning("guidance ledger scan failed: %s", exc)
            return []
        return [r for r in rows if isinstance(r, dict) and r.get("kind") == KIND]

    @staticmethod
    def _row_rank(row: dict) -> tuple:
        """Ordering key for latest-wins: (revision, updated_ts).

        ``revision`` is a monotonic per-recommendation counter bumped on every
        transition, so it — not scan order — decides which row is current. This
        is correct on the Firestore backend, whose ``order_by(time.time())`` is
        wall-clock (non-monotonic) with undefined tie ordering. ``updated_ts``
        is a secondary tiebreak; raw scan order is the final fallback (handled
        by ``>=`` in the reducer).
        """
        from .guidance_models import _safe_int

        return (_safe_int(row.get("revision", 0), 0), str(row.get("updated_ts", "")))

    def all_latest(self) -> List[GuidanceRecord]:
        """Every recommendation reduced to its current state by MAX(revision).

        Does NOT trust scan order (wrong on Firestore). One malformed/poison row
        is skipped + logged rather than crashing the whole read path.
        """
        best: dict[str, dict] = {}
        for row in self._scan_rows():
            rid = row.get("recommendation_id")
            if not rid:
                continue
            rid = str(rid)
            prev = best.get(rid)
            if prev is None or self._row_rank(row) >= self._row_rank(prev):
                best[rid] = row
        out: List[GuidanceRecord] = []
        for r in best.values():
            try:
                out.append(GuidanceRecord.from_dict(r))
            except Exception as exc:  # noqa: BLE001 — one poison row is not fatal
                log.warning(
                    "guidance: skipping malformed ledger row %s: %s",
                    r.get("recommendation_id"),
                    exc,
                )
        return out

    def get(self, recommendation_id: str) -> Optional[GuidanceRecord]:
        for rec in self.all_latest():
            if rec.recommendation_id == recommendation_id:
                return rec
        return None

    def open_items(self) -> List[GuidanceRecord]:
        """Non-terminal recommendations (still actionable)."""
        return [r for r in self.all_latest() if not r.is_terminal()]

    # ---- status transitions (append a flipped copy) --------------------
    def _transition(
        self,
        recommendation_id: str,
        new_status: GuidanceStatus,
        **fields: Any,
    ) -> Optional[GuidanceRecord]:
        rec = self.get(recommendation_id)
        if rec is None:
            log.warning("guidance transition: unknown recommendation_id %s", recommendation_id)
            return None
        rec.status = new_status.value
        rec.updated_ts = iso_now()
        rec.revision = rec.revision + 1  # monotonic — decides latest-wins
        for key, val in fields.items():
            if val is _UNSET:
                continue  # caller did not supply this field
            if not hasattr(rec, key):
                log.warning("guidance transition: unknown field %r ignored", key)
                continue
            setattr(rec, key, val)
        return self.append(rec)

    def accept(
        self, recommendation_id: str, *, action_plan: Optional[List[str]] = None
    ) -> Optional[GuidanceRecord]:
        """Accept a recommendation -> ACCEPTED (optionally refine the plan)."""
        return self._transition(
            recommendation_id,
            GuidanceStatus.ACCEPTED,
            action_plan=(action_plan if action_plan is not None else _UNSET),
        )

    def reject(
        self, recommendation_id: str, *, reason: Optional[str] = None
    ) -> Optional[GuidanceRecord]:
        return self._transition(
            recommendation_id,
            GuidanceStatus.REJECTED,
            outcome=(f"rejected: {reason}" if reason else "rejected"),
        )

    def start(self, recommendation_id: str) -> Optional[GuidanceRecord]:
        return self._transition(recommendation_id, GuidanceStatus.IN_PROGRESS)

    def abandon(
        self, recommendation_id: str, *, reason: Optional[str] = None
    ) -> Optional[GuidanceRecord]:
        return self._transition(
            recommendation_id,
            GuidanceStatus.ABANDONED,
            outcome=(f"abandoned: {reason}" if reason else "abandoned"),
        )

    def record_outcome(
        self,
        recommendation_id: str,
        *,
        outcome: str,
        impact_actual: Optional[str] = None,
        success_score: Optional[float] = None,
        lessons_learned: Optional[str] = None,
        implemented_actions: Optional[List[str]] = None,
    ) -> Optional[GuidanceRecord]:
        """Close out a recommendation with its measured effectiveness -> COMPLETED.

        This is the Guidance Effectiveness Tracking write: records the outcome,
        actual impact, a [0,1] success score, and lessons learned so the
        End-of-Day Operational Review can summarize what guidance worked.
        """
        score: Any = _UNSET
        if success_score is not None:
            try:
                score = max(0.0, min(1.0, float(success_score)))
            except (TypeError, ValueError):
                score = _UNSET
        return self._transition(
            recommendation_id,
            GuidanceStatus.COMPLETED,
            outcome=outcome,
            impact_actual=(coerce_level(impact_actual) if impact_actual else _UNSET),
            success_score=score,
            lessons_learned=(lessons_learned if lessons_learned is not None else _UNSET),
            implemented_actions=(
                implemented_actions if implemented_actions is not None else _UNSET
            ),
        )

    # ---- aggregate -----------------------------------------------------
    def effectiveness_summary(self) -> dict:
        """Aggregate view for the End-of-Day Operational Review.

        Counts by status + tier, and the mean success score over COMPLETED
        recommendations (the core "is the guidance working" metric).
        """
        recs = self.all_latest()
        by_status: dict[str, int] = {}
        by_tier: dict[int, int] = {}
        scores: List[float] = []
        for r in recs:
            by_status[r.status] = by_status.get(r.status, 0) + 1
            by_tier[r.tier] = by_tier.get(r.tier, 0) + 1
            if r.status == GuidanceStatus.COMPLETED.value and r.success_score is not None:
                scores.append(r.success_score)
        mean_score = round(sum(scores) / len(scores), 3) if scores else None
        return {
            "total": len(recs),
            "by_status": by_status,
            "by_tier": by_tier,
            "completed_count": len(scores),
            "mean_success_score": mean_score,
            "open_count": len([r for r in recs if not r.is_terminal()]),
        }

    # ---- compaction (supersession-based, NOT age-based) ----------------
    def compact(self) -> int:
        """Rewrite the ledger to only the current (latest-revision) rows.

        Every transition appends a full flipped row, so history grows without
        bound and every read does a full scan. Age-rotation is WRONG here — a
        still-open recommendation can be arbitrarily old. Instead we drop
        superseded revisions: keep exactly one row per recommendation_id (its
        ``all_latest()`` state). Only supported on the JSONL backend (the
        Firestore backend manages its own document set — one row per id is the
        natural shape there once we write by document id, out of scope here).

        Returns the number of rows dropped. Best-effort: any error is logged and
        returns 0 (the ledger is left intact). Call on a daily cadence.
        """
        from backend.common.persistence import JsonlLedger

        led = self._resolve()
        if not isinstance(led, JsonlLedger):
            log.info("guidance compact: skipped (non-jsonl backend)")
            return 0
        path = led.path
        try:
            before = led.scan()
        except Exception as exc:  # noqa: BLE001
            log.warning("guidance compact: scan failed: %s", exc)
            return 0
        current = {r.recommendation_id: r.to_dict() for r in self.all_latest()}
        kept_guidance = list(current.values())
        # Preserve any non-guidance rows that share the file (defensive).
        other = [r for r in before if isinstance(r, dict) and r.get("kind") != KIND]
        kept = other + kept_guidance
        dropped = len(before) - len(kept)
        if dropped <= 0:
            return 0
        try:
            import json as _json

            tmp = path.with_name(path.name + ".compact.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for row in kept:
                    fh.write(_json.dumps(row, ensure_ascii=False, default=str) + "\n")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001 — leave ledger intact on failure
            log.warning("guidance compact: rewrite failed: %s", exc)
            return 0
        log.info("guidance compact: dropped %d superseded rows", dropped)
        return dropped


# ---------------------------------------------------------------------------
# Top-level ingestion entry point.
# ---------------------------------------------------------------------------
def ingest_guidance(
    briefing_id: str,
    payload: Any,
    *,
    ledger: Optional[GuidanceLedger] = None,
) -> List[GuidanceRecord]:
    """Ingest an OpenAI guidance payload into durable, tracked records.

    ``briefing_id`` ties the records back to the briefing that prompted them.
    ``payload`` is whatever the advisor returned — structured JSON (preferred),
    a JSON string, or free-form prose; all are handled.

    Returns the list of created :class:`GuidanceRecord` (status PROPOSED). NEVER
    raises — a parse failure yields an empty list, a malformed item degrades to
    a best-effort record.
    """
    led = ledger if ledger is not None else GuidanceLedger()
    items = _coerce_items(payload)

    # Dedup-on-ingest: build the set of normalized texts already tracked by an
    # OPEN (non-terminal) record, so a recommendation re-emitted every cycle is
    # not re-proposed as a fresh duplicate. Seeded once per call (single scan);
    # updated as we go so duplicates WITHIN this payload also collapse.
    dedup = _dedup_enabled()
    seen: set[str] = set()
    if dedup:
        try:
            seen = {_norm_rec_text(r.recommendation) for r in led.open_items() if r.recommendation}
        except Exception as exc:  # noqa: BLE001 — dedup is best-effort, never fatal
            log.warning("guidance ingest: dedup preload failed (%s); proceeding", exc)
            seen = set()

    created: List[GuidanceRecord] = []
    skipped = 0
    for item in items:
        try:
            rec = _build_record(briefing_id, item)
        except Exception as exc:  # noqa: BLE001 — never let one bad item abort
            log.warning("guidance ingest: skipping malformed item: %s", exc)
            continue
        if not rec.recommendation:
            continue
        if dedup:
            key = _norm_rec_text(rec.recommendation)
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
        led.append(rec)
        created.append(rec)
    log.info(
        "guidance ingest: briefing=%s ingested=%d deduped=%d", briefing_id, len(created), skipped
    )
    return created


__all__ = [
    "GuidanceLedger",
    "ingest_guidance",
]
