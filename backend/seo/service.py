"""SEO workcell service orchestrators.

Three idempotent capabilities, one per pipeline stage:

    audit_site       -- HTTP GET + deterministic findings -> AuditResult
    optimize_page    -- AuditResult -> OptimizeResult
    generate_content -- OptimizeResult -> ContentResult (Anthropic or templated)

Idempotency is keyed on the target URL per stage. Audit events are appended
to ``SAMUS_SEO_AUDIT_PATH`` on each run.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from backend.common import events, persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.http_client import signed_post_json_sync
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from . import audit_cache
from .audit import audit_url
from .content import generate_content_drafts
from .models import (
    AuditRequest,
    AuditResult,
    ContentRequest,
    ContentResult,
    OptimizeRequest,
    OptimizeResult,
)
from .recommendations import build_recommendations
from .report import write_seo_report

_LOG = logging.getLogger("samus.seo.service")

_AUDIT_PATH_DEFAULT = "/opt/samus/data/seo/seo_audit.jsonl"


def _audit_ledger() -> persistence.JsonlLedger:
    path = os.getenv("SAMUS_SEO_AUDIT_PATH", _AUDIT_PATH_DEFAULT)
    return persistence.JsonlLedger(path)


def _dispatch_artifact_to_crm(payload: dict[str, Any]) -> None:
    """Best-effort enqueue of a ``create_artifact`` job for samus-crm.

    Dispatches via the gateway's ``POST /dispatch/crm`` so the gateway
    can route to the ``samus-crm-jobs`` SQS queue (or HTTP-fallback to
    samus-crm if no queue is configured). The producer's request stays
    alive for the duration of this sync call (~tens of ms with a healthy
    gateway), which means the dispatch survives Cloud Run scale-to-zero —
    a daemon-thread alternative does not.

    Wraps the HMAC-signed POST in a TaskEnvelope shape (task_id + payload
    + metadata.action). Config gaps (no gateway URL, no HMAC key) skip
    silently. Network failures are swallowed and logged — the producer
    never breaks because CRM is unreachable. Tests monkeypatch
    ``signed_post_json_sync`` to observe the dispatch without touching
    the network.
    """
    settings = get_settings()
    gateway_url = settings.gateway_urls.get("gateway")
    if not gateway_url:
        _LOG.debug("seo crm dispatch skipped: gateway_url_unset")
        return
    if not settings.shared_hmac_key:
        _LOG.debug("seo crm dispatch skipped: shared_hmac_key_unset")
        return

    envelope = {
        "task_id": f"seo-artifact-{payload.get('owner_entity_id') or 'unknown'}",
        "payload": payload,
        "metadata": {"action": "create_artifact"},
    }
    try:
        signed_post_json_sync(
            gateway_url,
            "/dispatch/crm",
            envelope,
            retries=2,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks producer
        _LOG.warning("seo crm artifact dispatch failed: %s", exc)


def audit_site(req: AuditRequest, *, force_refresh: bool = False) -> AuditResult:
    """Stage 1: deterministic page audit.

    When ``settings.seo_audit_cache_enabled`` is on (WIRED-DORMANT; default
    OFF), an ``input_hash``-keyed DURABLE cache short-circuits a repeat audit
    of an identical request within the TTL window — skipping the fetch/audit
    recompute AND the ledger + CRM-dispatch side effects. This closes the
    Cloud-Run cold-cache duplication (same site, same ``input_hash``, audited
    N times/day). With the flag off the path below is byte-for-byte unchanged.
    ``force_refresh`` bypasses the cache read and re-warms the entry.
    """
    cache_key = f"seo.audit:{req.url}"

    # WIRED-DORMANT durable cache (input_hash-keyed). No-op when the flag is
    # off — audit_cache reads settings.seo_audit_cache_enabled itself.
    hit = (
        None
        if force_refresh
        else audit_cache.get_cached(audit_cache.compute_input_hash(req.model_dump()))
    )
    if hit is not None:
        _LOG.info("audit_site durable cache hit url=%s", req.url)
        return AuditResult.model_validate(hit)

    cached = GLOBAL_IDEMPOTENCY_STORE.get(cache_key)
    if cached is not None and isinstance(cached, dict) and not force_refresh:
        _LOG.info("audit_site cache hit url=%s", req.url)
        return AuditResult.model_validate(cached)

    result = audit_url(req.url, req.keywords, req.industry)

    audit_event = events.build_audit_event(
        service="seo",
        task_id=req.url,
        action="audit_site",
        input_payload=req.model_dump(),
        output_payload=result.model_dump(),
        status="completed",
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("seo audit ledger append failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(cache_key, result.model_dump())
    # Durable-cache the fresh result (no-op when flag off).
    audit_cache.put_cached(audit_cache.compute_input_hash(req.model_dump()), result.model_dump())

    # Phase 5 — best-effort CRM artifact registration. Only fires when the
    # caller supplied a prospect_id so we have something to attach the
    # deliverable to; otherwise this is a URL-only ad-hoc audit and no CRM
    # row exists. Dispatch is non-blocking + never raises.
    if req.prospect_id:
        _dispatch_artifact_to_crm(
            {
                "kind": "seo_audit",
                "owner_entity_kind": "prospect",
                "owner_entity_id": req.prospect_id,
                "title": f"SEO audit: {req.url}",
                "inline_data": {
                    "url": result.url,
                    "seo_score": result.seo_score,
                    "issue_count": len(result.issues),
                },
                "source": "seo",
                "created_by": "samus-seo",
            }
        )

    return result


def optimize_page(req: OptimizeRequest) -> OptimizeResult:
    """Stage 2: turn audit findings into recommendations + on-page changes."""
    cache_key = f"seo.optimize:{req.url}"
    cached = GLOBAL_IDEMPOTENCY_STORE.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("optimize_page cache hit url=%s", req.url)
        return OptimizeResult.model_validate(cached)

    recs, on_page = build_recommendations(req.audit_data, req.target_keywords)
    result = OptimizeResult(
        url=req.url,
        recommendations=recs,
        on_page_changes=on_page,
        ts=iso_now(),
    )

    audit_event = events.build_audit_event(
        service="seo",
        task_id=req.url,
        action="optimize_page",
        input_payload=req.model_dump(),
        output_payload=result.model_dump(),
        status="completed",
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("seo audit ledger append failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(cache_key, result.model_dump())
    return result


def audit_and_report(
    req: AuditRequest,
    *,
    target_keywords: list[str] | None = None,
    tone: str = "professional",
    customer_label: str | None = None,
) -> dict:
    """End-to-end: audit -> optimize -> generate_content -> write markdown report."""
    cache_key = f"seo.audit_and_report:{req.url}"
    cached = GLOBAL_IDEMPOTENCY_STORE.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("audit_and_report cache hit url=%s", req.url)
        return cached

    keywords = target_keywords if target_keywords is not None else (req.keywords or [])
    audit_event_status = "completed"
    audit_event_err: str | None = None
    try:
        audit = audit_site(req)
        opt = optimize_page(
            OptimizeRequest(
                url=audit.url,
                audit_data=audit,
                target_keywords=keywords,
            )
        )
        content = generate_content(
            ContentRequest(
                url=opt.url,
                optimization_data=opt,
                target_keywords=keywords,
                tone=tone,
            )
        )
        report_path = write_seo_report(
            audit,
            opt,
            content,
            customer_label=customer_label,
        )
        result = {
            "audit": audit.model_dump(),
            "optimize": opt.model_dump(),
            "content": content.model_dump(),
            "report_path": str(report_path),
            "customer_slug": report_path.parent.name,
        }
    except Exception as exc:
        audit_event_status = "failed"
        audit_event_err = str(exc)
        audit_event = events.build_audit_event(
            service="seo",
            task_id=req.url,
            action="audit_and_report",
            input_payload=req.model_dump(),
            output_payload={"error": audit_event_err},
            status=audit_event_status,
        )
        try:
            _audit_ledger().append(audit_event)
        except OSError as ledger_exc:
            _LOG.warning("seo audit ledger append failed: %s", ledger_exc)
        raise

    audit_event = events.build_audit_event(
        service="seo",
        task_id=req.url,
        action="audit_and_report",
        input_payload=req.model_dump(),
        output_payload={
            "url": req.url,
            "report_path": result["report_path"],
            "customer_slug": result["customer_slug"],
            "seo_score": result["audit"]["seo_score"],
        },
        status=audit_event_status,
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("seo audit ledger append failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(cache_key, result)
    return result


def generate_content(req: ContentRequest) -> ContentResult:
    """Stage 3: generate page-content drafts (Anthropic or templated)."""
    cache_key = f"seo.content:{req.url}"
    cached = GLOBAL_IDEMPOTENCY_STORE.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("generate_content cache hit url=%s", req.url)
        return ContentResult.model_validate(cached)

    # Top-N deterministic gate: only fire LLM when the page actually benefits
    # from drafted content — at least 2 target keywords AND at least one
    # on-page change recommended by the optimizer. Lower-need pages stay on
    # the templated path even when an API key is configured. Keeps daily LLM
    # spend bounded under the Samus production cap.
    needs_llm = len(req.target_keywords) >= 2 and len(req.optimization_data.on_page_changes) >= 1
    # Business-gate via the anthropic_api_key parameter: None = skip LLM
    # (needs_llm=False -> not worth the invocation). Non-None fires the free
    # local LLM. The actual string is unused -- LM Studio needs no auth.
    key = "unused" if needs_llm else None
    drafts, word_count, used_llm, llm_cost_usd = generate_content_drafts(
        req.url,
        req.optimization_data,
        req.target_keywords,
        req.tone,
        anthropic_api_key=key,
    )
    result = ContentResult(
        url=req.url,
        page_drafts=drafts,
        word_count=word_count,
        used_llm=used_llm,
        llm_cost_usd=llm_cost_usd,
        ts=iso_now(),
    )

    audit_event = events.build_audit_event(
        service="seo",
        task_id=req.url,
        action="generate_content",
        input_payload=req.model_dump(),
        output_payload={"url": req.url, "word_count": word_count, "used_llm": used_llm},
        status="completed",
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("seo audit ledger append failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(cache_key, result.model_dump())
    return result
