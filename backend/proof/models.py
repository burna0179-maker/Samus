"""Dataclasses for proof assets. Stdlib-only."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CaseStudyInput:
    """The raw outcome data a case study is built from — typically mapped from a
    closed-won opportunity + its prospect/artifacts."""

    company: str
    industry: str = ""
    size: str = ""  # e.g. "12-person agency"
    challenge: str = ""  # the problem in the customer's words
    tried_before: str = ""  # what failed before Samus
    solution_used: str = ""  # the specific features / workflow used
    results: list[str] = field(default_factory=list)  # concrete metric strings
    quote: str = ""
    quote_author: str = ""
    quote_title: str = ""
    url: str = ""


@dataclass
class CaseStudy:
    """A finished case study, ready to publish."""

    title: str
    company: str
    industry: str = ""
    challenge: str = ""
    tried_before: str = ""
    solution_used: str = ""
    results: list[str] = field(default_factory=list)
    quote: str = ""
    quote_author: str = ""
    quote_title: str = ""
    narrative: str = ""  # connective prose (LLM-polished or templated)
    markdown: str = ""
    schema_jsonld: dict = field(default_factory=dict)
    used_llm: bool = False


@dataclass
class ProofPoint:
    """A short, atomic piece of social proof for the wall-of-love feed."""

    company: str
    result: str  # one-line headline metric/outcome
    quote: str = ""
    author: str = ""
    industry: str = ""


@dataclass
class ProofWall:
    """Aggregated social proof + simple roll-up stats."""

    points: list[ProofPoint] = field(default_factory=list)
    count: int = 0
    industries: list[str] = field(default_factory=list)
