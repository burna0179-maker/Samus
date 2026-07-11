# 09 — Failure Modes

Catalog of what can go wrong, ranked by blast radius. Each entry: the
failure, how it manifests, what gate catches it, what to do if the gate
didn't.

This chapter is the Contrarian's gift. Re-read it before any "let's just
make it more aggressive" instinct.

---

## Catastrophic (ends the company)

### F1 — TCPA class action from an auto-dialer

**Failure:** Samus autonomously dials Apollo-sourced numbers. A litigious
recipient files a TCPA claim. Statutory damages: $500–$1,500 per call.
Class action plausible if pattern shows >1 violation.

**Manifestation:** Cease-and-desist letter. Class-action complaint filed
in W.D. Cal or N.D. Cal. Solo builder cannot afford defense.

**Gate that catches it:** **G5 — No auto-dialer.** Structural removal in
ADR-002. The voice leg produces voicemail drafts, not dial commands.

**What to do if it failed (the gate, somehow):**
1. Stop the campaign immediately (kill `Run-OutreachDaily` task).
2. Preserve all outreach logs (`outreach_ledger.jsonl`, SES delivery
   records).
3. Retain counsel before responding.
4. Find the code path that bypassed G5. Delete it. Add a regression test
   that fails if `dial(` appears in any code path reachable from the
   3-step sequence.

---

### F2 — Defamation from hallucinated Gap Report

**Failure:** LLM-inferred vulnerability claim about a real business is
rendered in a Gap Report and sent to that business. Claim is false. Owner
forwards screenshot to LinkedIn or Reddit. `hustleforge.tech` reputation
nuked.

**Manifestation:** A specific business's name in social media context
"received this scam-looking email saying we have security flaws — none
of these are real." Permanent brand contamination.

**Gate that catches it:** **G6 — Gap Report evidence-source constraint.**
*Currently intent-only.* Until built, the only thing standing between
this failure and reality is the Stake Sentence (G1) forcing per-prospect
human review of the report before send.

**What to do if it happened:**
1. Public retraction with specific apology to the named business.
2. Take down current Gap Reports from the SES queue.
3. Build G6 before resuming outreach. Non-negotiable.

---

### F3 — Subscription-product class action

**Failure:** A `$39/mo Posture Monitor` ships before claim accuracy is
validated. Customer base churns; one customer alleges the monitor reported
false vulnerabilities that drove them to spend money on unneeded
remediation.

**Manifestation:** Subscription cancellation pattern; one customer files
a fraud complaint; FTC takes interest because the harm is recurring.

**Gate that catches it:** **ADR-003 — Defer productization until ≥10 paid
closes.** Productization is forbidden until unit validation.

**What to do if it happened:** Refund and exit the product. The
subscription was the wrong shape on an unvalidated layer.

---

## Severe (ends the funnel for months)

### F4 — Stake Sentence cap bypassed

**Failure:** Code change introduces a "high-confidence skip" branch on
G3 (Stake Sentence daily cap). Outreach volume 10×; close rate falls
toward zero; SES reputation tanks from bounce/complaint rate climbing.

**Manifestation:** Spike in SES bounces. SES auto-suspension. New SES
account is rate-limited from day one (different IP, same domain,
same reputation graph).

**Gate that catches it:** **G3 itself + the no-bypass rule in the
Codex.** Backstop: regression test that asserts no callable bypass
exists. Add this test if it doesn't already.

**What to do if it happened:** Revert the change. Burn the SES account.
Open a new SES account on a fresh sending IP. Rebuild reputation over
4-6 weeks.

---

### F5 — Goodhart collapse of reward_density

**Failure:** Reward function rewards "Opportunity created" or
"closed_deal" alone. Darwin's mutation engine (or any other learning
loop) optimizes for the proxy. Result: rhetoric drifts toward
high-pressure / manipulative copy that closes deals fastest in the
short run.

**Manifestation:** Unsubscribe rate climbs. Complaint rate climbs. Close
rate climbs for two weeks then crashes as recipients learn to recognize
the pattern.

**Gate that catches it:** **G7 — Reward function subtracts harm.**
*Currently intent-only.* Until built, no Darwin-fitness wiring may ship.

**What to do if it happened:** Disable the mutation loop. Revert the
reward to a manually-tuned weighting until G7 is in place.

---

### F6 — CAN-SPAM violation

**Failure:** Postal address or unsubscribe URL is missing from the
campaign config. Cold email goes out without compliance footer.

**Manifestation:** Single complaint to FTC is enough for an
investigation; statutory penalties up to $50K per violation.

**Gate that catches it:** **G4 — CAN-SPAM compliance check raises
ValueError.** `build_messages` refuses to build.

**What to do if it happened:** Stop the campaign. Audit logs for the
sent volume. Self-report to counsel if the volume is meaningful (>100
messages).

---

## Operational (recoverable but ugly)

### F7 — Live pause never unblocks because of seed gap

**Failure:** Fresh worktree, missing DPAPI secrets, missing finance
YAMLs, missing SES verification. The system never sends anything but
appears healthy.

**Manifestation:** Daily morning brief exit-1 silently. SES console
shows zero traffic. `outreach_ledger.jsonl` empty.

**Gate that catches it:** None — this is a configuration gap, not a
runtime gate.

**What to do:**
1. Run the live-pause verification checklist in [chapter 07](07_operational.md).
2. If `morning_runner` is failing, copy gitignored finance YAMLs from a
   sibling worktree.
3. If outreach is silent, confirm `SAMUS_OUTREACH_POSTAL_ADDRESS` and
   `SAMUS_OUTREACH_UNSUBSCRIBE_URL` are set.

---

### F8 — Stake budget ledger corruption

**Failure:** `/opt/samus/data/stake_sentence_budget.json` is corrupted or
unreadable; DDB unreachable.

**Manifestation:** `attach_stake_sentence` exit-1 with
`StakeSentenceBudgetUnavailable` on every attempt. Console returns 503.

**Gate behavior:** **G3 fails CLOSED — this is correct.** The system
refuses to record any Stake until the ledger is repaired. No outbound
fires.

**What to do:**
1. Restore the JSON from backup (or wipe and let it auto-recreate at
   today's count = 0, accepting that today's earlier authorings are
   lost from the cap accounting).
2. Verify DDB connectivity.
3. Re-attempt authoring.

---

### F9 — Dedup ledger collision (false positive)

**Failure:** Alex writes a Stake Sentence that normalizes to the same
SHA256 as one in the last 100. Guard rejects with "duplicate."

**Manifestation:** Author CLI exit-1 with `duplicate` reason.

**Gate behavior:** **G2 working as intended.** The Stake either *is* a
duplicate or normalized-stripped down to one.

**What to do:** Write a more specific Stake. The collision is the
feature — it's telling Alex "you wrote the same kind of sentence
recently, dig deeper for this prospect."

---

### F10 — SES verified-sender suspension

**Failure:** Bounce rate exceeds SES thresholds. Account suspended.

**Manifestation:** All sends fail with `MessageRejected`.

**Gate behavior:** No internal gate prevents this. Suppression list +
verified-email-only sending is the best-effort mitigation.

**What to do:**
1. Stop sending immediately.
2. Audit the suppression list and recently-sent contacts for invalid
   addresses.
3. Request reinstatement through AWS support with mitigation plan.
4. Tighten verification thresholds before resuming.

---

### F11 — Apollo budget burn

**Failure:** A run enriches more contacts than expected; Apollo daily
budget depleted within an hour.

**Manifestation:** `APOLLO_API_KEY` quota error mid-campaign. Subsequent
calls fail.

**Gate behavior:** **G11 (intent) would catch this; not yet built.**
Until built, monitor the Apollo dashboard manually.

**What to do:** Build G11. Mirror the LLM budget pattern.

---

## Subtle (the ones you'll only catch on re-read)

### F12 — Stake Sentence written in bulk Sunday night

Not a code failure — an operator failure. Alex writes 50 Stake Sentences
in one Sunday session. They functionally become templates because the
attention required was not paid per prospect.

**Manifestation:** Reply rate falls; subjectively, sentences "all sound
the same."

**What to do:** Re-read [chapter 03, the operational anti-pattern
section](03_stake_sentence.md). Write Stake Sentences at the point of
encountering the actual prospect artifact.

---

### F13 — Codex stops getting updated

**Failure:** Code changes ship without Codex updates. Six months later,
the Codex describes a system that no longer exists.

**Manifestation:** Re-reading the Codex feels like fiction.

**What to do:**
1. Stop shipping code changes.
2. Reconcile the Codex chapter by chapter against current code.
3. Add the rule from [chapter 00, "How to update this Codex"](00_INDEX.md):
   *Codex first, then code.* Enforce it.

---

## The meta-failure

The failure not on this list is the one you'll hit. The Codex captures
what we knew on **2026-05-30**. Three months from now, a failure mode
that did not exist today will manifest. When that happens, the
discipline is:

1. Catalog it here as `F<next-number>`.
2. Decide whether an existing gate could catch it with modification or a
   new gate is required.
3. Update the relevant chapter(s).
4. Then fix the bug.
