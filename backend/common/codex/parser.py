"""Codex chapter parser.

Regex-driven and forgiving of whitespace variation, strict on section
presence. Missing expected chapters or sections raise CodexParseError —
the registry catches and re-raises as CodexUnavailable (fail-closed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .exceptions import CodexParseError
from .models import ADR, BannedPhrase, Guardrail, ShutdownSignal


_GUARDRAILS_FILE = "04_guardrails.md"
_ADR_FILE = "08_decisions_log.md"
_GLOSSARY_FILE = "10_glossary.md"
_SHUTDOWN_FILE = "11_when_to_shut_it_down.md"


_GUARDRAIL_HEADER_RE = re.compile(
    r"^#{2,3} G(\d+)\s+[—–\-]\s+(.+?)\s*$",
    re.MULTILINE,
)
_ADR_HEADER_RE = re.compile(
    r"^## ADR-(\d{3,})\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*$",
    re.MULTILINE,
)
_DECISION_BODY_RE = re.compile(
    r"\*\*Decision[^*]*:\*\*\s*(.+?)(?=\n\n\*\*|\Z)",
    re.DOTALL,
)
_SUPERSEDED_RE = re.compile(
    r"\*\*Superseded by:\*\*\s*(.+?)\s*(?:\.|\n|$)",
)
_WHERE_LINE_RE = re.compile(r"\*\*Where:\*\*\s*(.+?)(?=\n\n|\Z)", re.DOTALL)
_WHAT_STOPS_RE = re.compile(r"\*\*What it stops:\*\*\s*(.+?)(?=\n\n|\Z)", re.DOTALL)
_WHAT_SHOULD_STOP_RE = re.compile(
    r"\*\*What it should stop:\*\*\s*(.+?)(?=\n\n|\Z)", re.DOTALL,
)
_INTENT_MARKERS = (
    "currently intent-only",
    "intent-only",
    "not yet built",
    "not yet enforced",
    "status: intent",
    "intent declared",
    "intent-defined, not yet enforced",
    "intent-defined, not yet built",
)
_SHUTDOWN_HEADER_RE = re.compile(r"^### (\d+)\.\s+(.+?)\s*$", re.MULTILINE)
_SHUTDOWN_SIGNAL_RE = re.compile(
    r"\*\*Signal:\*\*\s*(.+?)(?=\n\n|\Z)", re.DOTALL,
)
_SHUTDOWN_ACTION_RE = re.compile(
    r"\*\*Action:\*\*\s*(.+?)(?=\n\n|\Z)", re.DOTALL,
)
_GLOSSARY_TERM_RE = re.compile(
    r"^\*\*([^*]+?)\*\*\s+[—-]\s+(.+?)(?=\n\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class ParsedCodex:
    guardrails: list[Guardrail] = field(default_factory=list)
    adrs: list[ADR] = field(default_factory=list)
    shutdown_signals: list[ShutdownSignal] = field(default_factory=list)
    glossary_terms: dict[str, str] = field(default_factory=dict)
    banned_phrases: list[BannedPhrase] = field(default_factory=list)


def _read(codex_dir: Path, name: str) -> str:
    path = codex_dir / name
    if not path.is_file():
        raise CodexParseError(name, "(file)", f"chapter not found at {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexParseError(name, "(file)", f"unreadable: {exc}") from exc


def _extract_block(text: str, start: int, next_start: int | None) -> str:
    end = next_start if next_start is not None else len(text)
    return text[start:end]


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1).strip())


def _parse_where(block: str) -> tuple[str, ...]:
    raw = _first_group(_WHERE_LINE_RE, block)
    if not raw:
        return ()
    parts: list[str] = []
    for token in re.split(r"[,;]", raw):
        token = token.strip().strip("`")
        token = re.sub(r"^\*+|\*+$", "", token).strip()
        if token and token.lower() not in {"intended", "conceptual"}:
            parts.append(token)
    return tuple(parts)


def _detect_status(block: str) -> str:
    lowered = block.lower()
    for marker in _INTENT_MARKERS:
        if marker in lowered:
            return "intent"
    return "enforced"


def _parse_guardrails(text: str) -> list[Guardrail]:
    headers = list(_GUARDRAIL_HEADER_RE.finditer(text))
    if not headers:
        raise CodexParseError(_GUARDRAILS_FILE, "(headers)", "no G# headers found")
    out: list[Guardrail] = []
    for i, m in enumerate(headers):
        next_start = headers[i + 1].start() if i + 1 < len(headers) else None
        block = _extract_block(text, m.start(), next_start)
        gid = f"G{int(m.group(1))}"
        title = m.group(2).strip()
        status = _detect_status(block)
        what_stops = _first_group(_WHAT_STOPS_RE, block) or _first_group(
            _WHAT_SHOULD_STOP_RE, block,
        ) or ""
        if not what_stops:
            raise CodexParseError(
                _GUARDRAILS_FILE, gid,
                "missing both '**What it stops:**' and '**What it should stop:**'",
            )
        out.append(
            Guardrail(
                id=gid,
                title=title,
                status=status,
                description=what_stops,
                where=_parse_where(block),
                what_it_stops=what_stops,
                bypass_forbidden=True,
            )
        )
    return out


def _parse_adrs(text: str) -> list[ADR]:
    headers = list(_ADR_HEADER_RE.finditer(text))
    if not headers:
        raise CodexParseError(_ADR_FILE, "(headers)", "no ADR-### headers found")
    out: list[ADR] = []
    for i, m in enumerate(headers):
        next_start = headers[i + 1].start() if i + 1 < len(headers) else None
        block = _extract_block(text, m.start(), next_start)
        adr_id = f"ADR-{m.group(1)}"
        date = m.group(2)
        title = m.group(3).strip()
        decision = _first_group(_DECISION_BODY_RE, block)
        if not decision:
            raise CodexParseError(
                _ADR_FILE, adr_id, "missing '**Decision:**' body",
            )
        superseded_raw = _first_group(_SUPERSEDED_RE, block)
        superseded_by: str | None = None
        if superseded_raw and superseded_raw.lower() not in {"none", "n/a", "—", "-"}:
            sm = re.match(r"(ADR-\d{3})", superseded_raw)
            if sm:
                superseded_by = sm.group(1)
        out.append(
            ADR(
                id=adr_id,
                date=date,
                title=title,
                decision=decision,
                superseded_by=superseded_by,
            )
        )
    return out


def _parse_shutdown_signals(text: str) -> list[ShutdownSignal]:
    section_start = text.find("## Three reasons to shut Samus down")
    if section_start == -1:
        raise CodexParseError(
            _SHUTDOWN_FILE, "(section)",
            "missing '## Three reasons to shut Samus down' section",
        )
    section_end = text.find("\n## ", section_start + 1)
    if section_end == -1:
        section_end = len(text)
    section = text[section_start:section_end]
    headers = list(_SHUTDOWN_HEADER_RE.finditer(section))
    if len(headers) < 3:
        raise CodexParseError(
            _SHUTDOWN_FILE, "(signals)",
            f"expected 3 numbered shutdown signals, found {len(headers)}",
        )
    out: list[ShutdownSignal] = []
    for i, m in enumerate(headers[:3]):
        next_start = headers[i + 1].start() if i + 1 < len(headers) else len(section)
        block = section[m.start():next_start]
        description = _first_group(_SHUTDOWN_SIGNAL_RE, block) or m.group(2).strip()
        action = _first_group(_SHUTDOWN_ACTION_RE, block) or ""
        if not action:
            raise CodexParseError(
                _SHUTDOWN_FILE, f"S{i + 1}",
                "missing '**Action:**' line under shutdown signal",
            )
        out.append(
            ShutdownSignal(
                id=f"S{i + 1}",
                signal_description=description,
                action=action,
            )
        )
    return out


def _parse_glossary(text: str) -> dict[str, str]:
    terms: dict[str, str] = {}
    for m in _GLOSSARY_TERM_RE.finditer(text):
        term = m.group(1).strip()
        definition = re.sub(r"\s+", " ", m.group(2).strip())
        if term and term not in terms:
            terms[term] = definition
    if not terms:
        raise CodexParseError(_GLOSSARY_FILE, "(terms)", "no glossary terms parsed")
    return terms


def _load_banned_phrases() -> list[BannedPhrase]:
    from backend.common.stake_sentence_guard import STAKE_SENTENCE_BANNED_PHRASES

    source = "backend/common/stake_sentence_guard.py:STAKE_SENTENCE_BANNED_PHRASES"
    return [BannedPhrase(phrase=p, source=source) for p in STAKE_SENTENCE_BANNED_PHRASES]


def parse_codex(codex_dir: Path) -> ParsedCodex:
    if not codex_dir.is_dir():
        raise CodexParseError(
            "(directory)", "(root)", f"codex directory not found: {codex_dir}",
        )
    guardrails_text = _read(codex_dir, _GUARDRAILS_FILE)
    adr_text = _read(codex_dir, _ADR_FILE)
    shutdown_text = _read(codex_dir, _SHUTDOWN_FILE)
    glossary_text = _read(codex_dir, _GLOSSARY_FILE)

    return ParsedCodex(
        guardrails=_parse_guardrails(guardrails_text),
        adrs=_parse_adrs(adr_text),
        shutdown_signals=_parse_shutdown_signals(shutdown_text),
        glossary_terms=_parse_glossary(glossary_text),
        banned_phrases=_load_banned_phrases(),
    )


__all__ = [
    "ParsedCodex",
    "parse_codex",
]
