"""Single LLM entry-point with layered budget enforcement.

Backend resolution (checked once at import):

  1. If ``OPENAI_API_KEY`` is set → OpenAI Chat Completions API.
  2. Otherwise → local LM Studio (OpenAI-compatible endpoint at
     ``SAMUS_LM_STUDIO_URL``, default ``localhost:1234``).

Both backends speak the same OpenAI Chat Completions wire format, so
the transport layer is identical — only URL, auth header, default model,
and pricing differ.

Every LLM-using caller in Samus *should* route through this module so
the per-workcell token budget (:mod:`backend.common.llm_budget`), the
global $-cap (:mod:`backend.common.llm_global_budget`), and the circuit
breaker all get a vote on whether the call goes out and how it's
accounted for.

Order of operations (token-cost-hardening 2026-05-18):

  1. Control B: model floor — pass-through for local LM Studio; enforced
     for OpenAI (rejects models not matching the configured default
     unless ``allow_expensive_model=True``).
  2. Control A: global $-cap — estimate USD via :mod:`llm_pricing` and
     check the GLOBAL DDB row. If we'd breach the daily cap, refuse.
  3. Existing per-workcell token quota — :func:`LlmBudgetStore.can_spend`.
  4. Existing circuit-breaker check (Control C) inside ``can_spend``.
  5. HTTP call to the resolved backend (OpenAI Chat Completions format).
  6. Post-flight: record actuals to both stores (per-workcell tokens
     and global dollars).

Surface:

  - :class:`BudgetExceeded` — raised when per-workcell quota denies.
  - :class:`GlobalBudgetExceeded` — raised when the global $-cap denies.
  - :class:`ModelNotPermitted` — raised when an expensive model is
    requested without ``allow_expensive_model=True`` (OpenAI backend).
  - :func:`anthropic_messages` — sync httpx wrapper around POST
    /v1/chat/completions. Name kept for backward compat with ~60
    callers. Returns ``(text, usage_dict)`` on success; raises one of
    the above or :class:`LlmCallError` on failure.
  - :func:`estimate_tokens` — conservative pre-flight estimate.

Why sync: every existing caller in Samus is sync, and FastAPI gladly runs
sync handlers in its threadpool.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import httpx

from . import broker_client as _broker_client
from . import llm_telemetry as _llm_telemetry
from .llm_budget import LlmBudgetStore, QuotaDecision, get_store
from .net_limits import LLM_MAX_BYTES, ResponseTooLarge, check_httpx_size
from .shared_http import get_shared_client
from .llm_global_budget import (
    GlobalDecision,
    LlmGlobalBudgetStore,
    get_global_store,
)


_LOG = logging.getLogger("samus.common.llm_client")

# ── Backend resolution ────────────────────────────────────────────────
# OPENAI_API_KEY present → OpenAI cloud backend.
# Otherwise           → local LM Studio (unchanged from pre-cloud).
_OPENAI_API_KEY: str | None = os.environ.get("OPENAI_API_KEY") or None
_USING_OPENAI: bool = _OPENAI_API_KEY is not None

if _USING_OPENAI:
    _COMPLETIONS_URL = os.environ.get(
        "SAMUS_OPENAI_URL",
        "https://api.openai.com/v1/chat/completions",
    )
    _DEFAULT_MODEL = os.environ.get("SAMUS_OPENAI_MODEL", "gpt-4.1-mini")
    _DEFAULT_TIMEOUT = float(os.environ.get("SAMUS_OPENAI_TIMEOUT_S", "60"))
    _LOG.info(
        "llm_client: using OpenAI backend model=%s url=%s",
        _DEFAULT_MODEL, _COMPLETIONS_URL.split("/v1")[0] + "/v1/...",
    )
else:
    _COMPLETIONS_URL = os.environ.get(
        "SAMUS_LM_STUDIO_URL",
        "http://127.0.0.1:1234/v1/chat/completions",
    )
    _DEFAULT_MODEL = "local"
    _DEFAULT_TIMEOUT = 30.0
    _LOG.info("llm_client: using LM Studio backend url=%s", _COMPLETIONS_URL)


def _backend_name() -> str:
    """Resolved backend for telemetry: 'openai' (paid) or 'local' (free).

    The backend is fixed at import (the OPENAI_API_KEY switch above), so this
    is a constant per process -- but stamping it on every call-trace record is
    what makes each reasoning call reconstructable as paid-vs-free after the
    fact, instead of implicit in a single import-time log line.
    """
    return _llm_telemetry.BACKEND_OPENAI if _USING_OPENAI else _llm_telemetry.BACKEND_LOCAL


class BudgetExceeded(Exception):
    """Raised pre-flight when the per-workcell daily quota is spent.

    Carries the :class:`QuotaDecision` so the caller can log it and decide
    whether to degrade gracefully (templated path) or surface the limit.
    """

    def __init__(self, decision: QuotaDecision) -> None:
        super().__init__(decision.reason or "budget_exceeded")
        self.decision = decision


class GlobalBudgetExceeded(Exception):
    """Raised pre-flight when the global $-cap (Control A) denies the call.

    Distinct from :class:`BudgetExceeded` because the operator response
    differs: per-workcell cap is per-day adaptive and self-heals; global
    cap is a hard ceiling and a denial means *every* workcell is now
    blocked until UTC midnight.
    """

    def __init__(self, decision: GlobalDecision) -> None:
        super().__init__(decision.reason or "global_budget_exceeded")
        self.decision = decision


class ModelNotPermitted(Exception):
    """Raised by Control B when a non-Haiku model is requested without opt-in.

    Stops accidental Opus / Sonnet calls that would cost 5-20x what the
    workcell budgeted for. Callers that genuinely need a bigger model
    pass ``allow_expensive_model=True``.
    """


class LlmCallError(Exception):
    """Raised when the LLM call itself fails (network, 5xx, parse).

    Distinct from the budget exceptions because these errors do NOT count
    against the workcell's efficiency EMA (a transient outage shouldn't
    punish future quota), but they DO count toward the Control C circuit
    breaker's ``consecutive_errors``.
    """


def estimate_tokens(*, prompt_text: str, max_tokens: int) -> int:
    """Conservative pre-flight estimate.

    Rough rule: ``1 token ≈ 4 chars`` for input, plus the caller's
    ``max_tokens`` cap for output. Anthropic's actual tokenizer is more
    accurate but importing the official tokenizer for one estimate doubles
    the wheel size — this rule overestimates slightly which is what we want
    (better to under-budget than blow through quota).
    """
    input_chars = max(0, len(prompt_text or ""))
    input_est = max(1, input_chars // 4)
    output_cap = max(0, int(max_tokens))
    return input_est + output_cap


def _estimate_input_output_split(prompt_text: str, max_tokens: int) -> tuple[int, int]:
    """Same heuristic as :func:`estimate_tokens` but returns input/output split.

    Used by the global $-cap so the pricing table can apply different
    rates to input vs output tokens (output is 5x input on Haiku).
    """
    input_chars = max(0, len(prompt_text or ""))
    input_est = max(1, input_chars // 4)
    output_cap = max(0, int(max_tokens))
    return input_est, output_cap


def _extract_text(response_json: dict[str, Any]) -> str:
    """Pull the assistant text from an OpenAI Chat Completions response."""
    choices = response_json.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise LlmCallError("llm response has no choices")
    message = (choices[0] or {}).get("message") or {}
    text = (message.get("content") or "").strip()
    if not text:
        raise LlmCallError("llm response has no text content")
    return text


def _extract_usage(response_json: dict[str, Any]) -> dict[str, int]:
    """Pull token counters from an OpenAI Chat Completions response.

    Always returns the four standard counters for backward compat with
    callers that inspect cache fields. LM Studio does not support prompt
    caching so cache fields are always 0.
    """
    usage = response_json.get("usage") or {}
    if not isinstance(usage, dict):
        _LOG.warning("llm response usage is not a dict: %r", usage)
        return {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }
    return {
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _publish_cache_counters(workcell: str, usage: dict[str, int]) -> None:
    """Emit Control D's cache-hit / cache-creation counters."""
    try:
        from . import metrics as _metrics
        creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
        read = int(usage.get("cache_read_input_tokens", 0) or 0)
        if creation:
            _metrics.SAMUS_LLM_CACHE_CREATIONS_TOTAL.labels(workcell=workcell).inc(creation)
        if read:
            _metrics.SAMUS_LLM_CACHE_HITS_TOTAL.labels(workcell=workcell).inc(read)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("llm_client cache-counter publish skipped: %s", exc)


def _publish_workcell_dollar_gauge(workcell: str, dollars_today: float, cap: float) -> None:
    """Emit per-workcell dollar gauge (best-effort)."""
    try:
        from . import metrics as _metrics
        _metrics.SAMUS_LLM_DOLLAR_USED_TODAY.labels(scope=workcell).set(dollars_today)
        _metrics.SAMUS_LLM_DOLLAR_CAP.labels(scope=workcell).set(cap)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("llm_client per-workcell dollar gauge skipped: %s", exc)


_OPENAI_EXPENSIVE_MODELS = re.compile(r"^(gpt-4\.1(?!-mini|-nano)|o[1-9]|o3|o4-mini)")


def _check_model_floor(model: str, allow_expensive_model: bool) -> None:
    """Control B: model floor.

    LM Studio: pass-through (any model loaded is fine).
    OpenAI:    blocks expensive models (gpt-4.1 full, o-series) unless
               ``allow_expensive_model=True``.
    """
    if not _USING_OPENAI:
        return
    if allow_expensive_model:
        return
    if _OPENAI_EXPENSIVE_MODELS.match(model):
        raise ModelNotPermitted(
            f"model {model!r} requires allow_expensive_model=True on OpenAI backend"
        )


# ── Free-local routing (2026-07-08) ───────────────────────────────────────
# The paid budget gates (global $-cap + per-workcell token quota) exist to
# control PAID spend. A local LM Studio call has zero marginal cost, so
# metering it against the paid budget is wrong — it caused the cognition
# workcell to exhaust its 200k-token quota and get DENIED even though the
# work it needed was free. A workcell listed in SAMUS_LLM_FREE_WORKCELLS (or a
# call passing prefer_local=True) routes to LM Studio regardless of the global
# OPENAI_API_KEY switch AND is exempt from both budget gates — it can reason as
# much as it needs. Telemetry still records each call (backend=local) for
# observability; only the COST accounting is skipped.
def _free_workcells() -> frozenset[str]:
    raw = os.environ.get("SAMUS_LLM_FREE_WORKCELLS", "") or ""
    return frozenset(w.strip() for w in raw.split(",") if w.strip())


def _is_free_workcell(workcell: str) -> bool:
    return workcell in _free_workcells()


def _lm_studio_completions_url() -> str:
    """The LM Studio chat-completions endpoint, normalised. SAMUS_LM_STUDIO_URL
    may be a bare base (…/v1) or a full completions URL; accept either."""
    raw = (os.environ.get("SAMUS_LM_STUDIO_URL", "")
           or "http://127.0.0.1:1234/v1/chat/completions").rstrip("/")
    if raw.endswith("/chat/completions"):
        return raw
    if raw.endswith("/v1"):
        return raw + "/chat/completions"
    return raw + "/v1/chat/completions"


def _complete_free_local(
    *, workcell: str, system: str | None, prompt: str,
    max_tokens: int, timeout: float, est: int,
) -> tuple[str, dict[str, int]]:
    """UNMETERED local completion (LM Studio). Bypasses every paid budget gate +
    the broker — local inference has no marginal cost, so a free-routed workcell
    is never denied by the paid quota. Same (text, usage) return contract and the
    same call-trace telemetry (backend=local) as the paid path; raises
    LlmCallError on transport/HTTP/parse errors."""
    url = _lm_studio_completions_url()
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": "local", "max_tokens": int(max_tokens), "messages": messages}

    _t0 = time.monotonic()

    def _telemetry(**kw: Any) -> None:
        try:
            _llm_telemetry.record_llm_call(
                workcell=workcell, backend=_llm_telemetry.BACKEND_LOCAL,
                model="local", est_tokens=est,
                latency_ms=(time.monotonic() - _t0) * 1000.0, **kw,
            )
        except Exception:  # noqa: BLE001 — telemetry never breaks the call
            pass

    try:
        client = get_shared_client(timeout=timeout)
        resp = client.post(url, headers={"content-type": "application/json"}, json=body)
    except httpx.HTTPError as exc:
        _telemetry(decision=_llm_telemetry.ROUTED, outcome="error",
                   reason=f"transport:{exc}")
        raise LlmCallError(f"llm_transport_error: {exc}") from exc
    try:
        check_httpx_size(resp, max_bytes=LLM_MAX_BYTES, source="llm")
    except ResponseTooLarge as exc:
        raise LlmCallError(f"llm_response_too_large: {exc}") from exc
    if resp.status_code >= 400:
        _telemetry(decision=_llm_telemetry.ROUTED, outcome="error",
                   reason=f"http_{resp.status_code}")
        raise LlmCallError(f"llm_http_{resp.status_code}: {(resp.text or '')[:200]}")
    try:
        payload = resp.json()
        text = _extract_text(payload)
        usage = _extract_usage(payload)
    except (ValueError, json.JSONDecodeError, LlmCallError) as exc:
        raise LlmCallError(f"llm_invalid_json: {exc}") from exc
    _telemetry(
        decision=_llm_telemetry.ROUTED, outcome="success", actual_cost_usd=0.0,
        used_tokens=int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0),
    )
    return text, usage


def anthropic_messages(
    *,
    workcell: str,
    api_key: str,
    prompt: str,
    system: str | None = None,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 1024,
    timeout: float = _DEFAULT_TIMEOUT,
    store: LlmBudgetStore | None = None,
    global_store: LlmGlobalBudgetStore | None = None,
    estimated_tokens: int | None = None,
    allow_expensive_model: bool = False,
    cache_system: bool = False,
    security_label: str = "general",
    prefer_local: bool = False,
) -> tuple[str, dict[str, int]]:
    """Send one Chat Completions call to LM Studio. Layered budget-gated.

    Returns ``(text, usage)``.

    .. note:: Function name kept as ``anthropic_messages`` for backward
       compatibility with ~60 callers. The transport now targets a local
       LM Studio instance (OpenAI-compatible API) for fully-offline
       operation.

    Parameters kept for backward compat but now ignored:

      - ``api_key``: LM Studio needs no auth; accepted but unused.
      - ``cache_system``: LM Studio has no prompt-cache support; no-op.
      - ``allow_expensive_model``: model floor is a pass-through (any
        model loaded in LM Studio is fine).

    Outcome handling (unchanged):

      - 2xx + parseable response  -> token spend recorded to per-workcell
        store with ``outcome='success'`` and to the global store with the
        actual $ figure. Caller may flip to 'failure' via ``record_outcome``
        if the model's answer didn't satisfy the task.
      - non-2xx / network error / parse error -> ``outcome='error'`` and
        ``LlmCallError`` raised. Error counts toward Control C's
        circuit breaker.
      - per-workcell quota deny  -> ``BudgetExceeded`` raised; no HTTP call.
      - global cap deny          -> ``GlobalBudgetExceeded`` raised; no HTTP call.
    """
    if not prompt:
        raise LlmCallError("anthropic_messages requires non-empty prompt")

    # api_key accepted for backward compat but not used (LM Studio needs no auth).

    # INV-9: every LLM call must carry a sensitivity label for audit/routing.
    _LOG.debug(
        "llm_call workcell=%s model=%s security_label=%s",
        workcell, model, security_label,
    )

    # Control B: model floor. Cheap to evaluate; runs first so an Opus
    # typo doesn't even touch the budget stores.
    _check_model_floor(model, allow_expensive_model)

    budget = store or get_store()
    global_budget = global_store or get_global_store()

    # Estimates feed both gates.
    est = estimated_tokens if estimated_tokens is not None else estimate_tokens(
        prompt_text=(system or "") + prompt, max_tokens=max_tokens,
    )
    est_input, est_output = _estimate_input_output_split(
        (system or "") + prompt, max_tokens,
    )

    # Free-local routing (2026-07-08): a free-listed workcell (or an explicit
    # prefer_local) runs on LM Studio, UNMETERED — bypassing the $-cap, the
    # per-workcell quota, and the broker, since local inference is free. This is
    # the fix for cognition exhausting its paid token quota on work that could
    # run free. Paid callers fall through to the gates below unchanged.
    if prefer_local or _is_free_workcell(workcell):
        return _complete_free_local(
            workcell=workcell, system=system, prompt=prompt,
            max_tokens=max_tokens, timeout=timeout, est=est,
        )

    # Control A: global $-cap. Runs BEFORE per-workcell because a global
    # cap breach blocks every workcell — fail fast and loudly.
    global_decision = global_budget.can_spend_global(
        model, est_input, est_output,
    )
    if not global_decision.allowed:
        _LOG.warning(
            "llm global cap denied workcell=%s model=%s est=$%.4f used=$%.4f cap=$%.2f",
            workcell, model, global_decision.estimated_usd,
            global_decision.used_usd, global_decision.cap_usd,
        )
        _llm_telemetry.record_llm_call(
            workcell=workcell, backend=_backend_name(),
            decision=_llm_telemetry.DENIED, model=model,
            control=_llm_telemetry.CONTROL_GLOBAL_CAP,
            reason=global_decision.reason or "global_cap_exceeded",
            est_tokens=est,
        )
        raise GlobalBudgetExceeded(global_decision)

    # Per-workcell quota + Control C circuit breaker.
    decision = budget.can_spend(workcell, est)
    if not decision.allowed:
        _LOG.info(
            "llm budget denied workcell=%s est=%s used=%s quota=%s reason=%s",
            workcell, est, decision.used, decision.quota, decision.reason,
        )
        _llm_telemetry.record_llm_call(
            workcell=workcell, backend=_backend_name(),
            decision=_llm_telemetry.DENIED, model=model,
            control=_llm_telemetry.classify_deny_control(decision.reason or ""),
            reason=decision.reason or "", est_tokens=est,
            used_tokens=int(getattr(decision, "used", 0) or 0),
            quota_tokens=int(getattr(decision, "quota", 0) or 0),
        )
        raise BudgetExceeded(decision)

    # Meta-governance L1 broker reservation. After local budget gates have
    # cleared and BEFORE the outbound HTTP call. The broker sees the SAME
    # estimated_usd that Samus's own global gate saw, so the two views of
    # spend reconcile. BrokerDenied is fail-closed and is translated into
    # BudgetExceeded at the boundary so existing callers keep handling a
    # single exception type — the broker reason flows through the
    # QuotaDecision.reason field for log correlation.
    try:
        _reservation = _broker_client.reserve(
            kind="llm_tokens",
            cost=float(global_decision.estimated_usd),
            priority=int(_broker_client.workcell_priority_for(workcell)),
            workcell=workcell,
        )
    except _broker_client.BrokerDenied as exc:
        _LOG.warning(
            "llm broker denied workcell=%s est=$%.4f reason=%s retry_after=%s",
            workcell, global_decision.estimated_usd, exc.reason,
            exc.retry_after_sec,
        )
        # Synthesise a QuotaDecision so the existing BudgetExceeded contract
        # holds — callers inspect ``decision.reason`` for the deny cause.
        synthesized = QuotaDecision(
            allowed=False,
            quota=decision.quota,
            used=decision.used,
            requested=decision.requested,
            reason=f"broker:{exc.reason}",
        )
        _llm_telemetry.record_llm_call(
            workcell=workcell, backend=_backend_name(),
            decision=_llm_telemetry.DENIED, model=model,
            control=_llm_telemetry.CONTROL_BROKER,
            reason=f"broker:{exc.reason}", est_tokens=est,
            used_tokens=int(getattr(decision, "used", 0) or 0),
            quota_tokens=int(getattr(decision, "quota", 0) or 0),
        )
        raise BudgetExceeded(synthesized) from exc

    headers: dict[str, str] = {
        "content-type": "application/json",
    }
    if _USING_OPENAI and _OPENAI_API_KEY:
        headers["authorization"] = f"Bearer {_OPENAI_API_KEY}"
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": messages,
    }

    _t0 = time.monotonic()
    try:
        client = get_shared_client(timeout=timeout)
        resp = client.post(_COMPLETIONS_URL, headers=headers, json=body)
    except httpx.HTTPError as exc:
        budget.record_spend(workcell, input_tokens=0, output_tokens=0, outcome="error")
        _broker_client.release(
            _reservation, actual_cost=0.0, outcome="error",
        )
        _llm_telemetry.record_llm_call(
            workcell=workcell, backend=_backend_name(),
            decision=_llm_telemetry.ROUTED, model=model, est_tokens=est,
            outcome="error", reason=f"transport:{exc}",
            latency_ms=(time.monotonic() - _t0) * 1000.0,
        )
        raise LlmCallError(f"llm_transport_error: {exc}") from exc

    # S3: bound the response body before any decode. The Anthropic endpoint is
    # trusted, but the channel is not — a MITM or a hijacked DNS answer could
    # return an unbounded body and OOM the worker. Post-hoc guard on the body
    # httpx already buffered; the cap is generous headroom over a real Messages
    # reply and the response is otherwise used unchanged (resp.text/resp.json).
    try:
        check_httpx_size(resp, max_bytes=LLM_MAX_BYTES, source="llm")
    except ResponseTooLarge as exc:
        budget.record_spend(workcell, input_tokens=0, output_tokens=0, outcome="error")
        _broker_client.release(_reservation, actual_cost=0.0, outcome="error")
        raise LlmCallError(f"llm_response_too_large: {exc}") from exc

    if resp.status_code >= 400:
        budget.record_spend(workcell, input_tokens=0, output_tokens=0, outcome="error")
        _broker_client.release(
            _reservation, actual_cost=0.0, outcome="error",
        )
        _llm_telemetry.record_llm_call(
            workcell=workcell, backend=_backend_name(),
            decision=_llm_telemetry.ROUTED, model=model, est_tokens=est,
            outcome="error", reason=f"http_{resp.status_code}",
            latency_ms=(time.monotonic() - _t0) * 1000.0,
        )
        snippet = (resp.text or "")[:200]
        raise LlmCallError(f"llm_http_{resp.status_code}: {snippet}")

    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        budget.record_spend(workcell, input_tokens=0, output_tokens=0, outcome="error")
        _broker_client.release(
            _reservation, actual_cost=0.0, outcome="error",
        )
        raise LlmCallError(f"llm_invalid_json: {exc}") from exc

    try:
        text = _extract_text(payload)
        usage = _extract_usage(payload)
    except LlmCallError:
        budget.record_spend(workcell, input_tokens=0, output_tokens=0, outcome="error")
        _broker_client.release(
            _reservation, actual_cost=0.0, outcome="error",
        )
        raise

    # Token spend is real; record it on the per-workcell store. Outcome
    # here is "success" because the API call succeeded — the caller may
    # overwrite to "failure" via record_outcome() if the model's answer
    # didn't satisfy the task.
    budget.record_spend(
        workcell,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        outcome="success",
    )

    # Compute actual cost for broker accounting.
    # Local inference is free; OpenAI has real per-token costs.
    if _USING_OPENAI:
        from .llm_pricing import cost_from_usage as _cost_fn
        _actual_cost = _cost_fn(model, usage)
    else:
        _actual_cost = 0.0
    _broker_client.release(
        _reservation, actual_cost=_actual_cost, outcome="ok",
    )

    # Control A: record actual $ on the global store. Cache tokens are
    # always 0 for LM Studio but the interface is preserved.
    try:
        g = global_budget.record_spend_global(
            model,
            actual_input=usage["input_tokens"],
            actual_output=usage["output_tokens"],
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        )
        _publish_workcell_dollar_gauge(
            workcell, g.dollars_today, global_budget.daily_dollar_cap,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("llm global spend recording failed: %s", exc)

    # Cache counters (always 0 for LM Studio, but kept for metric continuity).
    _publish_cache_counters(workcell, usage)

    # Cognitive call-trace: one reconstructable record for this reasoning call
    # -- which backend it billed (paid vs free), its actual cost and latency.
    # Fail-soft; never alters the return contract.
    _llm_telemetry.record_llm_call(
        workcell=workcell, backend=_backend_name(),
        decision=_llm_telemetry.ROUTED, model=model, est_tokens=est,
        used_tokens=int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0),
        quota_tokens=int(getattr(decision, "quota", 0) or 0),
        actual_cost_usd=_actual_cost, outcome="success",
        latency_ms=(time.monotonic() - _t0) * 1000.0,
    )

    return text, usage


def record_outcome(
    workcell: str, *, outcome: str,
    store: LlmBudgetStore | None = None,
) -> None:
    """Adjust the efficiency EMA after the caller validates the LLM output.

    Use case: a call returns text but parsing/validation fails downstream
    (e.g. the model didn't include a required JSON field). The token spend
    is real but the task-outcome is a failure — this lets the caller flip
    the EMA contribution without double-counting tokens.

    Recorded as a zero-token spend with the given outcome.
    """
    if outcome not in ("success", "failure", "error"):
        raise ValueError("outcome must be one of {success, failure, error}")
    budget = store or get_store()
    budget.record_spend(workcell, input_tokens=0, output_tokens=0, outcome=outcome)


__all__ = [
    "BudgetExceeded",
    "GlobalBudgetExceeded",
    "ModelNotPermitted",
    "LlmCallError",
    "anthropic_messages",
    "estimate_tokens",
    "record_outcome",
]
