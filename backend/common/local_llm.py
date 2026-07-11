"""Thin LLM client for lightweight one-shot completions (voice analysis, etc.).

Backend resolution (same as llm_client.py):

  1. If ``OPENAI_API_KEY`` is set → OpenAI Chat Completions API.
  2. Otherwise → local LM Studio (OpenAI-compatible endpoint at
     ``SAMUS_LM_STUDIO_URL``, default ``localhost:1234``).

Uses httpx (already a Samus dependency). Never raises on network failure —
returns an empty string and logs a warning so callers can degrade gracefully.

qwen3 thinking suppression: /no_think is prepended to every user message
when using LM Studio so the model skips its <think> preamble. Suppressed
on OpenAI (not applicable).
"""

from __future__ import annotations

import logging
import os

import httpx

_LOG = logging.getLogger("samus.common.local_llm")

# ── Backend resolution — local primary, OpenAI backup ─────────────────
#
# Both backends are configured independently. ``chat()`` tries them in order
# (default: local LM Studio first, OpenAI as fallback) and returns the first
# NON-EMPTY result. Rationale, hard-earned 2026-06-30: when LM Studio went dark
# (a URL bug → HTTP 200 + empty choices) the WHOLE offline reasoning stack
# silently produced nothing, with no fallback. Samus has an OpenAI key in its
# DPAPI store; wiring it as a backup means the reasoning stack degrades to a
# paid call instead of going dark. It ALSO fixes the cloud case (no LM Studio
# present → the local attempt connect-refuses instantly → OpenAI takes over).
#
# Cost posture: local is FREE and tried first, so OpenAI only bills when local
# is unavailable/empty. Set SAMUS_LLM_PRIMARY=openai to flip the order for
# deployments that prefer OpenAI primary (e.g. cloud with no local model).

_LOCAL_BASE = os.getenv("SAMUS_LM_STUDIO_URL", "http://127.0.0.1:1234/v1")
_LOCAL_MODEL = os.getenv("SAMUS_LM_STUDIO_MODEL", "local")
_LOCAL_TIMEOUT = float(os.getenv("SAMUS_LM_STUDIO_TIMEOUT_S", "90"))

_OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY") or None
_OPENAI_BASE = os.getenv("SAMUS_OPENAI_URL", "https://api.openai.com/v1")
_OPENAI_MODEL = os.getenv("SAMUS_OPENAI_MODEL", "gpt-4.1-mini")
_OPENAI_TIMEOUT = float(os.getenv("SAMUS_OPENAI_TIMEOUT_S", "60"))

# Primary backend: "local" (default) or "openai".
_PRIMARY = (os.getenv("SAMUS_LLM_PRIMARY", "local") or "local").strip().lower()

# Back-compat: some callers/tests read these module attrs. _USING_OPENAI now
# means "OpenAI is the PRIMARY backend"; _BASE_URL mirrors the primary.
_USING_OPENAI: bool = _PRIMARY == "openai"
_BASE_URL = _OPENAI_BASE if _USING_OPENAI else _LOCAL_BASE

_NO_THINK_PREFIX = "/no_think\n\n"


def _resolve_chat_endpoint(base_url: str) -> str:
    """Derive the /chat/completions endpoint, tolerant of how ``base_url`` is set.

    Historically ``SAMUS_LM_STUDIO_URL`` was the bare base ("…/v1") and this
    appended "/chat/completions". But an operator (or a compose default) can
    just as reasonably set the FULL URL ("…/v1/chat/completions") — in which
    case a blind append produces "…/chat/completions/chat/completions", which
    LM Studio answers with HTTP 200 + an EMPTY choices array. That silent
    failure took the whole offline reasoning stack dark on 2026-06-30. Normalise
    both forms (and a bare "host:port") so the endpoint is correct regardless.
    """
    b = (base_url or "").rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/v1"):
        return b + "/chat/completions"
    # Bare host:port (or anything else) — assume OpenAI-compatible v1 path.
    return b + "/v1/chat/completions"


def _call_backend(
    *,
    kind: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    timeout: float | None,
) -> str:
    """One HTTP completion against a single backend (``kind`` = local|openai).

    Returns the content string, or "" on any failure / empty response so the
    caller can fall through to the next backend.
    """
    if kind == "openai":
        if not _OPENAI_API_KEY:
            return ""
        endpoint = _resolve_chat_endpoint(_OPENAI_BASE)
        model, tmo = _OPENAI_MODEL, (timeout or _OPENAI_TIMEOUT)
        user_content = user  # OpenAI ignores the qwen /no_think directive
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_OPENAI_API_KEY}"}
    else:  # local LM Studio
        endpoint = _resolve_chat_endpoint(_LOCAL_BASE)
        model, tmo = _LOCAL_MODEL, (timeout or _LOCAL_TIMEOUT)
        user_content = _NO_THINK_PREFIX + user
        headers = {"Content-Type": "application/json"}

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        resp = httpx.post(endpoint, json=body, headers=headers, timeout=tmo)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            _LOG.warning("local_llm[%s]: empty choices in response", kind)
            return ""
        content = (choices[0].get("message") or {}).get("content") or ""
        return content if isinstance(content, str) else ""
    except httpx.TimeoutException:
        _LOG.warning("local_llm[%s]: timed out after %.0fs", kind, tmo)
        return ""
    except httpx.HTTPStatusError as exc:
        _LOG.warning(
            "local_llm[%s]: HTTP %s — %s", kind, exc.response.status_code, exc.response.text[:200]
        )
        return ""
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("local_llm[%s]: unexpected error: %s", kind, exc)
        return ""


def _backend_order() -> list[str]:
    """Ordered backends to try. Default local→openai; openai-primary flips it.
    OpenAI is only included when a key is configured."""
    primary, secondary = ("openai", "local") if _PRIMARY == "openai" else ("local", "openai")
    order = [primary, secondary]
    return [b for b in order if b == "local" or _OPENAI_API_KEY]


def chat(
    system: str,
    user: str,
    *,
    max_tokens: int = 1200,
    temperature: float = 0.0,
    timeout: float | None = None,
) -> str:
    """One-shot completion with automatic backend fallback.

    Tries each configured backend in order (local first by default, OpenAI as
    backup) and returns the FIRST non-empty result. Returns "" only if every
    backend failed or returned empty. Never raises.
    """
    backends = _backend_order()
    for i, kind in enumerate(backends):
        out = _call_backend(
            kind=kind,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        if out.strip():
            if i > 0:
                _LOG.info("local_llm: served by fallback backend '%s'", kind)
            return out
    return ""


def is_available() -> bool:
    """Probe the primary backend's /models. True iff it answers 200 within 2s.
    (OpenAI fallback isn't probed here — chat() falls through to it on demand.)"""
    try:
        base = _OPENAI_BASE if _USING_OPENAI else _LOCAL_BASE
        probe = base.rstrip("/") + "/models"
        headers: dict[str, str] = {}
        if _USING_OPENAI and _OPENAI_API_KEY:
            headers["Authorization"] = f"Bearer {_OPENAI_API_KEY}"
        resp = httpx.get(probe, timeout=2.0, headers=headers)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False
