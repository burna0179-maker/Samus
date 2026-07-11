"""Samus signal_filter workcell — prospect pre-qualification gate.

A deterministic admission gate that sits in front of the prospecting →
enrichment → outreach pipeline. Inbound prospects are enriched from Tier-1
deterministic sources (DNS/MX, SSL, homepage fetch), scored into a
:class:`~backend.signal_filter.scoring.ProspectSignal`, and run through a
weighted threshold (:func:`~backend.signal_filter.queue_gate.should_enqueue`).
Only high-confidence prospects pass — low-probability prospects are rejected
before they consume queue throughput, LLM budget, SEO audit cycles, or
outbound outreach.

Zero LLM calls by design — the whole purpose of this workcell is to *reduce*
token spend, so it is pure deterministic logic.
"""
