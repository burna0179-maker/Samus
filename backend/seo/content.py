"""Content-draft generation for the SEO workcell.

Calls the LLM backend via ``anthropic_messages()`` and parses the JSON-only
response. Falls back to a deterministic templated draft on any transport /
parse error, or when the caller passes ``anthropic_api_key=None`` as a
business-gate signal that this page is not worth an LLM invocation (used by
:mod:`backend.seo.service`'s ``needs_llm`` check to skip low-value pages).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.common.llm_client import (
    BudgetExceeded,
    LlmCallError,
    anthropic_messages,
    record_outcome,
)

from .models import OptimizeResult

_LOG = logging.getLogger("samus.seo.content")

_LLM_MODEL = "local"

_LLM_MAX_TOKENS = 900
_BUDGET_WORKCELL = "seo"

_DRAFT_FIELDS = ("title", "meta_description", "h1", "body_intro", "body_main", "cta")


def _word_count(drafts: dict[str, str]) -> int:
    total = 0
    for v in drafts.values():
        if isinstance(v, str):
            total += len(v.split())
    return total


def _build_templated_drafts(
    url: str, optimization: OptimizeResult, target_keywords: list[str], tone: str
) -> dict[str, str]:
    primary_kw = target_keywords[0] if target_keywords else "local services"
    secondary = ", ".join(target_keywords[1:4]) if len(target_keywords) > 1 else ""
    on_page = optimization.on_page_changes or {}

    title = on_page.get("title") or f"{primary_kw.title()} | Reliable Local Experts"
    meta = on_page.get("meta_description") or (
        f"Need help with {primary_kw}? Our team delivers fast, transparent results. "
        "Get a free quote today."
    )
    h1 = on_page.get("h1") or f"{primary_kw.title()} You Can Trust"

    rec_lines = []
    for r in (optimization.recommendations or [])[:5]:
        rec_lines.append(f"- {r.action}")

    body_intro = (
        f"Looking for {primary_kw}? You're in the right place. "
        f"Our team focuses on delivering measurable outcomes in a {tone} way, "
        "with no surprises and no jargon."
    )
    body_main_parts = [
        f"We specialize in {primary_kw}.",
        f"Related services include: {secondary}." if secondary else "",
        "What you get when you work with us:",
        "- Transparent pricing with no hidden fees",
        "- Local team that responds within one business day",
        "- A clear plan before any work begins",
    ]
    if rec_lines:
        body_main_parts.append("Recent improvements we've made to this page:")
        body_main_parts.extend(rec_lines)
    body_main = "\n".join(p for p in body_main_parts if p)
    cta = f"Ready to get started with {primary_kw}? Request a free consultation today."

    return {
        "title": title,
        "meta_description": meta,
        "h1": h1,
        "body_intro": body_intro,
        "body_main": body_main,
        "cta": cta,
    }


# Lever 1.3 (token-cost-hardening 2026-05-18): static instructions + style /
# constraints block extracted so the budgeted path can pass it via the cached
# ``system`` field (Control D). Only the per-URL payload (recommendations,
# tone, target_keywords) varies — that stays in the user prompt.
_SEO_INSTRUCTIONS = (
    "You are a deterministic SEO content module. Produce JSON-only output "
    "with exactly these string keys: title, meta_description, h1, body_intro, "
    "body_main, cta.\n\n"
    "Style:\n"
    "  - title: <= 70 chars, include the primary keyword\n"
    "  - meta_description: 120-160 chars\n"
    "  - h1: short, keyword-led, max 12 words\n"
    "  - body_intro: 2-3 sentences\n"
    "  - body_main: 120-220 words, plain text, no markdown headings\n"
    "  - cta: one-sentence call to action\n\n"
    "Constraints: no marketing fluff, no emojis, no markdown.\n\n"
    "Respond with JSON only. No surrounding prose."
)


def _build_llm_prompt(
    url: str, optimization: OptimizeResult, target_keywords: list[str], tone: str
) -> str:
    """Build only the per-URL JSON payload. Instructions ride in ``system``
    via ``_SEO_INSTRUCTIONS`` so prompt caching (Control D) can warm-cache them.
    """
    rec_summary = [
        {"area": r.area, "action": r.action, "priority": r.priority}
        for r in optimization.recommendations
    ]
    payload = {
        "url": url,
        "target_keywords": target_keywords,
        "tone": tone,
        "on_page_changes": optimization.on_page_changes,
        "recommendations": rec_summary,
    }
    return f"Inputs:\n{json.dumps(payload, indent=2)}"


def _parse_llm_response(payload: dict[str, Any]) -> dict[str, str]:
    """Parse the 6-field drafts out of a raw Anthropic API response payload.

    Operates on the full ``response.json()`` dict from a direct httpx POST.
    Retained from origin/samus for direct-call tests and any caller that
    bypasses the llm_client abstraction.
    """
    content_blocks = payload.get("content") or []
    if not isinstance(content_blocks, list) or not content_blocks:
        raise ValueError("anthropic response has no content blocks")
    chunks: list[str] = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text") or ""
            if isinstance(txt, str):
                chunks.append(txt)
    full = "".join(chunks).strip()
    if not full:
        raise ValueError("anthropic response has no text content")
    if full.startswith("```"):
        lines = [ln for ln in full.splitlines() if not ln.strip().startswith("```")]
        full = "\n".join(lines).strip()

    parsed = json.loads(full)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected json object, got {type(parsed).__name__}")

    drafts: dict[str, str] = {}
    for field in _DRAFT_FIELDS:
        val = parsed.get(field, "")
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"anthropic response missing field '{field}'")
        drafts[field] = val.strip()
    return drafts


def _parse_llm_text(text: str) -> dict[str, str]:
    """Pull the 6-field drafts JSON object out of model text.

    Operates on the already-extracted text string returned by
    ``anthropic_messages``. Raises ``ValueError`` if the response can't be
    parsed or any required field is missing — callers translate that into a
    budget-side ``record_outcome("failure")`` so the workcell's efficiency
    EMA reflects that the call burned tokens without delivering usable output.
    """
    full = (text or "").strip()
    if not full:
        raise ValueError("anthropic response has no text content")
    if full.startswith("```"):
        lines = [ln for ln in full.splitlines() if not ln.strip().startswith("```")]
        full = "\n".join(lines).strip()

    parsed = json.loads(full)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected json object, got {type(parsed).__name__}")

    drafts: dict[str, str] = {}
    for field in _DRAFT_FIELDS:
        val = parsed.get(field, "")
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"anthropic response missing field '{field}'")
        drafts[field] = val.strip()
    return drafts


def _price_content_usage(usage: dict[str, int] | None) -> float:
    """Best-effort: dollar-cost one content-draft LLM call from its ``usage``.

    The content LLM path is pinned to ``_LLM_MODEL`` (Haiku), so that
    constant is the model id fed to the pricing table. Best-effort by
    contract — any pricing failure yields 0.0 (strategy-integration build,
    Unit 4: per-prospect LLM cost into the reward signal).
    """
    try:
        from backend.common.llm_pricing import cost_from_usage

        return cost_from_usage(_LLM_MODEL, usage)
    except Exception as exc:  # noqa: BLE001 — cost telemetry must never break work
        _LOG.debug("seo content llm cost pricing skipped: %s", exc)
        return 0.0


def generate_content_drafts(
    url: str,
    optimization: OptimizeResult,
    target_keywords: list[str],
    tone: str,
    *,
    anthropic_api_key: str | None,
) -> tuple[dict[str, str], int, bool, float]:
    """Generate page-content drafts. Falls back to templated on any failure.

    Returns ``(drafts, word_count, used_llm, llm_cost_usd)``. ``llm_cost_usd``
    (strategy-integration build, Unit 4) is the REAL Anthropic call priced
    from the response ``usage`` block — 0.0 on every templated / fallback
    path (no key, budget denied, transport error), and the priced cost when
    a call went out (including the parse-failure path, where tokens were
    spent but no usable content came back).

    Routes through :func:`backend.common.llm_client.anthropic_messages` so
    per-workcell token budgeting + outcome accounting are applied uniformly
    with every other LLM caller. Parse failures (model returned 200 but the
    JSON was malformed / missing fields) are reported back via
    ``record_outcome("failure")`` so the workcell's efficiency EMA reflects
    wasted spend.
    """
    # Business-gate: caller signals "skip LLM for this page" by passing None.
    # Any non-None value fires the (free) local LLM via `anthropic_messages()`
    # (the actual string is unused -- LM Studio needs no auth -- but callers
    # historically pass "unused" for legibility).
    if anthropic_api_key is None:
        drafts = _build_templated_drafts(url, optimization, target_keywords, tone)
        return drafts, _word_count(drafts), False, 0.0
    key = "unused"

    prompt = _build_llm_prompt(url, optimization, target_keywords, tone)

    try:
        text, usage = anthropic_messages(
            workcell=_BUDGET_WORKCELL,
            api_key=key,
            prompt=prompt,
            system=_SEO_INSTRUCTIONS,
            cache_system=True,
            max_tokens=_LLM_MAX_TOKENS,
            security_label="content_generation",
        )
    except BudgetExceeded as exc:
        _LOG.info(
            "llm budget denied workcell=%s reason=%s; falling back to templated",
            _BUDGET_WORKCELL,
            exc.decision.reason,
        )
        drafts = _build_templated_drafts(url, optimization, target_keywords, tone)
        return drafts, _word_count(drafts), False, 0.0
    except LlmCallError as exc:
        # Transport / 5xx / no-content. Wrapper already recorded outcome=error.
        _LOG.warning("anthropic content call failed, falling back to template: %s", exc)
        drafts = _build_templated_drafts(url, optimization, target_keywords, tone)
        return drafts, _word_count(drafts), False, 0.0

    # Tokens were spent regardless of whether the content parses — price the
    # real usage now so a wasted-but-billed call still counts.
    cost_usd = _price_content_usage(usage)

    try:
        drafts = _parse_llm_text(text)
    except (ValueError, json.JSONDecodeError) as exc:
        # 200 OK but content unparseable: tokens burned, no value -> failure.
        record_outcome(_BUDGET_WORKCELL, outcome="failure")
        _LOG.warning(
            "anthropic content unparseable, falling back to template: %s",
            exc,
        )
        drafts = _build_templated_drafts(url, optimization, target_keywords, tone)
        # used_llm stays False (no usable LLM content) but the dollars were
        # really spent — return the real cost so the reward signal sees it.
        return drafts, _word_count(drafts), False, cost_usd

    return drafts, _word_count(drafts), True, cost_usd
