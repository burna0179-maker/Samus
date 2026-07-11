"""Local analysis of call transcripts using LM Studio (qwen3-14b).

Runs fully on-device — no internet, no external API calls. Uses the
OpenAI-compatible endpoint at localhost:1234 via backend.common.local_llm.

For each transcript, Claude extracts:
  - outcome + reward signal (0.0–1.0)
  - objections raised + how handled + effectiveness score
  - talking points that landed vs flopped (specific, quotable)
  - conversion signals (prospect phrases predicting positive outcomes)
  - "what to listen for" — screening heuristics for future calls
  - prospect tier correction (hot/warm/low/dnc)
  - per-element script feedback (opener, voicemail, pitch, handlers)

When a ProspectRecord is available (passed from the privacy gate) the
prospect's callsheet context is included in the prompt so the model
can compare what was scripted vs what actually happened.

Each result is persisted to <artifact_root>/voice/analyses/<hash>.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

from backend.common import storage
from backend.common.dates import iso_now
from backend.common.local_llm import chat as llm_chat

from .transcript_ingest import RawTranscript

_LOG = logging.getLogger("samus.voice.transcript_analyzer")

_ANALYSES_SUBDIR = "voice/analyses"

_VALID_OUTCOMES = frozenset(
    {
        "converted",
        "warm_lead",
        "follow_up_scheduled",
        "call_back_requested",
        "objection_unresolved",
        "not_interested",
        "gatekeeper",
        "voicemail_left",
        "no_answer",
        "dnc_requested",
    }
)

_OUTCOME_REWARD: dict[str, float] = {
    "converted": 1.0,
    "follow_up_scheduled": 0.8,
    "call_back_requested": 0.7,
    "warm_lead": 0.6,
    "objection_unresolved": 0.3,
    "not_interested": 0.1,
    "gatekeeper": 0.05,
    "voicemail_left": 0.0,
    "no_answer": 0.0,
    "dnc_requested": 0.0,
}


@dataclass
class ObjectionRecord:
    objection_text: str
    how_handled: str
    effectiveness_score: float


@dataclass
class TranscriptAnalysis:
    source_file: str
    file_hash: str
    call_ts_iso: str
    direction: str
    contact_phone: str
    contact_name: str
    prospect_id: str | None
    company_name: str | None
    outcome: str
    reward: float
    objections_hit: list[ObjectionRecord] = field(default_factory=list)
    talking_points_landed: list[str] = field(default_factory=list)
    talking_points_flopped: list[str] = field(default_factory=list)
    conversion_signals: list[str] = field(default_factory=list)
    what_to_listen_for: list[str] = field(default_factory=list)
    prospect_tier_correction: str | None = None
    script_feedback: dict = field(default_factory=dict)
    llm_error: str | None = None
    analyzed_ts: str = ""


_SYSTEM = (
    "You are a sales call analyst for HustleForge, a local SEO and digital "
    "marketing agency selling to US small businesses. "
    "Analyze outbound sales call transcripts and extract structured intelligence: "
    "what worked, what failed, objection patterns, buying signals, and screening "
    "heuristics for future calls. "
    "Output ONLY a single valid JSON object — no prose, no markdown, no commentary."
)

_PROMPT = """\
Analyze this sales call transcript.

CALL INFO
contact: {contact_name} | phone: {contact_phone} | direction: {direction} | date: {call_ts}

PROSPECT CONTEXT
company: {company_name} | industry: {industry} | city: {city}, {state}
tier: {call_priority} | lead_score: {lead_score} | seo_score: {seo_score}

SCRIPTED CALLSHEET
opener:    {callsheet_opener}
pitch:     {callsheet_pitch}
offer:     {callsheet_offer}
voicemail: {callsheet_voicemail}
objection handlers:
{callsheet_objections}

TRANSCRIPT
{transcript_text}

OUTPUT SCHEMA (return only this JSON, nothing else):
{{
  "outcome": "converted|warm_lead|follow_up_scheduled|call_back_requested|objection_unresolved|not_interested|gatekeeper|voicemail_left|no_answer|dnc_requested",
  "reward": 0.0,
  "objections_hit": [
    {{"objection_text": "...", "how_handled": "...", "effectiveness_score": 0.0}}
  ],
  "talking_points_landed": ["specific quote or topic that got positive response"],
  "talking_points_flopped": ["specific quote or topic that got negative/no response"],
  "conversion_signals": ["exact prospect phrase signaling buying intent"],
  "what_to_listen_for": ["screening signal to apply on future cold calls"],
  "prospect_tier_correction": null,
  "script_feedback": {{
    "opener": null,
    "voicemail": null,
    "pitch": null,
    "objection_handlers": null
  }}
}}

Reward scale: converted=1.0, follow_up=0.8, callback=0.7, warm=0.6,
objection_unresolved=0.3, not_interested=0.1, gatekeeper=0.05, voicemail/no_answer=0.0.
prospect_tier_correction: null or one of hot/warm/low/dnc.
Be specific — generic phrases are not actionable.
"""


def analyze_transcript(
    raw: RawTranscript,
    *,
    prospect: object | None = None,
    dry_run: bool = False,
) -> TranscriptAnalysis:
    """Analyze a transcript locally. Never raises."""
    ts = iso_now()

    prospect_id = getattr(prospect, "prospect_id", None)
    company_name = getattr(prospect, "company_name", None)

    base = TranscriptAnalysis(
        source_file=raw.source_file,
        file_hash=raw.file_hash,
        call_ts_iso=raw.call_ts.isoformat(),
        direction=raw.direction,
        contact_phone=raw.contact_phone,
        contact_name=raw.contact_name,
        prospect_id=prospect_id,
        company_name=company_name,
        outcome="no_answer",
        reward=0.0,
        analyzed_ts=ts,
    )

    transcript_text = _build_text(raw)
    if not transcript_text.strip():
        base.llm_error = "empty_transcript"
        _persist(base)
        return base

    if dry_run:
        base.llm_error = "dry_run"
        _persist(base)
        return base

    prompt = _PROMPT.format(
        contact_name=raw.contact_name or "Unknown",
        contact_phone=raw.contact_phone or "Unknown",
        direction=raw.direction,
        call_ts=raw.call_ts.isoformat(),
        company_name=_pf(prospect, "company_name"),
        industry=_pf(prospect, "industry"),
        city=_pf(prospect, "city"),
        state=_pf(prospect, "state"),
        call_priority=_pf(prospect, "call_priority"),
        lead_score=_pf(prospect, "lead_score"),
        seo_score=_pf(prospect, "seo_score"),
        callsheet_opener=_pf(prospect, "callsheet_opener"),
        callsheet_pitch=_pf(prospect, "callsheet_pitch"),
        callsheet_offer=_pf(prospect, "callsheet_offer"),
        callsheet_voicemail=_pf(prospect, "callsheet_voicemail"),
        callsheet_objections=_format_objections(_pf(prospect, "callsheet_objections")),
        transcript_text=transcript_text[:6000],
    )

    raw_text = llm_chat(_SYSTEM, prompt, max_tokens=1200, temperature=0.0)

    if not raw_text.strip():
        base.llm_error = "lm_studio_empty_response"
        _persist(base)
        return base

    data, parse_err = _parse_json(raw_text)
    if parse_err:
        base.llm_error = f"json_parse_failed: {parse_err}"
        _LOG.warning("transcript_analyzer: %s for %s", base.llm_error, raw.source_file)
        _persist(base)
        return base

    outcome = str(data.get("outcome") or "no_answer")
    if outcome not in _VALID_OUTCOMES:
        outcome = "no_answer"

    try:
        reward = float(data.get("reward") or _OUTCOME_REWARD.get(outcome, 0.0))
        reward = max(0.0, min(1.0, reward))
    except (TypeError, ValueError):
        reward = _OUTCOME_REWARD.get(outcome, 0.0)

    objections: list[ObjectionRecord] = []
    for obj in (data.get("objections_hit") or [])[:10]:
        if not isinstance(obj, dict):
            continue
        try:
            eff = float(obj.get("effectiveness_score", 0.5))
            objections.append(
                ObjectionRecord(
                    objection_text=str(obj.get("objection_text") or "")[:200],
                    how_handled=str(obj.get("how_handled") or "")[:300],
                    effectiveness_score=max(0.0, min(1.0, eff)),
                )
            )
        except Exception:  # noqa: BLE001
            continue

    tier = data.get("prospect_tier_correction")
    if tier not in (None, "hot", "warm", "low", "dnc"):
        tier = None

    result = TranscriptAnalysis(
        source_file=raw.source_file,
        file_hash=raw.file_hash,
        call_ts_iso=raw.call_ts.isoformat(),
        direction=raw.direction,
        contact_phone=raw.contact_phone,
        contact_name=raw.contact_name,
        prospect_id=prospect_id,
        company_name=company_name,
        outcome=outcome,
        reward=reward,
        objections_hit=objections,
        talking_points_landed=_safe_list(data.get("talking_points_landed"), 20),
        talking_points_flopped=_safe_list(data.get("talking_points_flopped"), 20),
        conversion_signals=_safe_list(data.get("conversion_signals"), 20),
        what_to_listen_for=_safe_list(data.get("what_to_listen_for"), 20),
        prospect_tier_correction=tier,
        script_feedback=dict(data.get("script_feedback") or {}),
        analyzed_ts=ts,
    )
    _persist(result)
    _flow_reward_to_bandit(result)
    return result


def _flow_reward_to_bandit(analysis: TranscriptAnalysis) -> str:
    """Credit the call's [0,1] reward to the variant arm that placed it.

    Tranche 3 (learning loop): the dialer stamps a ``variant_arm_id`` on every
    dispatched call (:mod:`backend.voice.arm_stamp`); here the analyzed
    outcome flows back to :func:`backend.attribution.engine.record_outcome`
    so script variants learn. Fail-soft — no arm found or any error is a
    logged no-op. Returns the credited arm_id ("" when none).
    """
    try:
        from backend.attribution.engine import record_outcome
        from backend.voice.arm_stamp import lookup_arm

        arm_id = lookup_arm(
            prospect_id=analysis.prospect_id,
            phone=analysis.contact_phone,
        )
        if not arm_id:
            return ""
        record_outcome(
            arm_id,
            analysis.reward,
            won=(analysis.outcome == "converted"),
        )
        _LOG.info(
            "voice reward -> bandit arm=%s reward=%.2f outcome=%s",
            arm_id,
            analysis.reward,
            analysis.outcome,
        )
        return arm_id
    except Exception as exc:  # noqa: BLE001 — learning is never load-bearing
        _LOG.warning("voice reward flow failed: %s", exc)
        return ""


def _pf(prospect: object | None, field: str) -> str:
    """Safe field accessor for an optional ProspectRecord."""
    if prospect is None:
        return "(not provided)"
    val = getattr(prospect, field, None)
    return str(val) if val is not None else "(not provided)"


def _format_objections(raw: str) -> str:
    if not raw or raw == "(not provided)":
        return "  (none prepared)"
    return "\n".join(f"  - {h.strip()}" for h in raw.split(" | ") if h.strip())


def _build_text(raw: RawTranscript) -> str:
    if raw.turns and raw.parse_format in ("speaker_labeled_range", "speaker_labeled_bare"):
        return "\n".join(f"[{t.timestamp_offset}] {t.speaker}: {t.text}" for t in raw.turns)
    return raw.raw_text


def _parse_json(text: str) -> tuple[dict, str | None]:
    cleaned = text.strip()
    # Strip markdown fences if the model added them despite instructions
    if cleaned.startswith("```"):
        cleaned = "\n".join(ln for ln in cleaned.splitlines() if not ln.startswith("```")).strip()
    # Find the first { ... } block
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        return {}, "no_json_braces_found"
    try:
        return json.loads(cleaned[start : end + 1]), None
    except json.JSONDecodeError as exc:
        return {}, str(exc)


def _safe_list(val: object, max_items: int) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(v)[:200] for v in val[:max_items] if v]


def _persist(analysis: TranscriptAnalysis) -> None:
    try:
        target_dir = storage.root() / _ANALYSES_SUBDIR
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{analysis.file_hash}.json"
        path.write_text(
            json.dumps(asdict(analysis), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning("analysis persist failed %s: %s", analysis.file_hash, exc)


def load_all_analyses() -> list[TranscriptAnalysis]:
    """Load all persisted analyses from disk. Skips corrupt files."""
    target_dir = storage.root() / _ANALYSES_SUBDIR
    if not target_dir.exists():
        return []

    out: list[TranscriptAnalysis] = []
    for path in sorted(target_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            objs_raw = data.pop("objections_hit", None) or []
            objs = [ObjectionRecord(**o) for o in objs_raw if isinstance(o, dict)]
            ta = TranscriptAnalysis(**data)
            ta.objections_hit = objs
            out.append(ta)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("failed to load analysis %s: %s", path.name, exc)
    return out
