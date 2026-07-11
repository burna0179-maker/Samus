"""Samus PDC sandbox-participant adapter package.

Public entrypoint: :func:`run_sandbox_participation` — drives Samus's real
governance mechanisms (EFH, ISV scope consumer, hash-chained audit ledger)
against the Darwin PDC runner's sandbox injections and writes only run-scoped
sandbox observable records. No-op in normal production boot (only acts when an
injection directory for the given run_id exists).
"""

from __future__ import annotations

from .adapter import SANDBOX_ENV_MARKER, run_sandbox_participation

__all__ = ["run_sandbox_participation", "SANDBOX_ENV_MARKER"]
