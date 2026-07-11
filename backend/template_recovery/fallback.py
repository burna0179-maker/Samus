"""Deterministic fallback executor for the template-recovery workcell.

Given a task kind plus context, returns the rendered scaffold. The render is
deterministic, local, cached and constant-time, and consumes ZERO LLM calls
— no LLM client is imported anywhere in this module. An unknown task kind
resolves to a safe generic scaffold rather than raising.

The cache keys on ``(task_kind, canonical(context))`` so a repeated recovery
request returns the identical previously-rendered scaffold without re-running
the builder. Builders are already pure, so the cache is a pure speed-up and
never changes observable output.
"""
from __future__ import annotations

import json
from typing import Any

from .selector import select_scaffold


class ScaffoldResult:
    """Immutable result of a deterministic fallback render."""

    __slots__ = ("task_kind", "scaffold", "template_version", "generic_fallback")

    def __init__(
        self,
        *,
        task_kind: str,
        scaffold: str,
        template_version: str,
        generic_fallback: bool,
    ) -> None:
        self.task_kind = task_kind
        self.scaffold = scaffold
        self.template_version = template_version
        self.generic_fallback = generic_fallback

    def as_dict(self) -> dict[str, Any]:
        """Flatten to a plain dict for serialisation / response models."""
        return {
            "task_kind": self.task_kind,
            "scaffold": self.scaffold,
            "template_version": self.template_version,
            "generic_fallback": self.generic_fallback,
        }


# Process-local render cache. Bounded indirectly by the small fixed set of
# task kinds + context shapes a workflow produces; cleared via clear_cache().
_RENDER_CACHE: dict[str, ScaffoldResult] = {}


def _cache_key(task_kind: str, context: dict[str, Any]) -> str:
    """Build a deterministic, order-independent cache key.

    ``json.dumps`` with ``sort_keys=True`` canonicalises the context so that
    two semantically-equal contexts hash to the same key. ``default=str``
    keeps the key build total even for non-JSON-native context values.
    """
    canonical = json.dumps(context, sort_keys=True, default=str, separators=(",", ":"))
    return f"{task_kind}\x00{canonical}"


def render_scaffold(task_kind: str, context: dict[str, Any] | None = None) -> ScaffoldResult:
    """Render the deterministic scaffold for ``task_kind`` + ``context``.

    Constant-time after warm-up: a repeated ``(task_kind, context)`` returns
    the cached result. An unknown task kind yields the safe generic scaffold
    (``generic_fallback=True``). Never raises on an unrecognised kind and
    never makes an LLM call.
    """
    ctx: dict[str, Any] = dict(context or {})
    key = _cache_key(task_kind, ctx)
    cached = _RENDER_CACHE.get(key)
    if cached is not None:
        return cached

    builder, template_version, is_generic = select_scaffold(task_kind, ctx)
    scaffold = builder(ctx)
    result = ScaffoldResult(
        task_kind=task_kind,
        scaffold=scaffold,
        template_version=template_version,
        generic_fallback=is_generic,
    )
    _RENDER_CACHE[key] = result
    return result


def clear_cache() -> None:
    """Drop the process-local render cache (tests / long-running processes)."""
    _RENDER_CACHE.clear()


__all__ = ["ScaffoldResult", "render_scaffold", "clear_cache"]
