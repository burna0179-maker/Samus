"""Codex Validation Layer — runtime enforcement of `docs/codex/` rules.

The Codex (docs/codex/00..11) is the design's shared memory. This package
parses the chapters that carry enforceable rules — guardrails (G1-G11) and
decisions (ADR-001..) — and exposes a single `check_action(ProposedAction)`
gate that workcells call BEFORE running any outbound or load-bearing
action. Violations block the action and auto-draft an ADR stub the operator
must resolve.

Fail-closed: if the Codex itself can't be parsed at boot, every subsequent
check_action call raises `CodexUnavailable`. We never silently downgrade
to "no rules" — the rules failing to load IS the safety failure.
"""

from __future__ import annotations

from .exceptions import (
    CodexParseError,
    CodexUnavailable,
    CodexViolation,
)
from .models import ProposedAction, Verdict
from .registry import REGISTRY, CodexRegistry
from .resolution import (
    next_real_adr_number,
    promote_to_decisions_log,
    resolve_draft,
)
from .validator import check_action
from .watchdog import WatcherHandle, start_codex_watcher, stop_codex_watcher

__all__ = [
    "REGISTRY",
    "CodexRegistry",
    "CodexParseError",
    "CodexUnavailable",
    "CodexViolation",
    "ProposedAction",
    "Verdict",
    "WatcherHandle",
    "check_action",
    "next_real_adr_number",
    "promote_to_decisions_log",
    "resolve_draft",
    "start_codex_watcher",
    "stop_codex_watcher",
]
