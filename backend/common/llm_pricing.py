"""USD pricing table + cost calculator for LLM calls.

Source-of-truth for Control A (global $-cap) from the
token-cost-hardening 2026-05-18 brief. The global budget store calls
:func:`compute_cost_usd` to convert ``usage.input_tokens`` /
``usage.output_tokens`` into a single dollar figure that gets added
to today's running total on the GLOBAL DDB row.

LM Studio (local inference) is zero-cost. Legacy cloud pricing entries
are retained for accounting compatibility but are never matched when
the default model is ``local``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


class UnknownModelPricing(Exception):
    """Raised when a model id doesn't match any entry in PRICING_TABLE.

    Defense-in-depth: rather than guess a fallback price for an
    unknown model id (and silently undercharge the global cap), we
    raise so callers can decide whether to deny or fall through.
    The global-budget code treats this as a soft fail (allow but log)
    so a typo in the model name doesn't take down all LLM calls.
    """


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens."""

    model_pattern: str            # regex to match model id
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float


PRICING_TABLE: tuple[ModelPricing, ...] = (
    # LM Studio / local inference — zero cost (catch-all for any local model id).
    ModelPricing(
        r"^(local$|qwen|llama|gemma|mistral|phi-|deepseek|.+gguf)",
        input_per_mtok=0.0,
        output_per_mtok=0.0,
        cache_write_per_mtok=0.0,
        cache_read_per_mtok=0.0,
    ),
    # OpenAI models — used when OPENAI_API_KEY is set (Cloud Run deployment).
    ModelPricing(
        r"^gpt-4\.1-nano",
        input_per_mtok=0.10,
        output_per_mtok=0.40,
        cache_write_per_mtok=0.0,
        cache_read_per_mtok=0.025,
    ),
    ModelPricing(
        r"^gpt-4\.1-mini",
        input_per_mtok=0.40,
        output_per_mtok=1.60,
        cache_write_per_mtok=0.0,
        cache_read_per_mtok=0.10,
    ),
    ModelPricing(
        r"^gpt-4\.1($|-)",
        input_per_mtok=2.00,
        output_per_mtok=8.00,
        cache_write_per_mtok=0.0,
        cache_read_per_mtok=0.50,
    ),
    ModelPricing(
        r"^o4-mini",
        input_per_mtok=1.10,
        output_per_mtok=4.40,
        cache_write_per_mtok=0.0,
        cache_read_per_mtok=0.275,
    ),
    ModelPricing(
        r"^o3",
        input_per_mtok=2.00,
        output_per_mtok=8.00,
        cache_write_per_mtok=0.0,
        cache_read_per_mtok=0.50,
    ),
    # Legacy Anthropic cloud entries retained for accounting compatibility.
    ModelPricing(
        r"^claude-haiku-4",
        input_per_mtok=0.80,
        output_per_mtok=4.00,
        cache_write_per_mtok=1.00,
        cache_read_per_mtok=0.08,
    ),
    ModelPricing(
        r"^claude-sonnet-4",
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_write_per_mtok=3.75,
        cache_read_per_mtok=0.30,
    ),
    ModelPricing(
        r"^claude-opus-4",
        input_per_mtok=15.00,
        output_per_mtok=75.00,
        cache_write_per_mtok=18.75,
        cache_read_per_mtok=1.50,
    ),
)


# Pre-compile for hot path. Module-level so import-time cost is paid once.
_COMPILED: tuple[tuple[re.Pattern[str], ModelPricing], ...] = tuple(
    (re.compile(p.model_pattern), p) for p in PRICING_TABLE
)


def resolve_pricing(model: str) -> ModelPricing:
    """First-match-wins regex over PRICING_TABLE.

    Raises :class:`UnknownModelPricing` if no entry matches.
    """
    if not model:
        raise UnknownModelPricing("model id is empty")
    for pattern, pricing in _COMPILED:
        if pattern.match(model):
            return pricing
    raise UnknownModelPricing(f"no pricing entry matches model id: {model!r}")


# Decimal context constant — 1/MTok in 4-decimal cents form.
_PER_MTOK = Decimal("1000000")
_QUANT = Decimal("0.0001")


def compute_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Returns cost in USD (4-decimal precision).

    Note: when the Anthropic prompt-caching beta is active (Control D),
    ``input_tokens`` returned by the API is the *non-cached* input
    (the per-call delta) — the cache-read/write counters are split
    out separately. Callers should pass all four token counts as
    reported in ``usage`` to get an accurate $ figure.

    Uses Decimal arithmetic to avoid 0.1+0.2!=0.3 surprises when the
    global cap is doing addition across many calls. Result is rounded
    HALF_UP to the 4th decimal (1/100 of a cent) before being cast
    back to ``float`` for storage compatibility.
    """
    pricing = resolve_pricing(model)
    inp = max(0, int(input_tokens))
    out = max(0, int(output_tokens))
    cw = max(0, int(cache_write_tokens))
    cr = max(0, int(cache_read_tokens))

    total = (
        Decimal(inp) * Decimal(str(pricing.input_per_mtok)) / _PER_MTOK
        + Decimal(out) * Decimal(str(pricing.output_per_mtok)) / _PER_MTOK
        + Decimal(cw) * Decimal(str(pricing.cache_write_per_mtok)) / _PER_MTOK
        + Decimal(cr) * Decimal(str(pricing.cache_read_per_mtok)) / _PER_MTOK
    )
    rounded = total.quantize(_QUANT, rounding=ROUND_HALF_UP)
    return float(rounded)


def cost_from_usage(model: str, usage: dict[str, int] | None) -> float:
    """Best-effort: price an Anthropic ``usage`` block in USD.

    Convenience wrapper over :func:`compute_cost_usd` for callers that hold
    the four-counter ``usage`` dict returned by
    :func:`backend.common.llm_client.anthropic_messages` and want a single
    dollar figure for one call (strategy-integration build, Unit 4: feeding
    the per-prospect LLM cost into the reward signal).

    Reads ``input_tokens`` / ``output_tokens`` plus the two prompt-cache
    counters (``cache_creation_input_tokens`` / ``cache_read_input_tokens``)
    so a cache-warmed call is priced at its true (cheaper) cost.

    Best-effort by contract: an empty/None usage, a missing counter, or an
    unrecognised model id returns ``0.0`` rather than raising — the caller is
    cost telemetry on a side path and must never break the real work.
    """
    if not usage or not isinstance(usage, dict):
        return 0.0
    try:
        return compute_cost_usd(
            model,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
        )
    except (UnknownModelPricing, ValueError, TypeError):
        return 0.0


__all__ = [
    "ModelPricing",
    "PRICING_TABLE",
    "UnknownModelPricing",
    "compute_cost_usd",
    "cost_from_usage",
    "resolve_pricing",
]
