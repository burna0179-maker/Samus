# 01 — Premise

## What Samus is

Samus is an **information-asymmetry arbitrage engine** that happens to deliver
its value through outbound business development.

The asymmetry: a small business's public surface — their website, their
certificates, their DNS posture, their SEO health — contains facts about that
business that the owner does not know and would value learning. Samus finds
those facts cheaply at scale, and Alex monetizes the gap between "you don't
know this" and "I'll fix it for $X."

That is the whole business. The pipeline (prospecting → audit → proposal →
outreach → voice → CRM) is the **delivery vehicle**. The engine is the
asymmetry.

This framing matters because it changes what "more proactive" should mean.
A sales pipeline gets more proactive by sending more emails, calling more
people, escalating sooner. An arbitrage engine gets more proactive by
**noticing more, more specifically, earlier**, on the prospects most worth
the trade. Those are different products with different failure modes.

## What Samus is not

- **Not a cold-email blast.** Volume of outreach kills credibility. Five
  hand-tailored discoveries beats five hundred form letters every time.
- **Not an SDR replacement.** A real SDR builds warmth — Chamber memberships,
  referrals, LinkedIn relationships, named introductions. Samus is software;
  it cannot do these things. Pretending it can is the path to "this looks
  like a phishing operation" (see [chapter 02](02_council_verdict.md), the
  Outsider's verdict).
- **Not a robo-dialer.** TCPA exposure on automated cold calls is
  asymmetric, uninsurable for a solo builder, and quorum-gating it is
  "governance theater on an illegal action" (Contrarian). Samus drafts
  voicemails for Alex to invoke. The dialer never autonomously fires.
- **Not a subscription product.** A `$39/mo External Posture Monitor` was
  proposed (Expansionist) and rejected (Contrarian + Outsider) because it
  "industrializes the liability" of automated diagnostic claims about
  non-customers. See [chapter 08, ADR-003](08_decisions_log.md).
- **Not a manipulation engine.** Tying Darwin's mutation engine to
  `closed_deal` as sole fitness signal would Goodhart-collapse into a
  rhetoric optimizer that learns whatever closes deals fastest. The reward
  function subtracts harm explicitly — see [chapter 04, Guardrail G7](04_guardrails.md).

## The thesis, restated

**Proactive ≠ faster outbound. Proactive = deeper noticing before contact,
and warmer pre-flight signals at contact.** The Codex everywhere reflects
this inversion. If you find yourself adding throughput, ask first: would
you be adding *credibility per contact* by adding it? If not, stop.

## The keystone

The Stake Sentence ([chapter 03](03_stake_sentence.md)) is the irreducible
human input that makes the engine work. One sentence per prospect, written
by Alex, declaring why he chose this specific business. It is unfakeable by
LLM, unscrapeable by anyone else, and the surrounding automation exists
precisely to amplify its weight.

Without the Stake Sentence, Samus is a robot blasting strangers with
diagnoses they didn't ask for. With it, Samus is a careful operator's
research assistant whose conclusions Alex signs his name to before they
leave the building.

## The single hard rule

If a change to Samus could make the Stake Sentence less load-bearing — make
it shorter, make it templated, make it optional, make it auto-generated, make
it bypassed in some "high-confidence" branch — that change is forbidden.
Period. The whole architecture exists to make the 5% of human input the
load-bearing part.

If the 5% becomes 0%, this becomes phishing.
