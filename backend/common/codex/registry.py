"""Module-level Codex registry singleton.

`load()` is called once at boot. On parse failure the registry latches
into a permanently-failed state — subsequent calls raise CodexUnavailable
without retrying the parse. `reload()` clears the latch and re-parses
(admin endpoint use).

`search_decisions(query, k)` layers a lightweight keyword+recency scorer on
top of the parsed ADR corpus and the resolved-ADR drafts under
`docs/codex/_resolved/`. It exists so callers (belief_ledger.query_precedent,
intelligence_cycle's REASON-stage seam) can ask "has this been decided
before?" without pulling in embeddings or an LLM. Local-first: pure Python,
runs in the intelligence cycle inner loop.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .exceptions import CodexParseError, CodexUnavailable
from .models import ADR, BannedPhrase, Guardrail, ShutdownSignal
from .parser import ParsedCodex, parse_codex


# Recency decay: an ADR/resolved doc from N days ago is worth exp(-N/half_life)
# of the same match score today. 180 days chosen so the last two quarters
# dominate but older canon still surfaces on a strong keyword hit.
_RECENCY_HALF_LIFE_DAYS = 180.0
# Words below this length or on the stop-word list are ignored for scoring —
# they'd dominate frequency counts without carrying meaning.
_MIN_TOKEN_LEN = 3
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "not",
        "but",
        "are",
        "was",
        "has",
        "had",
        "have",
        "from",
        "into",
        "onto",
        "off",
        "out",
        "all",
        "any",
        "can",
        "may",
        "will",
        "shall",
        "should",
        "would",
        "could",
        "does",
        "did",
        "been",
        "being",
        "its",
        "our",
        "your",
        "their",
        "his",
        "her",
        "him",
        "she",
        "they",
        "them",
        "who",
        "whom",
        "what",
        "which",
        "when",
        "where",
        "why",
        "how",
        "then",
        "than",
        "too",
        "also",
        "such",
        "some",
        "each",
        "one",
        "two",
        "new",
        "old",
        "yes",
        "you",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class DecisionMatch:
    """One scored decision surfaced by :meth:`CodexRegistry.search_decisions`.

    ``source`` is ``"adr"`` for a numbered ADR in the decisions log and
    ``"resolved"`` for a resolved-draft file. ``adr_id`` is the canonical
    ADR-### id when known (both sources may carry it).
    """

    adr_id: str
    title: str
    decision: str
    date_iso: str
    source: str
    path: str
    score: float
    superseded_by: str | None = None
    keyword_hits: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Scoring helpers (module-private).
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [
        m.group(0)
        for m in _TOKEN_RE.finditer(text.lower())
        if len(m.group(0)) >= _MIN_TOKEN_LEN and m.group(0) not in _STOPWORDS
    ]


def _recency_multiplier(iso_date: str, *, _now: datetime | None = None) -> float:
    if not iso_date:
        return 0.5  # unknown date — neither boost nor bury
    try:
        parsed = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(
                date.fromisoformat(iso_date[:10]), datetime.min.time(), tzinfo=timezone.utc
            )
        except ValueError:
            return 0.5
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = _now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - parsed).total_seconds() / 86400.0)
    # Exponential decay: 1.0 today, ~0.5 at 180 days, ~0.25 at 360 days.
    import math

    return math.exp(-age_days / _RECENCY_HALF_LIFE_DAYS)


def _score_document(
    query_tokens: list[str],
    *,
    title: str,
    body: str,
    filename: str,
    date_iso: str,
    now: datetime | None = None,
) -> tuple[float, tuple[str, ...]]:
    """Keyword+title+filename+recency scorer. Returns (score, matched_tokens)."""
    if not query_tokens:
        return 0.0, ()
    title_tokens = set(_tokenize(title))
    filename_tokens = set(_tokenize(filename))
    body_tokens = _tokenize(body)
    body_counts: dict[str, int] = {}
    for t in body_tokens:
        body_counts[t] = body_counts.get(t, 0) + 1

    score = 0.0
    hits: list[str] = []
    for q in query_tokens:
        got = False
        if q in title_tokens:
            score += 4.0
            got = True
        if q in filename_tokens:
            score += 3.0
            got = True
        if q in body_counts:
            # log-dampen frequency so a spammy body word doesn't crush a
            # short high-precision one.
            import math

            score += 1.0 + math.log1p(body_counts[q])
            got = True
        if got:
            hits.append(q)
    if not hits:
        return 0.0, ()
    score *= _recency_multiplier(date_iso, _now=now)
    return score, tuple(hits)


def _default_codex_dir() -> Path:
    # backend/common/codex/registry.py -> Samus repo root is 4 parents up.
    return Path(__file__).resolve().parents[3] / "docs" / "codex"


class CodexRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._parsed: ParsedCodex | None = None
        self._failed_to_load = False
        self._failure_reason: str | None = None

    def load(self, codex_dir: Path | None = None) -> None:
        directory = codex_dir or _default_codex_dir()
        with self._lock:
            try:
                self._parsed = parse_codex(directory)
                self._failed_to_load = False
                self._failure_reason = None
            except CodexParseError as exc:
                self._parsed = None
                self._failed_to_load = True
                self._failure_reason = str(exc)
                raise CodexUnavailable(self._failure_reason) from exc

    def reload(self, codex_dir: Path | None = None) -> None:
        # Hot-reload is fail-OPEN: a parse error preserves the previously
        # loaded registry rather than yanking the rules out from under
        # callers mid-edit. Boot-time load() is still fail-CLOSED.
        directory = codex_dir or _default_codex_dir()
        try:
            new_parsed = parse_codex(directory)
        except CodexParseError as exc:
            raise CodexUnavailable(f"reload parse failed: {exc}") from exc
        with self._lock:
            self._parsed = new_parsed
            self._failed_to_load = False
            self._failure_reason = None

    def is_loaded(self) -> bool:
        return self._parsed is not None and not self._failed_to_load

    def _require(self) -> ParsedCodex:
        if self._failed_to_load:
            raise CodexUnavailable(self._failure_reason or "previous load failed")
        if self._parsed is None:
            raise CodexUnavailable("registry not loaded; call CodexRegistry.load()")
        return self._parsed

    def guardrails(self) -> list[Guardrail]:
        return list(self._require().guardrails)

    def enforced_guardrails(self) -> list[Guardrail]:
        return [g for g in self._require().guardrails if g.status == "enforced"]

    def intent_guardrails(self) -> list[Guardrail]:
        return [g for g in self._require().guardrails if g.status == "intent"]

    def adrs(self) -> list[ADR]:
        return list(self._require().adrs)

    def adr(self, adr_id: str) -> ADR | None:
        for entry in self._require().adrs:
            if entry.id == adr_id:
                return entry
        return None

    def banned_phrases(self) -> list[str]:
        return [bp.phrase for bp in self._require().banned_phrases]

    def banned_phrase_records(self) -> list[BannedPhrase]:
        return list(self._require().banned_phrases)

    def shutdown_signals(self) -> list[ShutdownSignal]:
        return list(self._require().shutdown_signals)

    def glossary(self) -> dict[str, str]:
        return dict(self._require().glossary_terms)

    # ------------------------------------------------------------------
    # Precedent search (Concept 1 — institutional memory)
    # ------------------------------------------------------------------

    def search_decisions(
        self,
        query: str,
        k: int = 5,
        *,
        codex_dir: Path | None = None,
        _now: datetime | None = None,
    ) -> list[DecisionMatch]:
        """Return the top-``k`` past decisions most relevant to ``query``.

        Scoring is deliberately local-first: keyword hits on title (x4),
        filename (x3), body (log-dampened frequency), then multiplied by an
        exponential recency decay so the last two quarters of decisions
        dominate. No embeddings, no LLM — safe to call from the inner
        reasoning loop.

        Sources:
          1. Numbered ADRs parsed from ``08_decisions_log.md`` (canonical).
          2. Resolved ADR drafts under ``docs/codex/_resolved/*.md``
             (operator-authored resolutions that never made it into the log).

        Fail-soft: returns ``[]`` on an empty query, on registry-not-loaded,
        or on any I/O error walking the resolved-drafts directory.
        """
        if not query or not query.strip():
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        matches: list[DecisionMatch] = []

        # Source 1 — parsed ADRs from the decisions log.
        try:
            adrs = self._require().adrs
        except CodexUnavailable:
            adrs = []
        log_path = str((codex_dir or _default_codex_dir()) / "08_decisions_log.md")
        for adr in adrs:
            score, hits = _score_document(
                query_tokens,
                title=adr.title,
                body=adr.decision,
                filename=adr.id,
                date_iso=adr.date,
                now=_now,
            )
            if score <= 0.0:
                continue
            matches.append(
                DecisionMatch(
                    adr_id=adr.id,
                    title=adr.title,
                    decision=adr.decision,
                    date_iso=adr.date,
                    source="adr",
                    path=log_path,
                    score=round(score, 4),
                    superseded_by=adr.superseded_by,
                    keyword_hits=hits,
                )
            )

        # Source 2 — resolved-draft files. These are operator-authored
        # resolutions of blocked-action drafts; they carry ADR-### in the
        # filename and a raw decision blob in the body.
        resolved_dir = (codex_dir or _default_codex_dir()) / "_resolved"
        try:
            if resolved_dir.is_dir():
                for path in sorted(resolved_dir.glob("*.md")):
                    matches.extend(_score_resolved_file(path, query_tokens, now=_now))
        except OSError:
            # I/O error walking the resolved corpus is not fatal — the ADR
            # log source above still yields results.
            pass

        matches.sort(key=lambda m: (-m.score, m.adr_id))
        return matches[: max(0, int(k))]


def _score_resolved_file(
    path: Path,
    query_tokens: list[str],
    *,
    now: datetime | None = None,
) -> list[DecisionMatch]:
    """Score one `_resolved/*.md` file. Returns 0 or 1 matches."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Filename convention: ADR-###_<slug>.resolved.md OR <slug>.resolved.md.
    adr_match = re.match(r"^(ADR-\d{3,})[_.-]?(.*?)\.resolved\.md$", path.name)
    if adr_match:
        adr_id = adr_match.group(1)
        # Slug carries the action-kind that was blocked; useful in the title.
        slug = adr_match.group(2).replace("_", " ").replace("-", " ").strip()
    else:
        adr_id = ""
        slug = path.stem.replace("_", " ").replace("-", " ").strip()

    # Title: first H1, else filename slug.
    title = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            break
    if not title:
        title = slug or path.name

    # Date: first "**Date:** ..." blob or fall back to the file mtime.
    date_iso = ""
    m = re.search(r"\*\*Date:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
    if m:
        date_iso = m.group(1)
    else:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            date_iso = mtime.date().isoformat()
        except OSError:
            date_iso = ""

    score, hits = _score_document(
        query_tokens,
        title=title,
        body=text,
        filename=path.name,
        date_iso=date_iso,
        now=now,
    )
    if score <= 0.0:
        return []
    # Body is often big; return a short decision blurb (first paragraph)
    # rather than the whole file.
    body_lines = [
        ln
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith(("#", ">", "**", "-"))
    ]
    decision_blurb = " ".join(body_lines[:3])[:400]
    return [
        DecisionMatch(
            adr_id=adr_id,
            title=title[:200],
            decision=decision_blurb,
            date_iso=date_iso,
            source="resolved",
            path=str(path),
            score=round(score, 4),
            superseded_by=None,
            keyword_hits=hits,
        )
    ]


REGISTRY = CodexRegistry()


def search_decisions(
    query: str,
    k: int = 5,
    *,
    codex_dir: Path | None = None,
) -> list[DecisionMatch]:
    """Module-level wrapper around :meth:`CodexRegistry.search_decisions`.

    Uses the module-level singleton. Loads the registry lazily if it hasn't
    been loaded yet — an unloaded registry is a common boot-order case in
    the intelligence cycle inner loop where the registry may not have been
    warmed up. Fail-soft on load failure.
    """
    if not REGISTRY.is_loaded():
        try:
            REGISTRY.load(codex_dir)
        except CodexUnavailable:
            # Fail-soft: resolved-drafts corpus is still searchable even
            # when the ADR log parse failed, so build a scratch registry
            # and let it fall through the ADR source empty-handed.
            pass
    return REGISTRY.search_decisions(query, k, codex_dir=codex_dir)


__all__ = [
    "REGISTRY",
    "CodexRegistry",
    "DecisionMatch",
    "search_decisions",
]
