"""Pydantic models for the template-recovery workcell.

A recovery request describes a failed LLM-driven step; a recovery response
carries the deterministic scaffold that lets the workflow continue without
spending more tokens.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RecoveryRequest(BaseModel):
    """Inbound request describing a failed LLM-driven step.

    ``task_kind`` is the kind of task that failed (e.g. ``seo_audit``,
    ``proposal``, ``cold_outreach``, ``callsheet``). ``context`` carries the
    deterministic inputs the template builder fills its fixed structure from
    (target keywords, business name, etc.). ``failure_reason`` is a free-text
    note on why the LLM step failed — recorded for audit, never re-prompted.
    """

    model_config = ConfigDict(extra="forbid")

    task_kind: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str = Field(default="", max_length=2000)


class RecoveryResponse(BaseModel):
    """Outbound response carrying the deterministic scaffold.

    ``scaffold`` is the rendered template content. ``template_version`` names
    the exact versioned builder that served it (e.g. ``seo_template_v3``).
    ``fallback_triggered`` is always ``True`` for a served recovery — it is
    the explicit signal to the caller that a deterministic scaffold (not an
    LLM result) is being returned.
    """

    model_config = ConfigDict(extra="forbid")

    task_kind: str
    scaffold: str
    template_version: str
    fallback_triggered: bool = True
    generic_fallback: bool = False
    failure_reason: str = ""


__all__ = ["RecoveryRequest", "RecoveryResponse"]
