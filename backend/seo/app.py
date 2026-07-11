"""SEO workcell FastAPI service.

Endpoints:
  POST /work             - TaskEnvelope wrapper, routes by metadata['action']
  POST /audit            - AuditRequest      (cap: audit_site)
  POST /optimize         - OptimizeRequest   (cap: optimize_page)
  POST /generate         - ContentRequest    (cap: generate_content)
  POST /audit_and_report - URL + keywords    (cap: audit_and_report)
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope
from backend.common.rate_limit import rate_limit_dependency

from .models import AuditRequest, ContentRequest, OptimizeRequest
from .service import audit_and_report, audit_site, generate_content, optimize_page

_LOG = logging.getLogger("samus.seo.app")


def _parse_envelope(body: Any) -> TaskEnvelope:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")
    try:
        return TaskEnvelope.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")


def _parse(model_cls, payload: Any):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"expected_{model_cls.__name__}_object")
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{model_cls.__name__}: {exc}")


def create_app():
    app = create_base_app(service_name="seo")

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        envelope = _parse_envelope(body)
        action = (envelope.metadata or {}).get("action") or "audit_site"
        if action == "audit_site":
            check_capability("seo", "audit_site")
            req = _parse(AuditRequest, envelope.payload)
            return audit_site(req).model_dump()
        if action == "optimize_page":
            check_capability("seo", "optimize_page")
            req = _parse(OptimizeRequest, envelope.payload)
            return optimize_page(req).model_dump()
        if action == "generate_content":
            check_capability("seo", "generate_content")
            req = _parse(ContentRequest, envelope.payload)
            return generate_content(req).model_dump()
        if action == "audit_and_report":
            check_capability("seo", "audit_and_report")
            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            return _run_audit_and_report(payload)
        # --- Growth-enrichment (dormant: no LLM spend unless caller opts in) ---
        if action == "geo_format":
            check_capability("seo", "geo_format")
            from backend.seo.geo_format import handle_geo_format

            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            return handle_geo_format(payload)
        if action == "aio_analyze":
            check_capability("seo", "aio_analyze")
            from backend.visibility.probe import handle_aio_analyze

            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            return handle_aio_analyze(payload)
        if action == "aio_probe":
            check_capability("seo", "aio_probe")
            from backend.visibility.probe import handle_aio_probe

            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            return handle_aio_probe(payload)
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post("/audit")
    async def audit(request: Request) -> dict[str, Any]:
        check_capability("seo", "audit_site")
        body = await request.json()
        req = _parse(AuditRequest, body)
        return audit_site(req).model_dump()

    @app.post("/optimize")
    async def optimize(request: Request) -> dict[str, Any]:
        check_capability("seo", "optimize_page")
        body = await request.json()
        req = _parse(OptimizeRequest, body)
        return optimize_page(req).model_dump()

    # M7 — /generate and /audit_and_report both run LLM-backed content
    # generation (and /audit_and_report also fetches an external URL). A
    # per-caller rate limit caps a runaway loop / a compromised caller before
    # it can burn the Anthropic budget. Env-tunable via
    # SAMUS_RATE_LIMIT_SEO_GENERATE_PER_MINUTE / ..._SEO_AUDIT_AND_REPORT_...
    @app.post(
        "/generate",
        dependencies=[Depends(rate_limit_dependency("seo_generate"))],
    )
    async def generate(request: Request) -> dict[str, Any]:
        check_capability("seo", "generate_content")
        body = await request.json()
        req = _parse(ContentRequest, body)
        return generate_content(req).model_dump()

    @app.post(
        "/audit_and_report",
        dependencies=[Depends(rate_limit_dependency("seo_audit_and_report"))],
    )
    async def audit_and_report_endpoint(request: Request) -> dict[str, Any]:
        check_capability("seo", "audit_and_report")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        return _run_audit_and_report(body)

    return app


def _run_audit_and_report(body: dict[str, Any]) -> dict[str, Any]:
    """Shared handler used by both /audit_and_report and /work."""
    audit_payload = {
        "url": body.get("url"),
        "keywords": body.get("keywords") or [],
        "industry": body.get("industry") or "",
    }
    req = _parse(AuditRequest, audit_payload)
    target_keywords = body.get("target_keywords")
    if target_keywords is not None and not isinstance(target_keywords, list):
        raise HTTPException(status_code=422, detail="target_keywords_must_be_list")
    tone = body.get("tone") or "professional"
    customer_label = body.get("customer_label")
    return audit_and_report(
        req,
        target_keywords=target_keywords,
        tone=tone,
        customer_label=customer_label,
    )


app = create_app()
