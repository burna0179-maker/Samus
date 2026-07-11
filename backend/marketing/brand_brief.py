"""Brand brief generator for self-marketing campaigns.

Produces a structured BrandBrief from a SelfMarketingCampaign config so the
content pipeline (blog_gen, repurpose, dispatch) has shared brand context
beyond raw keyword strings.

Mirrors backend.seo.blog_gen: LLM-assisted via budget-gated
``anthropic_messages``; deterministic template fallback whenever the LLM is
unavailable, over budget, or returns unparseable output. Always returns a
complete BrandBrief.

ASCII-only output. No new external dependencies.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from backend.common.dates import iso_now
from backend.common.llm_client import (
    BudgetExceeded,
    LlmCallError,
    anthropic_messages,
    record_outcome,
)
from backend.marketing.self_campaign import SelfMarketingCampaign

_LOG = logging.getLogger("samus.marketing.brand_brief")

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
_BUDGET_WORKCELL = "seo"
_BRIEF_MAX_TOKENS = 1000

# Hardcoded guards always appended to the brief's dont_say list. The LLM
# can extend dont_say but cannot override these. Keep this list short and
# surgical — every entry should be public-safety-relevant.
_HARDCODED_DONT_SAY: tuple[str, ...] = (
    "specific dollar amounts, percentages, multipliers, or counts that are not "
    "on the brand's public website",
    "internal agent names (Darwin, Anita, Optimus, Major, Sapphire, Samus)",
    "internal infrastructure terms (Agora, deliberation hub, stake sentence, "
    "audit chain, broker, ledger, governance hook, escalation queue)",
    "specific dates, incident timelines, production-readiness milestones, or deployment history",
    "first-person 'we run this on ourselves' / 'client zero' framing",
    "revolutionary, cutting-edge, world-class, game-changing, magical, "
    "set-and-forget, no-oversight",
)


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


@dataclass
class BrandBrief:
    """Structured brand brief consumed by downstream content generators."""

    brand: str
    one_liner: str  # 1-sentence elevator pitch (<=160 chars)
    positioning: str  # 60-120 word problem/mechanism/outcome
    voice_attributes: list[str]  # 3-5 short adjectives
    target_audience: str  # 1-2 sentences naming the buyer
    differentiators: list[str]  # 3-5 single-line bullets
    proof_points: list[str]  # 3-6 concrete claims (stats / dates / artifacts)
    key_messages: list[str]  # 3-5 messages every channel should land
    do_say: list[str]  # 3-6 phrases / topics to lean into
    dont_say: list[str]  # 3-5 phrases / topics to avoid
    tagline_candidates: list[str] = field(default_factory=list)
    used_llm: bool = False
    llm_cost_usd: float = 0.0
    ts: str = ""


# ---------------------------------------------------------------------------
# Template fallback
# ---------------------------------------------------------------------------


def _template_brief(
    campaign: SelfMarketingCampaign,
    extra_facts: list[str],
) -> BrandBrief:
    """Conservative website-only template fallback.

    Uses only information derivable from the SelfMarketingCampaign config
    (brand, site URL, keywords, themes) plus operator-supplied extra_facts.
    Never invents internal-system details, agent names, dollar amounts, or
    metrics. Safe to ship even with no LLM available.
    """
    brand = campaign.brand
    kws = list(campaign.target_keywords)

    one_liner = (
        f"{brand} helps small teams replace manual workflow steps with "
        f"systems that run on their own."
    )
    positioning = (
        f"{brand} builds workflow systems that connect the tools a small "
        f"team already uses so execution happens without manual handoffs. "
        f"The work is scoped, delivered, and measured against the outcomes "
        f"that matter to the operator."
    )
    voice = ["direct", "practical", "systems-focused", "plain-language"]
    audience = (
        "Operators of small teams that are losing time to manual handoffs, "
        "broken processes, and tools that do not talk to each other."
    )
    differentiators = [
        "Connects existing tools instead of replacing them",
        "Scoped engagements with a defined outcome, not open-ended retainers",
        "Plain operational language, not vendor jargon",
        "Built around making automation provable, not just promised",
    ]
    proof_points = list(extra_facts[:8])
    key_messages = [
        "Replace manual work with systems that run automatically",
        "Connect systems so execution stops breaking at handoffs",
        "Make automation provable through measurable outcomes",
    ]
    if kws:
        key_messages.append(f"Apply {kws[0]} where it removes the most friction first")
    do_say = [
        "workflow systems",
        "autonomous execution",
        "connect what you already use",
        "measurable outcomes",
        "real execution",
    ]
    dont_say = list(_HARDCODED_DONT_SAY)
    tagline_candidates = [
        "Autonomous systems. Real execution.",
        "Where work runs itself.",
        "Less manual. More measurable.",
    ]

    return BrandBrief(
        brand=brand,
        one_liner=one_liner,
        positioning=positioning,
        voice_attributes=voice,
        target_audience=audience,
        differentiators=differentiators,
        proof_points=proof_points,
        key_messages=key_messages,
        do_say=do_say,
        dont_say=dont_say,
        tagline_candidates=tagline_candidates,
        ts=iso_now(),
    )


# ---------------------------------------------------------------------------
# LLM prompt + parser
# ---------------------------------------------------------------------------

_BRIEF_SYSTEM = (
    "You are a deterministic brand-positioning module. Produce JSON-only output. "
    "Top-level keys: one_liner (str, <=160 chars), positioning (str, 60-120 words), "
    "voice_attributes (list of 3-5 short adjectives), target_audience (str, 1-2 sentences), "
    "differentiators (list of 3-5 single-line bullets), "
    "proof_points (list of 3-6 short claims; EVERY claim must be sourceable to "
    "the inputs given - do NOT invent stats, dates, customer counts, or dollar "
    "amounts), "
    "key_messages (list of 3-5 short message lines), "
    "do_say (list of 3-6 phrases/topics to lean into - prefer phrases that "
    "appear verbatim in the inputs), "
    "dont_say (list of 3-5 phrases/topics to avoid), "
    "tagline_candidates (list of 2-4 short taglines, <=60 chars each).\n\n"
    "Rules:\n"
    "- ASCII only, no em-dashes, no unicode bullets, use plain hyphens\n"
    "- No markdown formatting in strings, no surrounding prose\n"
    "- No hype superlatives (revolutionary, game-changing, world-class)\n"
    "- Concrete over abstract: prefer mechanism + outcome over vibes\n"
    "- NEVER invent a statistic, percentage, multiplier, customer count, "
    "revenue figure, or other number that is not explicitly in the inputs. "
    "If the inputs lack numbers, write proof_points qualitatively.\n"
    "- NEVER reveal internal implementation details (agent names, hub names, "
    "infrastructure, dates, incidents). If the inputs contain such details, "
    "ignore them. Use only inputs that describe what an outside customer "
    "could observe.\n"
    "Respond with JSON only."
)


def _build_brief_prompt(
    campaign: SelfMarketingCampaign,
    extra_facts: list[str],
) -> str:
    payload = {
        "brand": campaign.brand,
        "site_url": campaign.site_url,
        "target_keywords": campaign.target_keywords,
        "content_themes": campaign.content_themes,
        "extra_facts": extra_facts,
    }
    return f"Inputs:\n{json.dumps(payload, indent=2)}"


def _parse_brief_text(text: str, brand: str) -> BrandBrief:
    full = (text or "").strip()
    if not full:
        raise ValueError("empty response")
    if full.startswith("```"):
        lines = [ln for ln in full.splitlines() if not ln.strip().startswith("```")]
        full = "\n".join(lines).strip()

    parsed = json.loads(full)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected dict, got {type(parsed).__name__}")

    def req_str(key: str) -> str:
        v = parsed.get(key)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"missing or non-string field '{key}'")
        return v.strip()

    def req_list(key: str, min_len: int) -> list[str]:
        v = parsed.get(key)
        if not isinstance(v, list) or len(v) < min_len:
            raise ValueError(f"field '{key}' must be list of >= {min_len} items")
        return [str(x).strip() for x in v if str(x).strip()]

    return BrandBrief(
        brand=brand,
        one_liner=req_str("one_liner"),
        positioning=req_str("positioning"),
        voice_attributes=req_list("voice_attributes", 3),
        target_audience=req_str("target_audience"),
        differentiators=req_list("differentiators", 3),
        proof_points=req_list("proof_points", 3),
        key_messages=req_list("key_messages", 3),
        do_say=req_list("do_say", 3),
        dont_say=req_list("dont_say", 3),
        tagline_candidates=[
            str(x).strip() for x in (parsed.get("tagline_candidates") or []) if str(x).strip()
        ],
        ts=iso_now(),
    )


def _price_brief_usage(usage: dict[str, int] | None) -> float:
    try:
        from backend.common.llm_pricing import cost_from_usage

        return cost_from_usage(_ANTHROPIC_MODEL, usage)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("brand brief llm cost pricing skipped: %s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_brand_brief(
    campaign: SelfMarketingCampaign,
    *,
    anthropic_api_key: str | None = None,  # Unused — LM Studio backend needs no auth
    extra_facts: list[str] | None = None,
) -> BrandBrief:
    """Generate a structured BrandBrief for ``campaign``.

    Returns a fully-formed :class:`BrandBrief` regardless of LLM availability.
    ``used_llm=True`` only when the LLM call succeeded and was fully parsed.
    """
    facts = list(extra_facts or [])

    prompt = _build_brief_prompt(campaign, facts)
    try:
        text, usage = anthropic_messages(
            workcell=_BUDGET_WORKCELL,
            api_key="unused",
            prompt=prompt,
            system=_BRIEF_SYSTEM,
            cache_system=True,
            max_tokens=_BRIEF_MAX_TOKENS,
            security_label="brand_brief",
        )
    except BudgetExceeded as exc:
        _LOG.info(
            "brand brief budget denied workcell=%s reason=%s; falling back to template",
            _BUDGET_WORKCELL,
            exc.decision.reason,
        )
        return _template_brief(campaign, facts)
    except LlmCallError as exc:
        _LOG.warning("brand brief llm call failed, falling back to template: %s", exc)
        return _template_brief(campaign, facts)

    cost_usd = _price_brief_usage(usage)

    try:
        brief = _parse_brief_text(text, campaign.brand)
    except (ValueError, json.JSONDecodeError) as exc:
        record_outcome(_BUDGET_WORKCELL, outcome="failure")
        _LOG.warning("brand brief response unparseable, falling back to template: %s", exc)
        brief = _template_brief(campaign, facts)
        brief.llm_cost_usd = cost_usd
        return brief

    brief.used_llm = True
    brief.llm_cost_usd = cost_usd
    # Always append hardcoded guards (dedup, preserve LLM additions first).
    seen = {d.strip().lower() for d in brief.dont_say}
    for guard in _HARDCODED_DONT_SAY:
        if guard.strip().lower() not in seen:
            brief.dont_say.append(guard)
            seen.add(guard.strip().lower())
    return brief


# ---------------------------------------------------------------------------
# Persistence + voice-prompt rendering
# ---------------------------------------------------------------------------


_BRIEF_FILENAME = "brand_brief.json"


def _brief_dir(campaign_id: str) -> Path:
    """Resolve the on-disk directory for ``campaign_id``'s brief."""
    try:
        from backend.common import storage

        base = storage.root()
    except Exception:  # noqa: BLE001
        base = Path(os.getenv("SAMUS_ARTIFACT_ROOT", os.getenv("SAMUS_DATA_ROOT", "data")))
    return base / "marketing" / "briefs" / campaign_id


def load_brand_brief(campaign_id: str) -> BrandBrief | None:
    """Load a previously persisted BrandBrief; return None if absent or invalid."""
    path = _brief_dir(campaign_id) / _BRIEF_FILENAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return BrandBrief(
            brand=str(raw.get("brand", "")),
            one_liner=str(raw.get("one_liner", "")),
            positioning=str(raw.get("positioning", "")),
            voice_attributes=[str(x) for x in (raw.get("voice_attributes") or [])],
            target_audience=str(raw.get("target_audience", "")),
            differentiators=[str(x) for x in (raw.get("differentiators") or [])],
            proof_points=[str(x) for x in (raw.get("proof_points") or [])],
            key_messages=[str(x) for x in (raw.get("key_messages") or [])],
            do_say=[str(x) for x in (raw.get("do_say") or [])],
            dont_say=[str(x) for x in (raw.get("dont_say") or [])],
            tagline_candidates=[str(x) for x in (raw.get("tagline_candidates") or [])],
            used_llm=bool(raw.get("used_llm", False)),
            llm_cost_usd=float(raw.get("llm_cost_usd", 0.0) or 0.0),
            ts=str(raw.get("ts", "")),
        )
    except (OSError, ValueError, TypeError) as exc:
        _LOG.warning("load_brand_brief failed for %s: %s", campaign_id, exc)
        return None


def to_voice_prompt(brief: BrandBrief) -> str:
    """Render a BrandBrief as a voice/style preamble for downstream LLM calls.

    Returned text is safe to use as a ``system`` prompt or as the leading block
    of a user prompt. ASCII-only.

    The returned prompt always includes two hard rules: (1) the LLM must not
    invent statistics, percentages, or dollar amounts beyond those listed in
    proof_points; and (2) the LLM must not reveal internal implementation
    details. Callers cannot opt out of these rules.
    """
    lines = [
        f"Brand: {brief.brand}",
        f"One-liner: {brief.one_liner}",
        f"Positioning: {brief.positioning}",
        f"Target audience: {brief.target_audience}",
        "",
        "Voice attributes: " + ", ".join(brief.voice_attributes),
        "",
        "Key messages (land at least one per asset):",
        *[f"- {m}" for m in brief.key_messages],
        "",
        "Differentiators (use when relevant):",
        *[f"- {d}" for d in brief.differentiators],
        "",
        "Proof points (the ONLY specific claims you are allowed to make):",
        *[f"- {p}" for p in brief.proof_points],
        "",
        "DO use these words/phrases:",
        *[f"- {d}" for d in brief.do_say],
        "",
        "DO NOT use these words/phrases:",
        *[f"- {d}" for d in brief.dont_say],
        "",
        "HARD RULES (do not violate - your output will be rejected if you do):",
        "1. NO INVENTED NUMBERS OF ANY KIND. This includes percentages "
        "(40%, 30-40%), dollar amounts ($500, $2k/mo), time durations "
        "(10 hours a week, thirty minutes, 3-6 months, 24/7, in 30 days), "
        "multipliers (3x, 10x), customer counts (thousands of teams), "
        "ratios, frequencies, and dates. Every number must appear verbatim in "
        "the proof_points above or on the brand's public site. If you need "
        "specificity and have no allowed number, write qualitatively: say "
        "'meaningful lift' not '40% lift', 'fewer manual steps' not '10 hours "
        "a week saved', 'faster cycles' not '3x faster', 'always-on' not '24/7'.",
        "2. Do NOT reveal internal implementation details, infrastructure names, "
        "agent names, dates, or incident history even if they appear elsewhere in "
        "the context. Only write what an outside customer could already see on the "
        "brand's public site.",
        "3. Write in the brand's voice. Prefer concrete claims over hype. ASCII only.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runtime sanitizer (last-line defense against invented numbers)
# ---------------------------------------------------------------------------


import re as _re

# Patterns for inventable numeric claims. Each matches a candidate substring
# that, unless every digit it contains appears in the allowed token set, gets
# replaced with the paired [STAT: ...] placeholder.
#
# RANGES MUST APPEAR FIRST. Range patterns match e.g. "30-60 days" or "$50-200";
# if a single-side pattern fired first it would only catch one side of the
# range and leave the rest naked ("30-[STAT: time duration]").
_SCRUB_PATTERNS: tuple[tuple[_re.Pattern[str], str], ...] = (
    # ---- Ranges (must precede single-side patterns) ----
    (
        _re.compile(
            r"\$\s?\d+(?:[.,]\d+)?\s*(?:to|-)\s*\$?\s?\d+(?:[.,]\d+)?(?:\s*(?:k|m|mo|/mo|/month|/year|monthly|annually|per\s+month|per\s+user))?",
            _re.IGNORECASE,
        ),
        "[STAT: dollar amount]",
    ),
    (
        _re.compile(
            r"\b\d+(?:\.\d+)?\s*(?:to|-)\s*\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?|seconds?|days?|weeks?|months?|quarters?|years?)\b",
            _re.IGNORECASE,
        ),
        "[STAT: time duration]",
    ),
    (
        _re.compile(r"\b\d+(?:\.\d+)?\s*(?:to|-)\s*\d+(?:\.\d+)?\s*%", _re.IGNORECASE),
        "[STAT: percentage]",
    ),
    (
        _re.compile(
            r"\b\d+\s*(?:to|-)\s*\d+\s+(?:teams?|customers?|clients?|users?|businesses?|companies|operators?|founders?|tools?|platforms?|integrations?|tasks?)",
            _re.IGNORECASE,
        ),
        "[STAT: count range]",
    ),
    # Generic numeric range as last-resort range catch
    (_re.compile(r"\b\d+\s*(?:to|-)\s*\d+\b", _re.IGNORECASE), "[STAT: range]"),
    # ---- Single-side ----
    (_re.compile(r"\b\d+(?:\.\d+)?\s*%", _re.IGNORECASE), "[STAT: percentage]"),
    (_re.compile(r"\b\d+(?:\.\d+)?\s*(?:percent)\b", _re.IGNORECASE), "[STAT: percentage]"),
    (
        _re.compile(
            r"\$\s?\d+(?:[.,]\d+)*(?:\s*(?:k|m|mo|/mo|/month|/year|monthly|annually))?",
            _re.IGNORECASE,
        ),
        "[STAT: dollar amount]",
    ),
    (
        _re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:dollars?|usd|cents?)\b", _re.IGNORECASE),
        "[STAT: dollar amount]",
    ),
    (
        _re.compile(
            r"\b\d+(?:\.\d+)?\s*\+?\s*(?:hours?|hrs?|minutes?|mins?|seconds?|days?|weeks?|months?|quarters?|years?)\b",
            _re.IGNORECASE,
        ),
        "[STAT: time duration]",
    ),
    (_re.compile(r"\b\d+\s*x\b", _re.IGNORECASE), "[STAT: multiplier]"),
    (
        _re.compile(
            r"\b\d+\s*(?:times)\s+(?:more|less|faster|slower|higher|lower|larger|smaller)\b",
            _re.IGNORECASE,
        ),
        "[STAT: comparison]",
    ),
    (
        _re.compile(
            r"\b(?:thousands?|hundreds?|tens|millions?|dozens?)\s+of\s+(?:teams?|customers?|clients?|users?|businesses?|companies|operators?|founders?)",
            _re.IGNORECASE,
        ),
        "[STAT: customer count]",
    ),
    (
        _re.compile(
            r"\b\d+\s+(?:teams?|customers?|clients?|users?|businesses?|companies|operators?|founders?)\b",
            _re.IGNORECASE,
        ),
        "[STAT: customer count]",
    ),
)

# Third-party / competitor vendor names that should not appear in self-marketing.
# Substituted with a generic category descriptor. Word-boundary matched.
_VENDOR_BLACKLIST: tuple[tuple[_re.Pattern[str], str], ...] = (
    (_re.compile(r"\b(?:Zapier|Make\.com|Make,|Make )\b"), "your workflow platform "),
    (
        _re.compile(r"\b(?:n8n|Pabbly|Integromat|Tray\.io)\b", _re.IGNORECASE),
        "your workflow platform",
    ),
    (_re.compile(r"\b(?:Slack|Microsoft Teams|MS Teams|Teams)\b"), "your team chat"),
    (
        _re.compile(r"\b(?:Salesforce|HubSpot|Pipedrive|Close\.io|Close)\b", _re.IGNORECASE),
        "your CRM",
    ),
    (_re.compile(r"\b(?:Gmail|Outlook|Mailchimp|SendGrid)\b"), "your email"),
    (_re.compile(r"\b(?:Airtable|Notion|Coda|Smartsheet)\b"), "your database"),
)


def _extract_allowed_numbers(allowed_sources: list[str]) -> set[str]:
    """Pull every digit-bearing token out of trusted source strings."""
    allowed: set[str] = set()
    for s in allowed_sources:
        for m in _re.finditer(r"\d+(?:[.,]\d+)?", s):
            allowed.add(m.group())
    return allowed


# Preserve ANY square-bracketed block during scrubbing (covers our own
# [STAT: ...] placeholders AND any other [bracketed] content the model
# legitimately produced, e.g. reel timestamps like [00-05] or citation
# placeholders like [1]).
_STAT_BLOCK_RE = _re.compile(r"\[[^\]]*\]")


def _scrub_segment(text: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Scrub one segment that contains no [STAT: ...] markers."""
    flagged: list[str] = []

    def is_allowed(mt: str) -> bool:
        digits = _re.findall(r"\d+(?:[.,]\d+)?", mt)
        return bool(digits) and all(d in allowed for d in digits)

    out = text
    for pattern, placeholder in _SCRUB_PATTERNS:

        def _sub(m: _re.Match[str]) -> str:
            mt = m.group(0)
            if is_allowed(mt):
                return mt
            flagged.append(mt.strip())
            return placeholder

        out = pattern.sub(_sub, out)
    # Strip vendor/competitor names independent of the number guards.
    for pattern, replacement in _VENDOR_BLACKLIST:

        def _vsub(m: _re.Match[str]) -> str:
            flagged.append(m.group(0).strip())
            return replacement

        out = pattern.sub(_vsub, out)
    return out, flagged


def _post_rejoin_cleanup(text: str) -> str:
    """Run after segment rejoin: cleans up stranded numeric prefixes/suffixes
    around already-substituted [STAT: ...] placeholders that the per-segment
    pass cannot see (because the placeholder is in a sibling segment)."""
    out = text
    # Stranded prefix: "30-[STAT: ...]" → "[STAT: ...]"
    out = _re.sub(r"\b\d+(?:\.\d+)?\s*-\s*(?=\[STAT:)", "", out)
    # Stranded suffix: "[STAT: ...]-200 monthly" → "[STAT: ...]"
    # Alternatives ordered longest-first so "monthly" wins over "m".
    out = _re.sub(
        r"(?<=\])\s*-\s*\d+(?:\.\d+)?(?:\s*(?:monthly|annually|months?|weeks?|years?|hours?|days?|/month|/year|/mo|mo|k|m))?\b",
        "",
        out,
    )
    # Repair previously-corrupted suffixes ("]onthly" → "] monthly") if any
    # leak in from artifacts touched by a prior greedy build of this regex.
    out = _re.sub(r"\]onthly\b", "] monthly", out)
    out = _re.sub(r"\]nnually\b", "] annually", out)
    out = _re.sub(r"  +", " ", out)
    out = _re.sub(r"\s+,", ",", out)
    return out


def scrub_invented_numbers(
    text: str,
    *,
    allowed_sources: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Replace digit-bearing claims with [STAT: ...] placeholders.

    A match is preserved only if every digit run inside it also appears in
    ``allowed_sources`` (typically the brief's proof_points and the operator's
    extra_facts). Returns ``(scrubbed_text, flagged_substrings)``.

    Existing ``[STAT: ...]`` placeholders are preserved intact even when they
    contain digits, by splitting on them before substituting.
    """
    allowed = _extract_allowed_numbers(allowed_sources or [])

    # Split on STAT blocks, scrub only the non-block segments.
    pieces = _STAT_BLOCK_RE.split(text)
    blocks = _STAT_BLOCK_RE.findall(text)
    out_parts: list[str] = []
    all_flagged: list[str] = []
    for i, piece in enumerate(pieces):
        scrubbed, flagged = _scrub_segment(piece, allowed)
        out_parts.append(scrubbed)
        all_flagged.extend(flagged)
        if i < len(blocks):
            out_parts.append(blocks[i])
    rejoined = "".join(out_parts)
    return _post_rejoin_cleanup(rejoined), all_flagged


__all__ = [
    "BrandBrief",
    "generate_brand_brief",
    "load_brand_brief",
    "to_voice_prompt",
    "scrub_invented_numbers",
]
