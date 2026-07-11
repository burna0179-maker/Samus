"""Exception types for the Codex Validation Layer.

Three concrete failure modes:

* `CodexParseError` — a chapter on disk didn't match the expected shape.
  Always raised with chapter+section context so the operator can pinpoint
  the malformed text.
* `CodexUnavailable` — the registry couldn't load (parse failure, missing
  directory, etc.). Every subsequent check_action raises this. Fail-closed:
  we refuse to validate rather than silently pass.
* `CodexViolation` — a checked action was rejected. Carries the violated
  rule id, the human reason, and the path to the auto-drafted ADR stub.
"""

from __future__ import annotations

from pathlib import Path


class CodexParseError(ValueError):
    def __init__(self, chapter: str, section: str, detail: str) -> None:
        self.chapter = chapter
        self.section = section
        self.detail = detail
        super().__init__(f"codex parse error in {chapter} [{section}]: {detail}")


class CodexUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"codex unavailable: {reason}")


class CodexViolation(RuntimeError):
    def __init__(
        self,
        violated_rule_id: str,
        reason: str,
        drafted_adr_path: Path | None = None,
    ) -> None:
        self.violated_rule_id = violated_rule_id
        self.reason = reason
        self.drafted_adr_path = drafted_adr_path
        suffix = f" (ADR draft: {drafted_adr_path})" if drafted_adr_path else ""
        super().__init__(f"codex violation [{violated_rule_id}]: {reason}{suffix}")


__all__ = [
    "CodexParseError",
    "CodexUnavailable",
    "CodexViolation",
]
