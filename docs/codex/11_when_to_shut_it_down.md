# 11 — When To Shut It Down

> *When this whole thing gets too big, or too complex, or runs its course...
> we know exactly how to operate it without losing ourselves in the
> complexity of our own genius.*

This is the chapter you re-read when you're tired. It exists so that
"should we keep doing this?" is a question Samus can answer rather than a
question that haunts.

---

## Three reasons to shut Samus down

### 1. It worked

The most common reason a project should end. Samus succeeded — Alex has
the close rate he wanted in the vertical he picked, and the operating
overhead is now larger than the marginal value of running the agent.

**Signal:** Cost of operating Samus (electricity, AWS, Apollo, SES,
attention) exceeds the value of the next 10 prospects Samus would
surface, *and* Alex's pipeline is fed without Samus's surfacing.

**Action:** [Wind-down procedure](#wind-down-procedure) below. Keep the
artifacts.

### 2. The asymmetry closed

Samus's edge was information asymmetry: finding facts about prospects
they didn't know. If a competitor or a free tool makes those facts
available to the prospect first (Cloudflare publishes posture scores,
Google adds security-grade to search results, an open-source tool
auto-scans every business website in a vertical), the asymmetry is gone.

**Signal:** Reply rate falls below 2% even with valid Stake Sentences.
Recipients say "we already knew" or "we already got this from X."

**Action:** Don't crank harder. The product is gone. Wind down.

### 3. The Codex describes a system that doesn't exist anymore

If you re-read this Codex and three or more chapters describe behavior
the code no longer has — *and you can't easily reconcile* — then either
the code has drifted far enough that nobody remembers why, or the Codex
stopped getting updated and trust in the documentation is gone.

**Signal:** [F13](09_failure_modes.md) (Codex stops getting updated).

**Action:** Either rebuild the Codex from current code (a real
multi-week effort) or shut Samus down. Operating a system whose
documentation lies about its behavior is how solo builders end up
losing themselves in the complexity of their own genius — which is
exactly what this Codex was written to prevent.

---

## What "shut it down" means concretely

Samus shutdown is not "delete everything." It's an ordered, reversible
wind-down that preserves the artifacts that have value beyond the
agent itself.

---

## Wind-down procedure

### Phase A — Stop the outbound (15 minutes)

1. **Disable the scheduled task:**
   ```powershell
   Disable-ScheduledTask -TaskName "Run-OutreachDaily"
   Disable-ScheduledTask -TaskName "Run-ProspectingDaily"
   ```
2. **Block the campaign builder:** Unset
   `SAMUS_OUTREACH_POSTAL_ADDRESS` — campaign refuses to build (G4).
3. **Verify:** SES dashboard shows zero new sends within 30 minutes.

At this point Samus is **silent but not gone**. The data is intact. You
can resume by reversing this phase if you change your mind.

### Phase B — Drain in-flight Opportunities (1-7 days)

Any Opportunity already in `proposal` or `negotiation` stage may be
worth manually closing. Use the operator console:

```
GET /api/console/opportunities/pending_stake  → review and finish
```

For Opportunities in `new` or `qualified` with no Stake Sentence
authored, decide per-Opportunity whether to author + send manually or
abandon to `closed_lost`.

**Do not skip this phase.** Samus's value is in the relationships, not
the pipeline data, and an abandoned prospect mid-conversation is worse
than no contact.

### Phase C — Snapshot the artifacts (1 hour)

Before anything is deleted, preserve:

1. **CRM data:** Export `samus_opportunities`, `samus_artifacts`,
   `samus_prospects`, `samus_contacts` to JSONL into
   `D:\Hustleforge\Samus\.archive\<YYYY-MM-DD>\`.
2. **Codex:** This `docs/codex/` directory in current state. Tag the
   git commit `samus-codex-final`.
3. **Stake Sentence ledger:** Every Stake Sentence Alex ever wrote.
   This is the most valuable artifact in the system — it's the raw
   record of attention paid across hundreds of prospects.
4. **Outreach ledger:** `outreach_ledger.jsonl` — the audit trail.
5. **Decisions Log:** [Chapter 08](08_decisions_log.md). Even after
   Samus is gone, the decisions made building it inform future projects.

### Phase D — Decommission infrastructure (2 hours)

1. **AWS:** Snapshot DDB tables to S3. Delete tables. Cancel SES
   verification on dedicated sending IPs.
2. **DPAPI secrets:** Rotate or delete via `Hustleforge.Secrets` with
   scope `Samus`.
3. **Scheduled tasks:** Unregister, don't just disable.
4. **Docker:** `docker-compose down` on the Samus stack. `docker volume
   rm` for Samus volumes after confirming archive in C.
5. **Apollo / Anthropic / Stripe:** Rotate API keys. Cancel the Apollo
   plan if no other agent uses it.

### Phase E — Update the ecosystem (30 minutes)

1. **Other agents:** Anita, Major, Darwin, Optimus, Sapphire all need to
   know Samus is gone. Update `quorum` membership; remove Samus from
   hub subscribers; remove from boot scripts.
2. **Mark the repo:** Add a `RETIRED.md` at `D:\Hustleforge\Samus\`
   noting the date, the reason, and a pointer to the archive.

### Phase F — Memory and reference (15 minutes)

1. **Memory pointer:** Update the project memory to mark Samus as
   retired with date and reason.
2. **Wiki:** Move active Samus references in the Hivemind wiki to a
   `_archive/` zone. Leave the Codex itself in place as a reference.

---

## What survives after Samus is gone

These outlast the agent:

1. **The Stake Sentence discipline.** Whatever Alex builds next, the
   discipline of authoring one sentence of declared chosenness per
   prospect transfers. The keystone insight is portable.
2. **The Council verdict.** "Proactive ≠ faster outbound" applies to
   every BD project, not just Samus.
3. **The Decisions Log.** Every ADR captures a reason that informs the
   next decision.
4. **The Gap Reports.** The actual diagnostic value Samus produced for
   prospects (real or not-yet-closed) is portable — Alex can carry the
   skill of producing them by hand into other channels.
5. **The relationships.** Any prospect who became a customer is a
   relationship that survives the agent. The CRM export is the seed.

---

## What does NOT survive

- The automation. That's the point — when the automation no longer pays
  for itself in attention saved, it gets retired.
- The brand association with `hustleforge.tech` as an automated outbound
  source. If a future project re-uses the domain, the reputation graph
  carries forward. Decide whether that's a benefit or a cost.
- Any in-flight Opportunity not closed during Phase B. They're gone.
  Don't pretend they're not.

---

## The decision to shut down is not a failure

Building Samus taught us:
- That guardrails are the product, not the friction.
- That one human sentence outweighs a hundred machined ones.
- That "more proactive" is the wrong axis 90% of the time.
- That the Council framework — five lenses, blind cross-review, decisive
  Chairman — produces verdicts that survive contact with reality.

If Samus shuts down at the right moment, every lesson above is
captured in this Codex and applies to the next thing. That is the
purpose of a shared memory.

---

## When NOT to shut down

Watch for these false signals — they look like reasons to shut down but
aren't:

1. **"It feels overwhelming."** Samus runs itself. The feeling of
   overwhelm comes from accumulated context in your head, not the agent.
   Re-read [chapter 00](00_INDEX.md). Take a week off from authoring
   Stake Sentences. The agent will still be silent and patient when you
   come back.
2. **"Reply rate dipped this week."** Three-month moving average is the
   signal, not the weekly. Don't kill a working system for a noisy
   sample.
3. **"A competitor launched something similar."** A competitor selling
   the same product to the same prospects in the same vertical does not
   close the asymmetry — they expand the market for it. Wait for the
   asymmetry signal (recipients saying "we already knew"), not the
   competitor signal.
4. **"It's been a year and it hasn't 10×'d."** A 10× outcome was never
   the design goal — the design goal was a sustainable solo-builder
   pipeline. Re-read [chapter 02 ADR-001](08_decisions_log.md).

If you're considering shutdown, sleep on it for a week. If you still
want to shut down after a week, the procedure above is here.

---

## The last paragraph

When you shut Samus down — whenever that is, whether it's six months
from 2026-05-30 or six years — leave this Codex in the archive. Future
projects, future agents, future versions of Alex, all benefit from
knowing what was tried, what was decided, and what was learned.

The Codex is the inheritance.
