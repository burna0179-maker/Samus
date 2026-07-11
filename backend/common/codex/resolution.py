"""Draft-resolution helpers for the Codex Validation Layer.

A blocked action writes `docs/codex/_drafts/ADR-NNN_<slug>.draft.md`.
The operator then chooses one of two paths:

* ``allow``  — keep the action permitted, with constraints. The draft is
  moved to `docs/codex/_resolved/` with an appended ``## Resolution``
  block. A follow-up :func:`promote_to_decisions_log` call renumbers it
  into the canonical `08_decisions_log.md`.

* ``reject`` — the operator chose to modify code instead. The draft is
  deleted from disk and a forensic record is appended to
  ``host_artifacts/codex_rejected_drafts.jsonl``.

Numbering is computed across both `08_decisions_log.md` (parsed by the
registry) AND any unmerged drafts already promoted into `_resolved/`,
so two concurrent draft promotions don't collide.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .registry import CodexRegistry, _default_codex_dir


_LOG = logging.getLogger("samus.codex.resolution")

_RESOLVED_SUBDIR = "_resolved"
_DRAFTS_SUBDIR = "_drafts"
_TEMPLATE_MARKER = "## Template for new ADRs"
_DEFAULT_ARTIFACT_ROOT = "/opt/samus/data"
_REJECTED_LEDGER_NAME = "codex_rejected_drafts.jsonl"

_ADR_ID_RE = re.compile(r"ADR-(\d{3,})", re.IGNORECASE)
_ADR_HEADER_RE = re.compile(r"^## ADR-(\d{3})\b", re.MULTILINE)
_DRAFT_FILENAME_RE = re.compile(r"^ADR-(\d{3,})_.+\.draft\.md$", re.IGNORECASE)
_RESOLVED_FILENAME_RE = re.compile(r"^ADR-(\d{3,})_.+\.md$", re.IGNORECASE)


def _artifact_root() -> str:
    return os.getenv("SAMUS_ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)


def _rejected_ledger_path() -> Path:
    root = Path(_artifact_root()) / "host_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root / _REJECTED_LEDGER_NAME


def _resolved_dir(codex_dir: Path | None = None) -> Path:
    # In containers the codex tree is read-only; route _resolved/ to the
    # writable state root if available. Caller-provided codex_dir wins.
    if codex_dir is not None:
        target = codex_dir / _RESOLVED_SUBDIR
        target.mkdir(parents=True, exist_ok=True)
        return target
    state_root = os.environ.get("SAMUS_STATE_ROOT", "").strip()
    if state_root:
        target = Path(state_root) / "codex_resolved"
        target.mkdir(parents=True, exist_ok=True)
        return target
    target = _default_codex_dir() / _RESOLVED_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def _drafts_dir(codex_dir: Path | None = None) -> Path:
    if codex_dir is not None:
        return codex_dir / _DRAFTS_SUBDIR
    # Mirror adr_drafter._default_drafts_dir resolution so the console +
    # drafter agree on where drafts live in container mode.
    from .adr_drafter import _default_drafts_dir as _drafter_default
    return _drafter_default()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract_draft_adr_number(name: str) -> int | None:
    m = _DRAFT_FILENAME_RE.match(name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def next_real_adr_number(
    registry: CodexRegistry,
    *,
    codex_dir: Path | None = None,
) -> int:
    """Highest ADR number across the registry + `_resolved/` + 1.

    `_drafts/` numbers are intentionally NOT considered: drafts get a
    provisional number at draft-time, and we want resolution to be able to
    renumber them into a contiguous canonical sequence.
    """
    highest = 0
    for adr in registry.adrs():
        m = _ADR_ID_RE.match(adr.id)
        if m:
            highest = max(highest, int(m.group(1)))
    resolved = _resolved_dir(codex_dir)
    if resolved.is_dir():
        for path in resolved.iterdir():
            if not path.is_file():
                continue
            m = _RESOLVED_FILENAME_RE.match(path.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def _append_resolution_block(
    body: str,
    *,
    decision: str,
    operator: str | None,
    rationale: str,
) -> str:
    op = operator or os.environ.get("SAMUS_OPERATOR") or "alex"
    block = (
        "\n\n## Resolution\n"
        "\n"
        f"**Decision:** {decision}\n"
        f"**Operator:** {op}\n"
        f"**Resolved at:** {_utc_iso()}\n"
        f"**Rationale:** {rationale}\n"
    )
    if body.endswith("\n"):
        return body + block.lstrip("\n")
    return body + block


def _log_rejection(draft_path: Path, rationale: str, operator: str | None) -> None:
    record = {
        "ts": _utc_iso(),
        "draft_name": draft_path.name,
        "draft_path": str(draft_path),
        "operator": operator or os.environ.get("SAMUS_OPERATOR") or "alex",
        "rationale": rationale,
    }
    ledger = _rejected_ledger_path()
    try:
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        _LOG.error(
            "samus.codex.resolution.ledger_write_failed: path=%s err=%s",
            ledger, exc,
        )


def resolve_draft(
    draft_path: Path,
    *,
    decision: Literal["allow", "reject"],
    rationale: str,
    operator: str | None = None,
    codex_dir: Path | None = None,
) -> Path:
    """Resolve a single ADR draft. Returns the post-resolution path.

    For ``allow``: returns the new `_resolved/` path.
    For ``reject``: returns the (now-deleted) original `draft_path`.
    """
    if decision not in ("allow", "reject"):
        raise ValueError(f"unknown decision: {decision!r}")
    if not rationale or not rationale.strip():
        raise ValueError("rationale is required and must be non-empty")
    if not draft_path.is_file():
        raise FileNotFoundError(f"draft not found: {draft_path}")

    if decision == "reject":
        _log_rejection(draft_path, rationale, operator)
        try:
            draft_path.unlink()
        except OSError as exc:
            raise RuntimeError(f"failed to delete draft {draft_path}: {exc}") from exc
        _LOG.info(
            "samus.codex.resolution.rejected: name=%s operator=%s",
            draft_path.name, operator,
        )
        return draft_path

    # allow path
    body = draft_path.read_text(encoding="utf-8")
    updated = _append_resolution_block(
        body, decision="ALLOW", operator=operator, rationale=rationale,
    )
    resolved_dir = _resolved_dir(codex_dir)
    new_name = draft_path.name.replace(".draft.md", ".resolved.md")
    if new_name == draft_path.name:
        # Defensive: ensure rename even if pattern didn't match.
        new_name = draft_path.stem + ".resolved.md"
    target = resolved_dir / new_name
    if target.exists():
        raise FileExistsError(f"resolved target already exists: {target}")
    # write_bytes, not write_text: keep LF on Windows (text mode would emit
    # CRLF and churn the LF-normalized _resolved/ markdown in git).
    target.write_bytes(updated.encode("utf-8"))
    try:
        draft_path.unlink()
    except OSError as exc:
        # Roll back the resolved-copy to avoid double-presence.
        try:
            target.unlink()
        except OSError:
            pass
        raise RuntimeError(f"failed to remove draft {draft_path}: {exc}") from exc
    _LOG.info(
        "samus.codex.resolution.allowed: name=%s -> %s operator=%s",
        draft_path.name, target.name, operator,
    )
    return target


def _replace_first_adr_id(body: str, new_id: str) -> str:
    """Rewrite a leading ``# ADR-NNN ...`` heading line in a draft body."""
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("# ADR-") or line.startswith("## ADR-"):
            lines[idx] = re.sub(r"ADR-\d{3,}", new_id, line, count=1)
            break
    return "\n".join(lines) + ("\n" if body.endswith("\n") else "")


def promote_to_decisions_log(
    resolved_path: Path,
    *,
    decisions_log: Path | None = None,
    registry: CodexRegistry | None = None,
    codex_dir: Path | None = None,
    title: str | None = None,
    date_iso: str | None = None,
) -> str:
    """Append the resolved ADR body into `08_decisions_log.md`.

    The body is inserted BEFORE the ``## Template for new ADRs`` section
    and renumbered to the next sequential ADR-NNN. Returns the new ADR id.
    """
    if registry is None:
        from .registry import REGISTRY as _R  # local to dodge cycles
        registry = _R
    log_path = decisions_log or (_default_codex_dir() / "08_decisions_log.md")
    if not log_path.is_file():
        raise FileNotFoundError(f"decisions log not found: {log_path}")
    if not resolved_path.is_file():
        raise FileNotFoundError(f"resolved draft not found: {resolved_path}")

    n = next_real_adr_number(registry, codex_dir=codex_dir)
    new_id = f"ADR-{n:03d}"
    body = resolved_path.read_text(encoding="utf-8")
    body = _replace_first_adr_id(body, new_id)

    log_text = log_path.read_text(encoding="utf-8")
    marker_idx = log_text.find(_TEMPLATE_MARKER)
    if marker_idx == -1:
        raise RuntimeError(
            f"{_TEMPLATE_MARKER!r} not found in {log_path} — refusing to "
            "promote (decisions log shape unexpected)",
        )

    # Build the canonical heading line for the appended ADR.
    headline_title = title or _derive_title(body) or "promoted from draft"
    when = date_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    canonical_header = f"## {new_id} | {when} | {headline_title}\n\n"

    # Strip any pre-existing top-level heading from the promoted body so we
    # don't double-stack headers; what we keep is the substantive content.
    stripped_body = _strip_leading_heading(body)

    insertion = (
        canonical_header
        + stripped_body.rstrip()
        + "\n\n---\n\n"
    )
    new_log = log_text[:marker_idx] + insertion + log_text[marker_idx:]
    # write_bytes, not write_text: keep LF on Windows so promotion doesn't flip
    # the entire LF-normalized 08_decisions_log.md to CRLF (whole-file git diff).
    log_path.write_bytes(new_log.encode("utf-8"))
    _LOG.info(
        "samus.codex.resolution.promoted: %s -> %s in %s",
        resolved_path.name, new_id, log_path.name,
    )
    return new_id


def _derive_title(body: str) -> str | None:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# ADR-") or s.startswith("## ADR-"):
            parts = s.split("—", 1)
            if len(parts) == 2:
                return parts[1].strip().rstrip(".")
            parts = s.split("|")
            if len(parts) >= 3:
                return parts[-1].strip()
    return None


def _strip_leading_heading(body: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    skipped = False
    for line in lines:
        if not skipped and (line.startswith("# ADR-") or line.startswith("## ADR-")):
            skipped = True
            continue
        out.append(line)
    return "\n".join(out)


__all__ = [
    "next_real_adr_number",
    "promote_to_decisions_log",
    "resolve_draft",
]
