"""AIO (AI Answer Optimization) measurement — Phase C of the growth roadmap.

Only ~14% of marketers track whether AI answer engines cite them; starting now
is a structural advantage. This workcell asks a set of ICP questions across AI
platforms (Claude live via the budget-gated LLM client; OpenAI / Perplexity
when their keys are present) and measures:

* **citation rate** — fraction of answers that mention the brand
* **share of voice** — brand mentions vs competitor mentions
* **cited-source domains** — which 3rd-party sites the engines pull from

All probing is read-only (no posting, no writes beyond an auditable JSONL
ledger) and budget-gated. The analysis core (:mod:`backend.visibility.analyze`)
is pure and fully testable without any network call.
"""
