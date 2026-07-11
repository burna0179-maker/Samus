"""SEO workcell - audit -> optimize -> generate content pipeline.

Three sequential capabilities feed each other:

    audit_site       -> AuditResult     (deterministic httpx + heuristics)
    optimize_page    -> OptimizeResult  (deterministic recommendations)
    generate_content -> ContentResult   (Anthropic claude-haiku-4-5; templated fallback)

Local-first per the recovered system prompt: deterministic audit + reccs run
without any external key; only the content-generation step calls Anthropic.
"""
