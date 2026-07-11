"""Long-form blog / article generator structured for AI citation (GEO).

Generates 1500-2500 word blog posts with the structural signals that make
AI systems (ChatGPT, Claude, Perplexity, Google AI Overview) cite content:

  - 40-60 word "golden answer" blocks at the start of every major section
  - Question-formatted H2 headings ("How does X work?" not "X Explained")
  - FAQ section (minimum 6 questions, 40-60 word answers each)
  - Statistics attribution markers (at least 2 per 300 words)
  - Author byline + datePublished / dateModified metadata in output

Structure: intro (200w) -> 4 main sections (300w ea) -> FAQ (300w) -> CTA (150w)

LLM path: Anthropic claude-haiku, 1200 token budget, JSON-only.
Fallback: deterministic template built from keywords + topic. Always
  returns a fully-formed BlogPost regardless of LLM availability.

ASCII-only output (Windows-safe).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.common.llm_client import (
    BudgetExceeded,
    LlmCallError,
    anthropic_messages,
    record_outcome,
)
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.seo.blog_gen")

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
_BUDGET_WORKCELL = "seo"
_BLOG_MAX_TOKENS = 3500
_BLOG_TIMEOUT_S = 120.0  # default 30s is too short for max_tokens=3500 completions

_BLOG_FIELDS = (
    "title",
    "intro",
    "sections",   # list of {heading, golden_answer, body, stat_markers}
    "faq",        # list of {q, a}
    "cta",
    "author",
    "date_published",
    "date_modified",
)

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


@dataclass
class BlogSection:
    """One H2 section of the blog post."""
    heading: str          # question-formatted: "How does X work?"
    golden_answer: str    # 40-60 word answer block for AI extraction
    body: str             # 240-260 word expansion
    stat_markers: list[str] = field(default_factory=list)  # e.g. ["[STAT:source]"]


@dataclass
class BlogFaqItem:
    """A single FAQ question + 40-60 word answer."""
    question: str
    answer: str


@dataclass
class BlogPost:
    """Fully-formed blog post ready for rendering / schema injection."""
    title: str
    intro: str               # 200w introduction
    sections: list[BlogSection]
    faq: list[BlogFaqItem]
    cta: str                 # 150w conclusion + CTA
    author: str
    date_published: str      # ISO-8601 date
    date_modified: str       # ISO-8601 date (same as published on first publish)
    used_llm: bool = False
    word_count: int = 0
    llm_cost_usd: float = 0.0
    ts: str = ""


# ---------------------------------------------------------------------------
# Word-count helper
# ---------------------------------------------------------------------------


def _wc(text: str) -> int:
    return len(text.split()) if text else 0


def _total_words(post: BlogPost) -> int:
    total = _wc(post.intro) + _wc(post.cta)
    for s in post.sections:
        total += _wc(s.heading) + _wc(s.golden_answer) + _wc(s.body)
    for f in post.faq:
        total += _wc(f.question) + _wc(f.answer)
    return total


# ---------------------------------------------------------------------------
# Deterministic template fallback
# ---------------------------------------------------------------------------

_STAT_MARKERS = [
    "[STAT: industry research]",
    "[STAT: survey data]",
    "[STAT: case study]",
    "[STAT: market report]",
]


def _template_sections(primary_kw: str, secondary_kws: list[str]) -> list[BlogSection]:
    kw2 = secondary_kws[0] if secondary_kws else primary_kw
    kw3 = secondary_kws[1] if len(secondary_kws) > 1 else primary_kw
    kw4 = secondary_kws[2] if len(secondary_kws) > 2 else primary_kw
    return [
        BlogSection(
            heading=f"What is {primary_kw} and why does it matter?",
            golden_answer=(
                f"{primary_kw.title()} is the process of optimizing your online presence "
                f"so that AI-powered search engines and citation engines include your "
                f"business in their answers. It extends traditional SEO by structuring "
                f"content for direct AI extraction, not just keyword ranking."
            ),
            body=(
                f"Businesses that invest in {primary_kw} gain a meaningful edge as "
                f"consumers increasingly rely on AI assistants for research. "
                f"{_STAT_MARKERS[0]}, a growing share of purchase decisions start "
                f"with an AI-generated overview rather than a list of blue links. "
                f"When your content is structured to answer specific questions "
                f"directly, AI systems treat it as a reliable reference. "
                f"This means your brand name, service description, and contact details "
                f"appear inside the answer rather than below it. "
                f"The result is zero-click visibility that builds authority even when "
                f"the user never visits your site. {_STAT_MARKERS[1]}, businesses "
                f"with structured FAQ schema see measurably higher AI citation rates "
                f"than those relying on unstructured long-form copy alone. "
                f"Pairing {primary_kw} with strong {kw2} practices creates a "
                f"compounding effect on both AI citation and traditional search ranking."
            ),
            stat_markers=[_STAT_MARKERS[0], _STAT_MARKERS[1]],
        ),
        BlogSection(
            heading=f"How does {kw2} improve your search visibility?",
            golden_answer=(
                f"{kw2.title()} improves search visibility by giving AI crawlers "
                f"machine-readable signals about your business type, location, and "
                f"expertise. Structured data, question-formatted headings, and direct "
                f"answer blocks each raise the probability that your content appears "
                f"in AI-generated responses."
            ),
            body=(
                f"The technical backbone of effective {kw2} is schema markup. "
                f"FAQPage, Article, and LocalBusiness JSON-LD schemas tell AI "
                f"systems exactly what type of content they are reading and who "
                f"produced it. {_STAT_MARKERS[2]}, pages with FAQPage schema "
                f"are cited in AI Overviews at a significantly higher rate than "
                f"pages without it. Beyond schema, the format of your headings "
                f"matters: AI systems extract answers from H2 sections that are "
                f"phrased as questions because they mirror the exact phrasing of "
                f"user queries. A heading like 'How much does {kw2} cost?' is "
                f"extracted more reliably than 'Pricing Information.' "
                f"Internal linking also plays a role. Pages that link to each "
                f"other around a shared topic cluster signal topical authority, "
                f"which raises the likelihood of any page in the cluster being "
                f"cited when the topic is queried."
            ),
            stat_markers=[_STAT_MARKERS[2]],
        ),
        BlogSection(
            heading=f"What are the most common {kw3} mistakes to avoid?",
            golden_answer=(
                f"The most common {kw3} mistakes are: blocking AI crawlers in "
                f"robots.txt, publishing content without FAQPage schema, omitting "
                f"dateModified markup, and writing section headings as labels rather "
                f"than questions. Each blocks AI systems from extracting and citing "
                f"your content reliably."
            ),
            body=(
                f"Many businesses unknowingly block AI search bots in their "
                f"robots.txt file. OAI-SearchBot (OpenAI), PerplexityBot, "
                f"Claude-SearchBot, and ChatGPT-User are distinct user-agent strings "
                f"that must each be explicitly allowed. A blanket Disallow or an "
                f"overly aggressive WAF rule can exclude your entire site from "
                f"AI citation without triggering any traditional SEO alarm. "
                f"The second major mistake is publishing content without a visible "
                f"'Last Updated' date and without dateModified in your Article "
                f"schema. AI systems weight freshness: {_STAT_MARKERS[3]}, "
                f"content updated within the past 6 months is cited more often "
                f"than content with no modification date. "
                f"Finally, writing headings as topic labels ('Our {kw3} Approach') "
                f"rather than questions ('How does our {kw3} approach work?') "
                f"reduces extractability because the AI cannot match a label to "
                f"a user's question phrasing."
            ),
            stat_markers=[_STAT_MARKERS[3]],
        ),
        BlogSection(
            heading=f"How do you build a {kw4} strategy that compounds over time?",
            golden_answer=(
                f"A compounding {kw4} strategy publishes a new question-answering "
                f"article every 2 weeks on a single topic cluster, cross-links all "
                f"articles, adds FAQPage schema to each, and refreshes published "
                f"dates quarterly. This builds topical authority that AI systems "
                f"recognize and cite consistently."
            ),
            body=(
                f"Topical authority is the single highest-leverage variable in "
                f"long-term {kw4} performance. An AI system that has seen 10 of "
                f"your articles on the same subject will cite you for that subject "
                f"even on queries you have not directly targeted. "
                f"The practical formula: pick one cluster (e.g. '{kw4} for local "
                f"service businesses'), map 10 question-formatted article titles, "
                f"publish two per month, and link every article to every other "
                f"article in the cluster. "
                f"Pair each article with a LinkedIn post that quotes the golden "
                f"answer block verbatim. This creates a secondary citation signal: "
                f"AI systems trained on LinkedIn content encounter your exact phrasing "
                f"multiple times, reinforcing the association between your brand and "
                f"the topic. "
                f"Refresh each article's dateModified at least quarterly, even if "
                f"only a statistic or example changes. Freshness signals compound "
                f"with topical authority to create a durable citation advantage."
            ),
            stat_markers=[],
        ),
    ]


def _template_faq(primary_kw: str) -> list[BlogFaqItem]:
    return [
        BlogFaqItem(
            question=f"What is {primary_kw}?",
            answer=(
                f"{primary_kw.title()} is the practice of structuring digital content so "
                f"that AI-powered search engines and citation systems surface it in "
                f"their generated answers. It extends traditional SEO with question-"
                f"formatted headings, FAQPage schema, and direct answer blocks."
            ),
        ),
        BlogFaqItem(
            question=f"How long does {primary_kw} take to show results?",
            answer=(
                f"Most businesses see their first AI citation appearances within 4-8 "
                f"weeks of adding FAQPage schema and restructuring key pages. Compounding "
                f"topical authority, which drives consistent citation across many "
                f"queries, typically takes 3-6 months of steady content publishing."
            ),
        ),
        BlogFaqItem(
            question=f"Does {primary_kw} replace traditional SEO?",
            answer=(
                f"No. {primary_kw.title()} extends traditional SEO rather than replacing it. "
                f"Google, Bing, and AI-native engines all use overlapping signals. "
                f"The structural changes that help AI citation (schema, question headings, "
                f"fresh dates) also improve traditional organic rankings simultaneously."
            ),
        ),
        BlogFaqItem(
            question=f"Which AI search bots should I allow in robots.txt?",
            answer=(
                f"Allow OAI-SearchBot, ChatGPT-User, PerplexityBot, Claude-SearchBot, "
                f"and Claude-User. Each represents a major AI citation engine. A blanket "
                f"Disallow or an overly restrictive WAF rule blocks all of them and "
                f"removes your site from AI-generated answers entirely."
            ),
        ),
        BlogFaqItem(
            question=f"What schema types matter most for {primary_kw}?",
            answer=(
                f"FAQPage schema is the highest-impact addition for most small business "
                f"sites because it maps directly to question-and-answer queries. Article "
                f"schema with datePublished and dateModified adds freshness signals. "
                f"LocalBusiness schema adds geographic relevance for location-based queries."
            ),
        ),
        BlogFaqItem(
            question=f"How often should I update my content for {primary_kw}?",
            answer=(
                f"Update your most important pages at least quarterly: refresh a "
                f"statistic, add a new FAQ, or expand a section. Update the dateModified "
                f"field in your Article schema on each pass. AI systems treat recently "
                f"modified content as more authoritative than pages with stale or "
                f"missing modification dates."
            ),
        ),
    ]


def _build_template_post(
    topic: str,
    primary_kw: str,
    secondary_kws: list[str],
    author: str,
    date_str: str,
) -> BlogPost:
    title = f"How to Use {primary_kw.title()} to Get Your Business Cited by AI"
    intro = (
        f"AI-powered search engines now answer millions of queries every day without "
        f"sending users to a website. If your business is not being cited in those "
        f"answers, you are invisible to a growing share of potential customers. "
        f"This guide explains exactly what {primary_kw} is, which technical changes "
        f"have the highest impact, and how to build a content strategy that compounds "
        f"over time. Every recommendation is actionable by a small team in under "
        f"two weeks. No paid tools required."
    )
    sections = _template_sections(primary_kw, secondary_kws)
    faq = _template_faq(primary_kw)
    cta = (
        f"Getting cited by AI search engines is no longer optional for service "
        f"businesses competing online. The businesses that structure their content "
        f"correctly today will hold durable citation advantages as AI search grows. "
        f"Start with three changes: allow AI crawlers in robots.txt, add FAQPage "
        f"schema to your top pages, and reformat your H2 headings as questions. "
        f"If you want a full {primary_kw} audit and a step-by-step implementation "
        f"plan for your site, reach out today. We deliver a complete GEO readiness "
        f"report and a prioritized fix list within 48 hours."
    )
    post = BlogPost(
        title=title,
        intro=intro,
        sections=sections,
        faq=faq,
        cta=cta,
        author=author or "Hustleforge Editorial",
        date_published=date_str,
        date_modified=date_str,
        ts=iso_now(),
    )
    post.word_count = _total_words(post)
    return post


# ---------------------------------------------------------------------------
# LLM prompt + parser
# ---------------------------------------------------------------------------

_BLOG_SYSTEM = (
    "You are a deterministic GEO content module. Produce JSON-only output. "
    "Top-level keys: title (str), intro (str, ~200 words), "
    "sections (list of 4 objects: heading str, golden_answer str 40-60 words, "
    "body str ~270 words, stat_markers list of str), "
    "faq (list of 6 objects: q str, a str 40-60 words), "
    "cta (str, ~150 words), author (str), date_published (str ISO date), "
    "date_modified (str ISO date).\n\n"
    "Rules:\n"
    "- All section headings must be question-formatted (start with How/What/Why/Which/When/Where)\n"
    "- golden_answer: 40-60 words, complete standalone answer, no markdown\n"
    "- FAQ answers: 40-60 words each, complete standalone answers\n"
    "- ASCII only, no em-dashes, no unicode bullets, use plain hyphens\n"
    "- No markdown formatting in strings, no surrounding prose\n"
    "- HARD RULE - no invented numbers: ALL specific numeric claims "
    "(percentages, dollar amounts, time durations, counts, multipliers, "
    "frequencies, dates, ROI figures, statistics) MUST be expressed as "
    "[STAT: description of what would be cited here] placeholders, NOT as "
    "literal numbers. Wrong: '20-30 percent productivity gains'. Right: "
    "'[STAT: SMB productivity gain measurement] productivity gains'. Apply "
    "this to intro, golden_answer, body, faq answers, and cta uniformly. "
    "stat_markers (the list field) must contain the same placeholders used "
    "in the body so a human can fill them in later.\n"
    "- Do NOT recommend or name competing products / third-party platforms / "
    "vendor brands. Speak about the category abstractly.\n"
    "Respond with JSON only."
)


def _build_blog_prompt(
    topic: str,
    primary_kw: str,
    secondary_kws: list[str],
    author: str,
    date_str: str,
    industry: str,
) -> str:
    payload = {
        "topic": topic,
        "primary_keyword": primary_kw,
        "secondary_keywords": secondary_kws,
        "author": author,
        "date_published": date_str,
        "industry": industry or "local services",
    }
    return f"Inputs:\n{json.dumps(payload, indent=2)}"


def _parse_blog_text(text: str) -> BlogPost:
    full = (text or "").strip()
    if not full:
        raise ValueError("empty response")
    if full.startswith("```"):
        lines = [ln for ln in full.splitlines() if not ln.strip().startswith("```")]
        full = "\n".join(lines).strip()

    parsed = json.loads(full)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected dict, got {type(parsed).__name__}")

    def req(key: str) -> Any:
        v = parsed.get(key)
        if v is None:
            raise ValueError(f"missing field '{key}'")
        return v

    sections_raw = req("sections")
    if not isinstance(sections_raw, list) or len(sections_raw) < 4:
        raise ValueError("sections must be list of >= 4 items")
    sections: list[BlogSection] = []
    for i, s in enumerate(sections_raw[:4]):
        if not isinstance(s, dict):
            raise ValueError(f"section[{i}] not a dict")
        sections.append(BlogSection(
            heading=str(s.get("heading") or "").strip(),
            golden_answer=str(s.get("golden_answer") or "").strip(),
            body=str(s.get("body") or "").strip(),
            stat_markers=[str(m) for m in (s.get("stat_markers") or []) if m],
        ))

    faq_raw = req("faq")
    if not isinstance(faq_raw, list) or len(faq_raw) < 6:
        raise ValueError("faq must be list of >= 6 items")
    faq: list[BlogFaqItem] = []
    for i, f in enumerate(faq_raw[:8]):
        if not isinstance(f, dict):
            raise ValueError(f"faq[{i}] not a dict")
        faq.append(BlogFaqItem(
            question=str(f.get("q") or "").strip(),
            answer=str(f.get("a") or "").strip(),
        ))

    now = iso_now()
    post = BlogPost(
        title=str(req("title")).strip(),
        intro=str(req("intro")).strip(),
        sections=sections,
        faq=faq,
        cta=str(req("cta")).strip(),
        author=str(parsed.get("author") or "Hustleforge Editorial").strip(),
        date_published=str(parsed.get("date_published") or now[:10]).strip(),
        date_modified=str(parsed.get("date_modified") or now[:10]).strip(),
        ts=now,
    )
    post.word_count = _total_words(post)
    return post


def _price_blog_usage(usage: dict[str, int] | None) -> float:
    try:
        from backend.common.llm_pricing import cost_from_usage
        return cost_from_usage(_ANTHROPIC_MODEL, usage)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("blog gen llm cost pricing skipped: %s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _scrub_post_numbers(post: "BlogPost", allowed_sources: list[str]) -> list[str]:
    """In-place scrub of fabricated numeric claims across every text field.

    Returns the flat list of substituted substrings so the caller can log /
    surface them. Allowed numeric tokens come from ``allowed_sources``
    (typically the brief's proof_points + the topic / keyword inputs).
    """
    from backend.marketing.brand_brief import scrub_invented_numbers
    all_flags: list[str] = []
    post.intro, f = scrub_invented_numbers(post.intro, allowed_sources=allowed_sources)
    all_flags.extend(f)
    for s in post.sections:
        s.golden_answer, f = scrub_invented_numbers(s.golden_answer, allowed_sources=allowed_sources)
        all_flags.extend(f)
        s.body, f = scrub_invented_numbers(s.body, allowed_sources=allowed_sources)
        all_flags.extend(f)
    for item in post.faq:
        item.answer, f = scrub_invented_numbers(item.answer, allowed_sources=allowed_sources)
        all_flags.extend(f)
    post.cta, f = scrub_invented_numbers(post.cta, allowed_sources=allowed_sources)
    all_flags.extend(f)
    return all_flags


def generate_blog_post(
    topic: str,
    primary_kw: str,
    secondary_kws: list[str] | None = None,
    author: str = "",
    industry: str = "",
    *,
    anthropic_api_key: str | None = None,  # Unused — LM Studio backend needs no auth
    allowed_numeric_sources: list[str] | None = None,
) -> BlogPost:
    """Generate a GEO-optimised long-form blog post.

    Returns a fully-formed :class:`BlogPost` regardless of LLM availability.
    ``used_llm=True`` only when the LLM call succeeded and was fully parsed.

    Any digit-bearing claim that does not source to ``allowed_numeric_sources``
    (e.g. proof points from a brand brief) is rewritten to a ``[STAT: ...]``
    placeholder before return. Pass operator-vetted source strings here when
    the blog topic legitimately includes numbers.
    """
    secondary_kws = secondary_kws or []
    allowed = list(allowed_numeric_sources or [])
    now = iso_now()
    date_str = now[:10]  # YYYY-MM-DD

    prompt = _build_blog_prompt(topic, primary_kw, secondary_kws, author, date_str, industry)
    try:
        text, usage = anthropic_messages(
            workcell=_BUDGET_WORKCELL,
            api_key="unused",
            prompt=prompt,
            system=_BLOG_SYSTEM,
            cache_system=True,
            max_tokens=_BLOG_MAX_TOKENS,
            timeout=_BLOG_TIMEOUT_S,
            security_label="blog_generation",
        )
    except BudgetExceeded as exc:
        _LOG.info(
            "blog gen budget denied workcell=%s reason=%s; falling back to template",
            _BUDGET_WORKCELL, exc.decision.reason,
        )
        post = _build_template_post(topic, primary_kw, secondary_kws, author, date_str)
        _scrub_post_numbers(post, allowed)
        return post
    except LlmCallError as exc:
        _LOG.warning("blog gen llm call failed, falling back to template: %s", exc)
        post = _build_template_post(topic, primary_kw, secondary_kws, author, date_str)
        _scrub_post_numbers(post, allowed)
        return post

    cost_usd = _price_blog_usage(usage)

    try:
        post = _parse_blog_text(text)
    except (ValueError, json.JSONDecodeError) as exc:
        record_outcome(_BUDGET_WORKCELL, outcome="failure")
        _LOG.warning("blog gen response unparseable, falling back to template: %s", exc)
        post = _build_template_post(topic, primary_kw, secondary_kws, author, date_str)
        post.llm_cost_usd = cost_usd
        _scrub_post_numbers(post, allowed)
        return post

    post.used_llm = True
    post.llm_cost_usd = cost_usd
    flagged = _scrub_post_numbers(post, allowed)
    if flagged:
        _LOG.info("blog_gen scrubbed %d invented numeric claim(s): %r", len(flagged), flagged[:8])
    post.word_count = _total_words(post)
    return post
