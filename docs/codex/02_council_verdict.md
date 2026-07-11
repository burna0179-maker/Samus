# 02 — The Council Verdict

On 2026-05-29 a five-advisor Council deliberated the question:

> *Once the 3-phase plan (funnel → proposal → closure) is instituted, how could
> I increase the proactive traits of Samus further, while staying inside the
> guardrails?*

The Council **rejected the premise**. The verdict reframed "proactive" from
*throughput-and-escalation* to *truth-and-warmth*, and that reframing is the
spine of every chapter that follows. This document preserves the deliberation
because the *why* matters more than the *what*.

---

## The five chairs

| Chair | Core position |
|---|---|
| **Contrarian** | Don't crank proactivity. The dialer is TCPA bait, the Gap Report is auto-defamation, the reward loop is a vanity-metric money pit. *Make it accurate first.* |
| **First-Principles** | "Proactive" is the wrong axis. Samus is an information-asymmetry arbitrage engine; the bottleneck is being-believed, not throughput. *5 hand-tailored discoveries beats 500 form letters.* |
| **Expansionist** | The thinking is too small. Productize the Gap Report ($39/mo Posture Monitor), turn install base into the next pipeline, wire Darwin→CRM fitness into a vertical-specialist learning loop. |
| **Outsider** | From outside, this looks like phishing. "Proactive" conflates four distinct actions; Samus has 0% referrals. *Real warmth before faster outbound.* |
| **Executor** | Eight ranked moves. Ship the live-pause unblock (Apollo+SES+postal DPAPI) this week, then quorum-gate the dialer, refactor reward, drift-watch dormant accounts. |

---

## Where the Council converged

**Strongest idea — backed by 3 of 5 reviewers (Contrarian, Outsider, Executor):**
**Verification gate between detection and action.** Concretely: Gap Report
claims carry an `evidence_source` enum (`crawled_header | cert | dns |
redirect`); LLM-inferred vulnerabilities rejected at the serialization layer.
Executor's quorum-gate on the dialer is the same organ from the other side.

**Most dangerous idea — flagged by 3 of 5 (Contrarian, Outsider, Executor):**
**The $39/mo Posture Monitor subscription + Darwin-on-close-rate fitness loop.**
Industrializes liability with contracts and churn metrics, breeds a
manipulation-optimizer, scope-explodes the build. Outsider's line lands: *"you
don't franchise a restaurant whose kitchen is on fire."*

**Traps everyone hit (4 of 5 reviewers, including First-Principles on its own
submission):** every proposal accelerates an unexamined premise — *that
auto-generated diagnostics about non-customers, delivered to those
non-customers, is a legitimate primitive*. Accurate defamation is still
defamation. Quorum-gated cold-dialing is still a TCPA target. Validated
subscriptions on unvalidated underlying claims is the Expansionist trap
squared.

**Quantified agreement:** 5/5 said the reward function must change (penalize
false-positive contact, not just reward closure). 4/5 said the auto-dialer
is the single highest-risk leg. 3/5 said the Gap Report needs human review
before external send. **0/5 endorsed cranking proactivity along the axis the
question implied.**

---

## The Chairman's verdict — verbatim

> *The council is rejecting the premise of your question.* "How do I increase
> proactive traits" assumes proactivity is the right axis. It isn't — yet.
> Here's the corrected question and answer:
>
> **Increase proactivity along the *truth-and-warmth* axis, NOT the
> *throughput-and-escalation* axis.** Concretely:
>
> 1. **Strip the Gap Report to crawler-verifiable facts only.** External-facing
>    claims must carry an `evidence_source ∈ {crawled_header, cert, dns,
>    redirect, public_registry}`. LLM-inferred vulnerabilities rejected at
>    serialization with a pytest. This kills the defamation vector and —
>    paradoxically — makes Samus *more* credible per email.
>
> 2. **Kill the auto-dialer leg, replace with a voicemail-draft-for-Alex.**
>    TCPA exposure is asymmetric and uninsurable for a solo builder.
>    Quorum-gating it is governance theater on an illegal action. Samus
>    drafts the voicemail; Alex pushes record.
>
> 3. **Rebuild `reward_density`** as `stage_advanced − llm_cost_cents − k *
>    (retracted_claims + unsubscribes + complaints)`, with terminal multiplier
>    on `stripe.payment_intent.succeeded` matched to `prospect_id`.
>    Closure-only fitness is what would make Darwin-mutation dangerous;
>    subtracting harm makes the loop self-correcting.
>
> 4. **Add a pre-flight legitimacy signal** before any outbound: prospect must
>    have ≥1 of {public RFP, Chamber roster membership, prior inbound, public
>    hiring signal, deterministic registry hit}. Cold-cold prospects go to a
>    "needs warm path" queue, not the outreach queue.
>
> 5. **Then — and only then — ship Executor #1 (live-pause unblock).** With
>    #1–4 in place, email firing is safe to crank.
>
> **Defer productization entirely until you have ≥10 closed deals on the
> current funnel.** Subscription-shaped products on top of unvalidated atomic
> units is the move that ends companies.

---

## What the Council told us to AVOID

- **Auto-dialing strangers under any guardrail** — quorum-CLEAN doesn't
  neutralize TCPA; *"three robots agreeing to do the lawsuit together"*
  (Outsider).
- **Any new revenue surface** ($39/mo Posture Monitor, new Stripe SKUs)
  until the underlying claim layer is verified and ≥10 deals close on the
  current funnel.
- **Darwin-mutation tied to `closed_deal` as sole fitness signal** —
  Goodhart collapse → manipulation-optimizer.
- **Reward-function rewrites without a defined meter, data source, and
  regression test** (Executor's caveat).
- **Shipping live-pause removal on a calendar, not on a verified
  hallucination-rate number** on a labeled Gap Report sample.

---

## The decisive insight

The post-Council follow-up question reframed the whole thing again:

> *In a highly constrained environment where every action must be traceable
> back to an external fact, and every follow-up touchpoint is manually vetted
> for legal risk, what single, small piece of human input — what one
> non-protocol-driven element — can we introduce at the point of contact that
> acts as a hyper-efficient catalyst?*

The answer became the **Stake Sentence** — see [chapter 03](03_stake_sentence.md).
The Codex's spine runs through that chapter.

---

## Why this chapter is preserved verbatim

Future-you will be tempted to revisit "more proactive = good." The Council's
verdict is the document that breaks the spell. Read it again before you
crank a knob.
