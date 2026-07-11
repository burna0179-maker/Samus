# 03 — The Stake Sentence

## The keystone

> One handwritten line from Alex, per prospect, inserted at the top of every
> Gap Report and read verbatim at the start of every voicemail. Not data. Not
> value-prop. **A declared judgment that proves a human chose this specific
> business.**

The Stake Sentence is the irreducible human input in an otherwise audited
machine. It is the load-bearing 5% the surrounding automation exists to
amplify. Without it, Samus refuses to fire.

---

## The shape

Canonical form:

> *"I'm reaching out because [one specific, non-protocol-derivable reason I
> personally chose you]."*

What it is **not**:
- Not the Gap Report finding ("your TLS expires Nov 14") — that's data.
- Not a hook ("noticed you're growing fast") — that's templatable.
- Not flattery, not a generic referral, not a manufactured common-ground.

What it **is**: a fact about *them* that an LLM cannot produce because it
requires Alex to have **decided something**. Examples of the shape:

- *"I'm reaching out because I drove past your Marysville location Thursday
  and the second-site expansion is exactly the moment this matters."*
- *"I'm reaching out because your competitor on H Street took 11 days to come
  back online last month and I don't want you in that line."*
- *"I'm reaching out because I'm only taking three commercial-real-estate
  accounts this quarter and you're one of two I picked."*
- *"I'm reaching out because the job listing you posted for an ops manager
  will fail their first week if this isn't fixed first."*

---

## Why it converts vendor→partner

The constrained environment we built — fact-anchored, human-vetted, audited —
does something paradoxical and powerful: it makes the one un-automated
sentence land with **disproportionate weight**. In a fully manual cold-email
world, "I noticed your new location" reads as a suck-up. In a *visibly
machined* artifact (Gap Report + priced remediation + literal evidence
sources), one human sentence at the top reads as **unusual deliberation**.
The asymmetry between the surrounding precision and that single hand-placed
sentence is itself the signal.

The mechanism is **chosenness**. Vendors get chosen *by* the prospect.
Partners get chosen *by each other*. A vendor says "we serve businesses like
yours." A partner says "I picked you, for this reason, and the reason isn't
something a scraper could find." Every transaction in the funnel becomes
asymmetric the moment the prospect realizes: the person on the other end of
this knew enough about my actual situation to refuse most of the queue and
stop on me.

---

## Where it appears

A single authored Stake Sentence renders verbatim in **three places**:

1. **Top of the outreach email body**, on its own line, blank line, then the
   compliance-footed template body.
2. **Top of the Gap Report markdown**, rendered as `> *{stake}*` with a
   horizontal rule below, above the severity-badge cover.
3. **Opening line of the callsheet opener and voicemail script**, followed
   by `...` (a pause cue for Alex when he records).

This is enforced in code at the composition sites:
- `backend/outreach/campaign.py::compose_body`
- `backend/seo/report.py::render_seo_report_markdown`
- `backend/prospecting/callsheet.py::_opener` and `_voicemail`

---

## The guard

Every Stake Sentence is validated by
`backend/common/stake_sentence_guard.py::validate_stake_sentence` before it
can be persisted or rendered. Rejection reasons:

| Rule | Why |
|---|---|
| Length 40–280 chars | <40 = too thin to carry judgment; >280 = stops being a sentence |
| No banned phrases | Filters template tells: "noticed you", "we help businesses", "leverage", "synergy", "circle back", etc. (full list in [chapter 10](10_glossary.md)) |
| Not all-lowercase | No capital letter = no proper noun = no real human authored it |
| No repeated whitespace | Copy-paste artifacts |
| ASCII ratio ≥ 0.95 | Blocks emoji-spam and obfuscated content |
| Not a duplicate | SHA256 over normalized text vs the last 100 used; if you can't write a fresh sentence, you didn't actually choose them |

If any rule fails, the gate raises `StakeSentenceRejected`. No bypass exists.

---

## The daily cap

`backend/common/stake_sentence_budget.py` enforces a hard daily ceiling on
how many Stake Sentences can be recorded.

- Default: `10` per day (env `SAMUS_STAKE_SENTENCE_DAILY_CAP`)
- Persistence: DynamoDB `samus_stake_sentence_budgets` with JSON fallback at
  `/opt/samus/data/stake_sentence_budget.json`
- Reset: by UTC `bucket_day` on read
- **Fail-CLOSED**: if neither DDB nor JSON is readable/writable, the cap
  module raises `StakeSentenceBudgetUnavailable`. Recording is refused. This
  is inverted from the LLM budget (which fails-open on persistence error)
  because the Stake Sentence cap is the **outbound ceiling**, not a cost
  control. Losing the ceiling losing safety, not cost.

**This cap is Samus's real proactivity ceiling.** Not Apollo throughput, not
LLM budget, not opportunities created — the number of Stake Sentences Alex
can write per day with care. That number is the product.

---

## The flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. Prospect qualifies → Opportunity created                             │
│    (stake_sentence is OPTIONAL at create time — auto-callers don't      │
│     know it yet)                                                        │
├─────────────────────────────────────────────────────────────────────────┤
│ 2. Operator console lists pending: GET /api/console/opportunities/      │
│    pending_stake                                                        │
├─────────────────────────────────────────────────────────────────────────┤
│ 3. Alex authors stake_sentence per Opportunity:                         │
│    - CLI: python -m backend.crm.stake_opportunity <opp_id> "..."        │
│    - Console: POST /api/console/opportunities/{opp_id}/stake            │
├─────────────────────────────────────────────────────────────────────────┤
│ 4. Pipeline: cap → guard → dedup → write opp → record_use → artifact    │
│    Any failure: refuse, exit 1, no partial writes                       │
├─────────────────────────────────────────────────────────────────────────┤
│ 5. Outreach, Gap Report, Callsheet render the stake verbatim            │
│    The outreach gate REFUSES to fire on any opportunity with empty      │
│    stake_sentence. No bypass env var exists.                            │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Why `CreateOpportunityRequest.stake_sentence` is OPTIONAL

A subagent flagged this during implementation, and it's worth preserving the
reasoning because future-you will wonder.

Making it Pydantic-required at Opportunity birth would break ~24 auto-creation
callers: `convert_lead`, `log_call` booked-deal handoff, finance webhook
close-by-id, strategy bandit attribution, two dozen tests. All of these mint
an Opportunity *before* Alex has authored a Stake Sentence for it.

**The Opportunity is the slot; the Stake Sentence gets attached afterward.**
The hard invariant — *outreach refuses to fire without it* — is enforced at
the outbound dispatch gate, which is unconditional. That is the correct cut.

Validators still reject banned phrases / length / casing whenever a non-empty
Stake is supplied at create time. The "optional" only means "may be empty at
creation"; it never means "may be empty at dispatch."

---

## The hard rule, restated

If a change to Samus could make the Stake Sentence less load-bearing — make
it shorter, make it templated, make it optional at dispatch, make it
auto-generated, make it bypassed in some "high-confidence" branch — that
change is forbidden. The whole architecture exists to make the 5% of human
input the load-bearing part.

If the 5% becomes 0%, this becomes phishing.

---

## Operational anti-pattern: "I'll write them in bulk Sunday night"

The temptation is to sit down on Sunday and crank out 50 Stake Sentences for
the week. Don't. The point of the cap is not throughput control — it's
**attention forcing**. A Sunday-batch Stake Sentence is functionally a
template; it reads as one to the prospect because it was one to you.

Write the sentence when you encounter the prospect's actual artifact. Read
the Gap Report. Open the website. Think for ninety seconds. *Then* write the
sentence. If you can't, that prospect doesn't get outreach today. That's the
feature, not the bug.
