"""Query AI answer engines and assemble a visibility report.

Claude is queried live through the budget-gated ``anthropic_messages`` client;
OpenAI and Perplexity are queried over HTTP only when their API keys are
present (otherwise the probe records ``*_not_configured`` and moves on — never
an exception). Every probe is read-only and appended to a JSONL ledger.

``query_platform`` is the single network seam, so tests stub it to run the
whole pipeline offline.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from backend.visibility.analyze import aggregate, analyze_answer
from backend.visibility.models import (
    AiPlatform,
    CitationProbe,
    ShareOfVoice,
    VisibilityReport,
)

_LOG = logging.getLogger("samus.visibility.probe")

_BUDGET_WORKCELL = "visibility"
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_LEDGER_PATH = Path(
    os.getenv("SAMUS_VISIBILITY_LEDGER_PATH", "/opt/samus/data/visibility/probes.jsonl")
)


# ---------------------------------------------------------------------------
# Per-platform queries — each returns (text|None, error)
# ---------------------------------------------------------------------------


def _query_claude(question: str, *, workcell: str) -> tuple[str | None, str]:
    try:
        from backend.common.llm_client import anthropic_messages

        text, _usage = anthropic_messages(
            workcell=workcell, api_key="unused", prompt=question, max_tokens=700
        )
        return text, ""
    except Exception as exc:  # noqa: BLE001 — never raise out of a probe
        return None, f"claude_error:{type(exc).__name__}"


def _query_openai(question: str) -> tuple[str | None, str]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None, "openai_not_configured"
    return _chat_completions(
        "https://api.openai.com/v1/chat/completions",
        api_key,
        os.getenv("SAMUS_VISIBILITY_OPENAI_MODEL", "gpt-4o-mini"),
        question,
        "openai",
    )


def _query_perplexity(question: str) -> tuple[str | None, str]:
    api_key = os.getenv("PERPLEXITY_API_KEY", "")
    if not api_key:
        return None, "perplexity_not_configured"
    return _chat_completions(
        "https://api.perplexity.ai/chat/completions",
        api_key,
        os.getenv("SAMUS_VISIBILITY_PERPLEXITY_MODEL", "sonar"),
        question,
        "perplexity",
    )


def _chat_completions(
    url: str, api_key: str, model: str, question: str, label: str
) -> tuple[str | None, str]:
    """Shared OpenAI-compatible chat-completions call. Fail-closed."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [{"role": "user", "content": question}]},
            )
        if resp.status_code != 200:
            return None, f"{label}_http_{resp.status_code}"
        data = resp.json()
        text = (
            (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        )
        return (text, "") if text else (None, f"{label}_empty")
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        return None, f"{label}_error:{type(exc).__name__}"


def query_platform(
    platform: AiPlatform, question: str, *, workcell: str = _BUDGET_WORKCELL
) -> tuple[str | None, str]:
    """Dispatch one question to one platform. Returns ``(text|None, error)``."""
    if platform == "claude":
        return _query_claude(question, workcell=workcell)
    if platform == "openai":
        return _query_openai(question)
    if platform == "perplexity":
        return _query_perplexity(question)
    return None, f"unknown_platform:{platform}"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_probes(
    questions: list[str],
    brand_terms: list[str],
    competitor_terms: list[str],
    platforms: list[AiPlatform],
    *,
    workcell: str = _BUDGET_WORKCELL,
    ledger_path: str | Path | None = None,
    persist: bool = True,
) -> VisibilityReport:
    """Run every (question x platform) probe, analyze each answer, persist to
    the ledger, and return a full :class:`VisibilityReport` with aggregate
    share-of-voice."""
    probes: list[CitationProbe] = []
    analyses: list[dict] = []
    ledger = Path(ledger_path) if ledger_path else _LEDGER_PATH

    for question in questions:
        for platform in platforms:
            text, error = query_platform(platform, question, workcell=workcell)
            answered = text is not None
            if answered:
                a = analyze_answer(text, brand_terms, competitor_terms)
            else:
                a = {"brand_cited": False, "competitor_hits": {}, "cited_domains": []}
            probe = CitationProbe(
                query=question,
                platform=platform,
                answered=answered,
                brand_cited=a["brand_cited"],
                competitors_cited=list(a["competitor_hits"].keys()),
                cited_domains=a["cited_domains"],
                error=error,
                ts=_now_iso(),
            )
            probes.append(probe)
            analyses.append({"answered": answered, **a})

    sov_dict = aggregate(analyses)
    sov = ShareOfVoice(**sov_dict)
    report = VisibilityReport(
        brand_terms=brand_terms,
        competitor_terms=competitor_terms,
        platforms=platforms,
        probes=probes,
        sov=sov,
        generated_at=_now_iso(),
    )
    if persist:
        _persist(report, ledger)
    return report


def probe_enabled() -> bool:
    """Live probing spends paid tokens, so it is flag-gated default-OFF."""
    return os.getenv("SAMUS_VISIBILITY_PROBE_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ---------------------------------------------------------------------------
# HTTP-adapter handlers (dict in, dict out)
# ---------------------------------------------------------------------------


def handle_aio_analyze(payload: dict) -> dict:
    """Analyze caller-supplied AI answers — pure, NO LLM spend. ``answers`` is
    ``[{platform, query, text}]``; returns per-answer citations + aggregate."""
    brand = [str(b) for b in (payload.get("brand_terms") or [])]
    competitors = [str(c) for c in (payload.get("competitor_terms") or [])]
    analyses: list[dict] = []
    probes: list[dict] = []
    for ans in payload.get("answers") or []:
        if not isinstance(ans, dict):
            continue
        text = str(ans.get("text") or "")
        a = analyze_answer(text, brand, competitors)
        analyses.append({"answered": True, **a})
        probes.append(
            {
                "query": str(ans.get("query") or ""),
                "platform": str(ans.get("platform") or ""),
                "brand_cited": a["brand_cited"],
                "competitors_cited": list(a["competitor_hits"].keys()),
                "cited_domains": a["cited_domains"],
            }
        )
    return {"sov": aggregate(analyses), "probes": probes}


def handle_aio_probe(payload: dict) -> dict:
    """Run live probes across AI engines. Flag-gated default-OFF (spends tokens);
    returns ``{enabled: false}`` until ``SAMUS_VISIBILITY_PROBE_ENABLED`` is set."""
    if not probe_enabled():
        return {
            "enabled": False,
            "reason": "probe_disabled",
            "hint": "set SAMUS_VISIBILITY_PROBE_ENABLED=true to run live probes (spends tokens)",
        }
    questions = [str(q) for q in (payload.get("questions") or [])]
    brand = [str(b) for b in (payload.get("brand_terms") or [])]
    competitors = [str(c) for c in (payload.get("competitor_terms") or [])]
    platforms = [str(p) for p in (payload.get("platforms") or ["claude"])]
    report = run_probes(questions, brand, competitors, platforms, persist=bool(payload.get("persist", True)))  # type: ignore[arg-type]
    return {
        "enabled": True,
        "sov": asdict(report.sov) if report.sov else None,
        "probes": [asdict(p) for p in report.probes],
    }


def _persist(report: VisibilityReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as fh:
            header = {
                "_kind": "visibility_run",
                "generated_at": report.generated_at,
                "brand_terms": report.brand_terms,
                "platforms": report.platforms,
                "sov": asdict(report.sov) if report.sov else None,
            }
            fh.write(json.dumps(header, ensure_ascii=False, default=str) + "\n")
            for probe in report.probes:
                fh.write(json.dumps(asdict(probe), ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        _LOG.warning("visibility ledger append failed: %s", exc)
