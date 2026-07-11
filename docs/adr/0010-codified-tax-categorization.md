# ADR-0010: Codified Tax Categorization Engine

## Status
Accepted

## Context
The LLC has real tax exposure (overdue CA franchise tax, quarterly estimates) but no tax-category awareness. The operator rejected CPA handoff and wants internal end-to-end ownership. LLM-freeform categorization is unacceptable for the same reason it is for SEO claims (ADR-0003, Codex G6): unexplainable classifications cannot be defended.

## Decision
Add a codified, versioned tax-rules YAML (per tax year) that maps vendor substrings and CODB categories to Schedule C lines. Every category assignment cites the matched rule. Unmatched items surface for operator review, never guessed. Estimated tax projection (SE tax, HoH brackets, CA franchise tiers) is pure math with zero I/O. Tax-break discovery and spending-timing recommendations are RECOMMEND-ONLY via the existing GuidanceLedger — never auto-file or auto-disburse.

## Consequences
Positive: auditable categorization with evidence trail; no external dependency for tax season; runway-safe recommendations.
Negative: ruleset must be maintained per tax year; no professional backstop means conservative positions only; filing and disbursement stay fully manual.
