# System Evolution

Engineering narrative of how Samus evolved from v1.0 through v2.1.1. This is not a changelog. It's a record of which design assumptions broke, what evidence surfaced the breakage, and what the next design stage had to solve.

---

## v1.0: The Right Design for an Unknown Problem

v1.0 was a pipeline. Prospect discovery fed into outreach generation fed into delivery. The workcells were coarse — a handful of FastAPI endpoints, minimal SQS usage, one LLM chain per outreach type. Governance was a checklist applied before send.

That design was right for the stage. At v1.0, I didn't know which parts of the problem were hard. I needed to find out what failed before I could design against failure. Building a sophisticated infrastructure for a problem space I hadn't mapped yet would have been speculative over-engineering. The v1.0 architecture was simple enough that its failure modes were legible.

The main failure modes that became legible quickly: the pipeline had no admission control, the LLM chain had no cost floor, and the governance checklist was applied too late to be cheap.

---

## v1.1.0 — v1.2.0: Learning the Shape of the Problem

These versions added workcells as new failure modes surfaced. Each new workcell was a response to something specific: outreach template drift, prospect data quality variance, delivery rate instability. The workcell count grew from a handful to something approaching the current 21.

The structural problem these versions created was drift. With each workcell managing its own auth, retry, and logging independently, changes that touched the boundary of any workcell required working through each one separately. This was tolerable at eight workcells. It became expensive at twelve.

---

## v1.3.0: The Workcell Explosion and the First Layer of Control

By v1.3.0 I had enough working surface to see which problems were fundamental and which were incidental.

The fundamental problem was admission: I was spending LLM tokens and SES sends on prospects that the system should have rejected upstream. `signal_filter` was the fix — a 7-axis `ProspectSignal` composite with a calibrated admission threshold. The 0.62 value isn't theory; it's the boundary derived from scoring a real prospect batch after the workcell shipped.

`path_optimizer` (EMA-driven routing) came from observing that some outreach paths consistently underperformed others but the system had no mechanism to learn this. The EMA weight decays older performance data appropriately without requiring a full ML pipeline.

`template_recovery` was the most pragmatic addition: a deterministic scaffold fallback that generates a usable outreach template with zero LLM calls when the primary generation chain fails. It's not as good as the full chain. It's infinitely better than silence. The design explicitly accepts the quality tradeoff in exchange for reliability.

`portfolio_controller` addressed the LLM budget problem that had been accumulating since v1.0. Per-workcell quota tracking with priority rebalancing at dispatch replaced the single top-of-pipeline budget that was being consumed unevenly depending on queue timing.

`entropy` was the final v1.3.0 addition — a signal diversity workcell that detects when the outreach corpus is converging toward homogeneity and injects variation. It exists because a live run exposed that without it, the system gradually generates near-duplicate outreach at scale.

v1.3.0 also triggered the extraction of `backend/common/`. Three workcells had drifted on HMAC signing in the same two-week period. That was the evidence I needed.

---

## v2.0.0: Reconceiving the Control Loop

v1.x was a pipeline with instrumentation bolted on. The control flow was linear: admit → generate → govern → send. Feedback from outcomes wasn't feeding back into routing or generation decisions in any principled way.

v2.0.0 reconceived this as a MAPE-K autonomy loop: Monitor → Analyze → Plan → Execute, with a knowledge base that persists across cycles. This isn't autonomy for its own sake. It's the right abstraction for a system that needs to adapt its outreach strategy based on what's working without requiring manual reconfiguration.

The bandit strategy (UCB1/hierarchical) replaced static routing logic for outreach path selection. UCB1 handles the exploration-exploitation tradeoff that v1.x was managing poorly — the EMA from `path_optimizer` was a local approximation of this, and the v2.0.0 bandit is the generalization.

Governance was redesigned in v2.0.0 to link directly to the Codex. The 12-chapter Codex had existed since early development as a design specification, but in v1.x the governance workcells didn't enforce Codex rules at runtime — they enforced their own independent check logic, which could drift from the Codex. The `check_action` gate in v2.0.0 validates runtime calls against the declared Codex rules. Governance drift became structurally impossible.

The Stake Sentence requirement was enforced at dispatch in v2.0.0 after the v1.x design allowed LLM calls without operator-authored context. Making it an enforcement point rather than a convention was the right call — conventions erode under schedule pressure.

Immutable baseline for governance files was also a v2.0.0 addition. The motivation was straightforward: if governance files can be silently modified, the 11 guardrails aren't guarantees, they're defaults. Silencing that class of failure required making modification detectable.

---

## v2.1.1: Additive Seams Without Breaking Contracts

v2.1.1 added substantial capability — causal uplift experiments, a 5-advisor deliberation router, a belief ledger, resilience benchmarks, control-loop friction instrumentation, org-debt scoring — without modifying existing workcell contracts.

This required discipline. Each addition had to be grafted onto the existing system as a seam: a new call site that the existing workcell could invoke optionally, or a new signal that could inform routing without replacing existing routing logic. The alternative — modifying existing workcells to accommodate new capabilities — would have coupled the additions to the existing test surface and created regression risk across 4,582 test functions.

The additive seam pattern is now explicit design policy rather than an emergent constraint. New capability ships as a new workcell or a new optional seam. Existing contracts don't change to accommodate it.

---

## What This Trajectory Reveals

The system didn't start with a MAPE-K loop or a bandit strategy or causal uplift. It started with a pipeline because that's what the problem required at the time. Each architectural upgrade was justified by specific failure evidence, not by a priori design elegance.

The pattern that holds across all six versions: the right design at stage N is the simplest thing that makes the current failure modes legible. The wrong design at stage N is the design that solves stage N+2 problems before stage N problems are understood. Every significant addition in this system's history was motivated by a specific failure mode that became visible only after the previous stage was running in production.

The test surface — 416 files, 4,582 functions — grew in proportion to integration complexity, not in advance of it. It reflects accumulated knowledge of what breaks, not a coverage target set at the start.
