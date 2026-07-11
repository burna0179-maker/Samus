# 08 — Decisions Log

ADR-style record of choices made and roads not taken. **Append-only.** Never
delete a decision — supersede it with a new entry that explains why.

Each entry: ADR-NNN | Date | Decision | Why | Consequences | Alternatives
rejected.

---

## ADR-001 | 2026-05-29 | Reject "more proactive" as the design axis

**Decision:** The original 3-phase plan's framing of "increase Samus's
proactive traits" is replaced. Proactivity along
*throughput-and-escalation* is forbidden; proactivity along
*truth-and-warmth* is the new axis.

**Why:** The Council unanimously concluded that escalation-style
proactivity collapses into TCPA exposure, defamation exposure, and a
Goodhart-collapsing reward loop. Volume kills credibility; the engine's
edge is information asymmetry, not throughput.

**Consequences:**
- Every chapter of the Codex inherits this reframing.
- The Stake Sentence (ADR-005) becomes load-bearing.
- The auto-dialer is structurally removed (ADR-002).
- Reward function is redesigned (ADR-004).

**Alternatives rejected:**
- "Quorum-gate the dialer and crank throughput" — governance theater on an
  illegal action.
- "Productize the Gap Report subscription and scale" — industrializes
  liability (ADR-003).
- "Add more workcells faster" — accelerates an unexamined premise.

---

## ADR-002 | 2026-05-29 | Auto-dialer is structurally removed

**Decision:** Samus does not autonomously dial telephones. The voice leg
produces a **voicemail draft artifact** Alex listens to and chooses whether
to record. No `SAMUS_AUTODIAL_ENABLED` flag will ever exist.

**Why:** TCPA exposure is $500–$1,500 per violating call, with class-action
risk. Apollo-sourced numbers carry no prior express written consent.
Auto-dialing after an ignored email is the exact pattern plaintiffs' firms
scan for. Quorum-gating does not neutralize statutory liability.

**Consequences:**
- The "7-day no-reply → call" step in the original 3-phase plan becomes
  "7-day no-reply → voicemail draft for Alex."
- The callsheet generator (`backend/prospecting/callsheet.py`) still
  produces the script, but it's input to Alex's human read, not to an
  outbound API.
- Strategy workcell's "stalled-Opp → dialer" wiring is forbidden.

**Alternatives rejected:**
- Auto-dial behind quorum gate — see "governance theater" above.
- Operator-confirmation modal per dial — the modal is a tactical mitigation
  on a strategic risk; not enough.

---

## ADR-003 | 2026-05-29 | Defer productization until ≥10 paid closes

**Decision:** No new revenue surface (subscription products, new Stripe
SKUs, $39/mo Posture Monitor) ships until the current funnel has produced
≥10 paid closes. The Expansionist advisor's 10x suggestions are valid
*after* unit validation, not before.

**Why:** A subscription product on top of an unvalidated claim layer
converts one-shot defamation risk into recurring contractually-documented
liability with churn metrics. The Outsider's line lands: "you don't
franchise a restaurant whose kitchen is on fire."

**Consequences:**
- No `samus-subscription` workcell.
- No Stripe SKU additions until the gate clears.
- The Gap Report stays a sales artifact, not a product.

**Alternatives rejected:**
- "Free-tier monitor as lead-magnet" — same liability surface, no closure
  validation underneath.

**Supersedes:** none.
**Superseded by:** none (revisit when ≥10 closes recorded).

---

## ADR-004 | 2026-05-30 | reward_density formula intent

**Decision (intent, not yet built — Guardrail G7):** The reward function
will be:

```
reward = stage_advanced
       − llm_cost_cents
       − k * (retracted_claims + unsubscribes + complaints)
       + terminal_multiplier_on_stripe_payment_intent_succeeded
```

**Why:** Closure-only fitness is what would make Darwin's mutation engine
dangerous — it would breed a manipulation optimizer reinforcing whatever
rhetoric closes deals fastest. Subtracting harm makes the loop
self-correcting. Terminal Stripe multiplier makes actual cash the ground
truth.

**Consequences:**
- Until built, no Darwin-mutation→Samus wiring may ship.
- Harm signals (`retracted_claims`, `unsubscribes`, `complaints`) need
  collection wiring as a prerequisite.
- The `k` coefficient is a tuning knob with caution: too low and harm
  doesn't bite; too high and the reward stops being able to reach positive
  values from any path.

**Status:** Intent. Track as a P0 prerequisite to any Darwin integration.

---

## ADR-005 | 2026-05-30 | The Stake Sentence is the keystone

**Decision:** Every outbound action requires a Stake Sentence —
operator-authored, 40–280 chars, passing the anti-template guard, unique
in the last 100. Outreach refuses to fire without it. The Stake Sentence
renders verbatim in the email body, the Gap Report header, and the
callsheet/voicemail opener.

**Why:** The Council's keystone insight: in a 95% audited environment,
one human sentence at the top reads as unusual deliberation. The
asymmetry between surrounding precision and that one hand-placed sentence
*is* the signal. Vendor→partner conversion through declared chosenness.

**Consequences:**
- `backend/common/stake_sentence_{guard,budget}.py` are new and load-bearing.
- `compose_body` raises before any LLM call if Stake missing.
- Daily Stake cap (default 10) becomes Samus's actual outbound ceiling.
- The Opportunity model carries `stake_sentence`, `stake_sentence_authored_by`,
  `stake_sentence_authored_at`.
- No bypass env var exists or will be added.

**Alternatives rejected:**
- Auto-generated stake from prospect data — defeats the purpose.
- Operator-selected from a list of pre-written sentences — same as templating.
- Optional with a "marked as urgent" override — every shortcut becomes the
  default within 30 days.

**Implementation:** Commits `dccfb694` + `025558c` on
`feat/samus-stake-sentence`.

---

## ADR-006 | 2026-05-30 | `CreateOpportunityRequest.stake_sentence` is OPTIONAL

**Decision:** The Pydantic field `stake_sentence` on
`CreateOpportunityRequest` is **optional** at Opportunity creation time.
Required-at-creation would break the ~24 auto-callers that create
Opportunities programmatically (lead conversion, finance webhook, strategy
attribution).

**Why:** The Opportunity is the **slot**; the Stake Sentence gets attached
afterward. The hard invariant — *outreach refuses to fire without it* — is
enforced at the outbound dispatch gate (ADR-005), which is unconditional.

**Consequences:**
- Auto-creation paths continue to work unchanged.
- `list_opportunities_pending_stake` becomes the canonical queue for Alex.
- Validators still reject banned phrases / length / casing whenever a
  non-empty Stake IS supplied at create time — they just don't reject
  emptiness.

**Alternatives rejected:**
- Required-at-creation with a "system" placeholder default — placeholders
  become indistinguishable from real sentences and pollute audit trails.
- Required-at-creation with a parallel "draft Opportunity" model — doubles
  the surface area for no payoff.

**Surfaced by:** subagent during implementation; correctly identified as a
design call, not a bug.

---

## ADR-007 | 2026-05-30 | Stake Sentence budget fails CLOSED on persistence error

**Decision:** Unlike the LLM budget (which fails open on ledger I/O
failure), the Stake Sentence budget refuses to record when neither DDB nor
JSON ledger is readable/writable. Raises `StakeSentenceBudgetUnavailable`.

**Why:** The LLM budget protects against bounded overspend (cost). The
Stake Sentence cap protects against unbounded outbound (safety). Losing
the cap loses the outbound ceiling, which is the structural enforcer of
proactivity-as-truth-and-warmth (ADR-001).

**Consequences:**
- A failed ledger writes a host-side outage on the entire Stake authoring
  path until fixed.
- Operations playbook needs a `/opt/samus/data/` integrity check.

**Alternatives rejected:**
- Fail-open mirroring LLM budget — same code shape, opposite failure
  semantics, dangerous symmetry.

---

## ADR-008 | 2026-05-30 | Banned-phrase list expansion requires a Codex entry

**Decision:** Additions to `STAKE_SENTENCE_BANNED_PHRASES` require an ADR
entry naming the failure mode the new phrase catches. Removals require an
ADR explaining why the phrase is no longer a template tell.

**Why:** The banned-phrase list is a guard against templating. Without a
process discipline, the list either drifts to laxity (operator removes
phrases that triggered false rejections without auditing) or to overfit
(every false positive in a single bad sentence adds a new ban).

**Consequences:** All list edits are auditable. The list itself is in
`backend/common/stake_sentence_guard.py::STAKE_SENTENCE_BANNED_PHRASES`.

---

## ADR-009 | 2026-05-30 | Gap Report evidence-source enum (intent — G6)

**Decision (intent, not yet built):** Every external-facing claim in a
Gap Report must carry an `evidence_source ∈ {crawled_header, cert, dns,
redirect, public_registry}`. LLM-inferred vulnerability claims rejected
at the serialization layer.

**Why:** Hallucinated vulnerability claims in a document signed-as-fact
are auto-defamation. Crawler-verifiable facts are not.

**Consequences:**
- Audit pipeline must tag each finding with its source at extraction time.
- Report renderer drops untagged findings (fail-closed at render).
- A pytest enforces the schema on a held-out report sample.

**Status:** Intent. The Codex tracks this as a P0 blocker on cranking
outreach throughput.

---

## ADR-010 | 2026-05-30 | Pre-flight legitimacy filter (intent — G8)

**Decision (intent, not yet built):** Outreach refuses to fire on a
prospect with zero warmth signal. Required: ≥1 of {public RFP, Chamber
roster, prior inbound, deterministic public-registry hit}. Cold-cold
prospects divert to a `needs_warm_path` queue.

**Why:** The Outsider's structural critique: software cannot manufacture
referrals, but it can refuse to act without them. This is the
operationalization of "earlier warmth, not faster outbound."

**Consequences:**
- A new prospecting filter and a new queue.
- Alex's job on the warm-path queue: find a signal or drop the prospect.

**Status:** Intent.

---

## ADR-011 | 2026-05-30 | The Codex becomes the runtime source of truth

**Decision:** Build a Codex Validation Layer that parses
`docs/codex/04_guardrails.md`, `08_decisions_log.md`, `10_glossary.md`,
`11_when_to_shut_it_down.md`, and the banned-phrase list at
`backend/common/stake_sentence_guard.py:STAKE_SENTENCE_BANNED_PHRASES`,
exposes a `check_action(ProposedAction) -> Verdict` gate, and is wired
into every workcell's boot path. Violations halt the action and
auto-draft an ADR stub in `docs/codex/_drafts/` that the operator must
resolve.

**Why:** The original Codex was passive documentation. Documentation
drifts. Code grows. The risk the Codex was written to prevent —
*losing ourselves in the complexity of our own genius* — is the risk of
shipping a change that violates a rule we wrote but forgot. An active
runtime check closes that loop. Every proposed outbound or load-bearing
action passes through the validator before it runs.

**Consequences:**
- Three integration hooks in v1: `outreach.compose_body`,
  `seo.render_seo_report_markdown`, the operator console (`GET/POST
  /api/console/codex/{pending_adrs,reload}`).
- App construction (`backend/common/app_factory.create_base_app`) now
  calls `_ensure_codex_loaded` — every workcell refuses to boot if the
  Codex can't be parsed. No env-var bypass.
- A new chapter — [12_the_validation_layer.md](12_the_validation_layer.md)
  — documents the gate, the v1 ruleset, the hook contract, and the
  extension procedure.
- The validator is **narrow**: it gates discrete actions, not ongoing
  state, not code edits, not "vibes." The discipline of re-reading the
  Codex (per ADR-008, F13) is still required.
- The `_drafts/` directory contents are gitignored except `.gitkeep`;
  drafts are local artifacts of operator sessions and must not be
  shared via git.

**Alternatives rejected:**
- **Linter / pre-commit hook only.** Static-analysis-time enforcement
  catches code structure but not runtime payloads (e.g., a banned
  phrase appearing in a personalized email body). The decision was to
  put the gate at the action site, not the code site.
- **Quorum-style cross-agent gate.** Adding Anita/Major to a Codex
  veto would couple Samus's outbound to ecosystem availability. The
  Codex is owned by Samus locally; the gate stays local.
- **Manual review queue with no auto-drafting.** A log entry is a
  passive artifact; a draft file is an active one. Active artifacts
  win.
- **Make the layer optional via env var.** Would break the
  no-bypass-env-var rule. Rejected categorically.

**Supersedes:** none — extends and operationalizes ADR-001, ADR-002,
ADR-003, ADR-005, ADR-008.
**Superseded by:** none.

**Implementation:** Commits on `feat/samus-stake-sentence` —
`2332c25` (package + tests) and the integration commit that adds
this entry.

---

## ADR-012 | 2026-05-30 | Flip G6 / G7 / G8 / G11 from intent to enforced

**Decision:** All four previously-intent guardrails are flipped to
enforced. The backing implementations shipped in parallel work-streams
on 2026-05-30 (commits on `feat/samus-g6-evidence`, `feat/samus-g7-reward`,
`feat/samus-g8-legitimacy`, `feat/samus-g11-apollo`, `feat/samus-codex-infra`,
all merged into `feat/samus-stake-sentence`). Codex Validation Layer rules
`VW-G6 → VR-G6`, `VW-G7 → VR-G7`, `VW-G8 → VR-G8` were promoted to
blocking. G11 is enforced by the budget module's own fail-CLOSED cap-hit;
the Codex layer carries an observation-only hook for audit visibility.

**Why:** ADR-001 / ADR-005 / the Council verdict established that
proactivity along the truth-and-warmth axis requires these four gates.
The intent declarations served their purpose (named the gaps in the
Codex, surfaced VW-* warnings to measure how often they'd fire). The
backing primitives now exist; running with the gates still advisory
would be the same kind of governance theater the Codex was written to
prevent.

**Consequences:**
- `backend/common/codex/validator.py` carries 8 blocking rules (was 5)
  and 0 warning rules (was 3). All tests in
  `tests/test_codex_validator_warning.py` were rewritten to assert the
  blocking outcomes (the file is retained under its existing name for
  forensic continuity).
- `compose_body` now refuses outreach without `legitimacy_signal` (G8)
  in addition to the existing stake-sentence requirement (G1). The
  pre-flight legitimacy filter in `run_campaign._apply_warmth_gate`
  ensures contacts carry the signal before they reach `compose_body`,
  or are diverted to `needs_warm_path`.
- `render_seo_report_markdown` now refuses to render any Gap Report
  whose caller doesn't declare a post-filter `evidence_sources` list
  (empty list permitted; missing key blocks). The `_filter_verified_issues`
  helper is the fail-closed serialization layer.
- `compute_reward` now flows through the Codex with
  `subtracts_harm=True` and persists every computation to
  `host_artifacts/reward_computations.jsonl`. Any future caller updating
  the reward function without `subtracts_harm=True` will be blocked.
- Apollo calls in `apollo_source.py` are wrapped by `apollo_budget` with
  pre-flight `assert_allows` (fail-CLOSED on cap-hit) and
  post-flight `record_spend` (fail-OPEN on ledger I/O). Per-day cap via
  `SAMUS_APOLLO_DAILY_BUDGET_USD` (default `$5.00`).
- Chapter 04 status blocks for G6/G7/G8/G11 are flipped from
  "intent-defined, not yet enforced" to "enforced 2026-05-30 via
  ADR-012."
- Chapter 12 rule table updated; warning-rules section now reads "v1.1:
  none."
- Three EvidenceSource enum values added (`crawled_html`, `crawled_meta`)
  to cover deterministic HTML / meta parser output that isn't LLM-inferred
  but also isn't network-level. The original 8-value enum was too narrow
  to express what the audit pipeline actually produces.
- Hot-reload semantics on `CodexRegistry.reload()` were corrected to be
  fail-OPEN (preserves the previously-parsed state on parse error). The
  boot-time `load()` remains fail-CLOSED.
- `_drafts/_resolved/` workflow + watchdog auto-reload + morning-brief
  surfacing landed in `backend/common/codex/{watchdog,resolution}.py`
  and `backend/morning.py`. Operator routes:
  `POST /api/console/codex/drafts/{name}/resolve` and `/promote`.

**Alternatives rejected:**
- **Flip the rules without shipping the backing implementations.** Would
  break every existing caller with no path forward. The whole point of
  the intent-phase was to measure first.
- **Keep G6 advisory until production data confirms the false-positive
  rate is low.** The Codex's defense-in-depth posture (filter at the
  seo layer + Codex meta-gate) makes the false-positive risk near zero;
  an LLM-inferred claim that escaped the filter would have to escape
  twice. Lower than the residual risk of leaving a defamation vector
  open.
- **Add a `SAMUS_CODEX_INTENT_GATES_ENFORCE=0` bypass for emergency
  operations.** No bypass env var on any gate path. Categorically.

**Supersedes:** none — extends ADR-001, ADR-004, ADR-005, ADR-009,
ADR-010, ADR-011.
**Superseded by:** none.

**Implementation:** Commits on `feat/samus-stake-sentence` —
`cd2db9f` (G6), `1d7a61b` (G7), `a30f5bf` + `c4c8878` (G8), `81d6e0a` (G11),
`d0dc67f` + `47adfbc` (Codex infra). The consolidating commit on
`feat/samus-stake-sentence` carries the rule flips, the validator-warning
test rewrite, fixture updates, and this ADR.

---

## ADR-013 | 2026-06-24 | ALLOW outreach_send under VR-G8, bounded by the warmth signal it enforces

**Decision:** The `outreach_send` action blocked by `VR-G8` on
2026-06-23 (auto-draft `ADR-013_outreach-send.draft.md`) is **ALLOWED**.
This is **not** a relaxation of G8. `outreach_send` remains permitted
*only* for a payload that carries a non-empty `legitimacy_signal` — the
exact invariant `VR-G8` exists to enforce (ADR-010, ADR-012). The block
fired because the proposed action presented a stake sentence with no
resolved warmth signal attached; the operator's decision is that such an
action is permitted **once the pre-flight warmth gate has supplied the
signal**, and forbidden otherwise. The allowance is the normal
G8-compliant send path, recorded here so the auto-drafted block resolves
to an explicit, bounded ALLOW rather than lingering as an open draft.

**Why:** `VR-G8` is doing its job — it refused an outbound that had not
yet passed the legitimacy filter. The decision the Codex demanded was not
"should outreach exist" (ADR-005/ADR-010 already settled that) but
"under what bound is *this* send permitted." The bound is the warmth
signal itself: an `outreach_send` is truth-and-warmth-aligned (ADR-001)
precisely when the prospect carries ≥1 of `{public_registry,
chamber_roster, prior_inbound, rfp, open_job_listing}`. Encoding the
ALLOW as "permitted *with* the signal, never *without* it" keeps the
gate load-bearing while clearing the specific blocked action.

**Consequences:**
- No code or rule change. `VR-G8` stays blocking; `compose_body` still
  refuses any `outreach_send` whose payload lacks `legitimacy_signal`
  (chapter 04 §G8, chapter 12 rule table). The warmth gate
  (`backend/outreach/run_campaign.py::_apply_warmth_gate`) remains the
  populator of record, and cold-cold prospects still divert to
  `needs_warm_path`.
- **New accompanying safeguard (send-quality):** a cold-send
  email-quality gate now sits *in front of* the G8-cleared compose path,
  strengthening send quality independently of the warmth signal.
  `is_cold_sendable_email` in
  `backend/prospecting/contact_validation.py` is the single canonical,
  fail-closed predicate — an address must be structurally deliverable
  (RFC-1035 hostname rules) **and** must not be a role/system/department
  mailbox (`info@`, `sales@`, `bugreport@`, …) to be eligible as a cold
  `to`. `backend/outreach/cash_engine_entry.py::_select_cold_send_email`
  consults it: it prefers a cold-sendable `owner_email`, else the first
  cold-sendable address in `contact_emails`, else returns `""` and
  `compose_initial_packet` raises `OutreachStakeMissing` (fail-closed) so
  a non-cold-sendable contact is never sent. This protects the SendGrid
  sender-domain reputation — a send-quality concern orthogonal to, and
  additive on top of, the G8 warmth requirement.
- **Failure modes introduced:** (1) a prospect that legitimately has only
  a role mailbox (`info@` for a one-person shop) is now refused at the
  cold-send selection step even when it carries a valid warmth signal —
  the contact must be re-sourced with a personal address or worked via a
  warm path; this is an accepted false-negative in favour of
  domain-reputation safety. (2) The scraped address remains on the record
  untouched, so operator-facing surfaces may still display a role mailbox
  that the engine will not cold-send to; the divergence is intentional
  (storage ≠ cold-send eligibility) but must not be misread as "we will
  email this."

**Alternatives rejected:**
- **REJECT (option B — modify code so the action no longer triggers
  VR-G8).** Rejected: the action *should* trigger VR-G8 whenever the
  signal is absent — that is the gate working, not a bug. Suppressing the
  trigger would defeat ADR-010/ADR-012.
- **Broad ALLOW that permits `outreach_send` without `legitimacy_signal`.**
  Rejected categorically: it would gut G8 and re-open the cold-cold
  outbound vector the Outsider's critique (ADR-010) closed. The ALLOW is
  bound to the signal, full stop.
- **Add a `legitimacy_signal` bypass env var for the blocked action.**
  Rejected — violates the no-bypass-env-var rule (ADR-005, ADR-011,
  ADR-012).

**Supersedes:** none — operationalizes ADR-010 and ADR-012 by resolving a
concrete `VR-G8` block; the G8 rule and its enforcement are unchanged.
**Superseded by:** none.

**Resolution provenance:** Promoted from
`docs/codex/_drafts/ADR-013_outreach-send.draft.md` (auto-drafted
2026-06-23 by `backend/common/codex/adr_drafter.py` under `VR-G8`).
Operator decision: ALLOW (with constraints). Resolved draft retained at
`docs/codex/_resolved/ADR-013_outreach-send.resolved.md`.

---

## ADR-014 | 2026-06-24 | ALLOW voice_dial in principle — activation DEFERRED pending Twilio compliance + number provisioning

**Decision:** The `voice_dial` action blocked by `VR-G5` (auto-draft
`ADR-013_voice-dial.draft.md`, triggered 2026-06-05) is resolved as
**ALLOW IN PRINCIPLE, OPERATIONALLY GATED**. The operator accepts
`voice_dial` as a *future* capability, but its activation is **DEFERRED**
and **does not turn on now**. Activation is gated on two external
conditions, both of which are currently unmet:

  1. **Twilio compliance clearance**, and
  2. **purchase/provisioning of the outbound phone number(s)**
     (`vapi_phone_number_id`).

Until **both** clear, the voice leg remains exactly what ADR-002
mandates: a **voicemail-draft artifact** plus, at most, the
limited/operator-initiated Vapi calls the unprovisioned account permits.
**Autonomous dialing stays disabled.** This ADR does **not** enable
autonomous dialing and must not be read as doing so.

**Why:** ADR-002 removed the auto-dialer because Apollo-sourced numbers
carry no prior express written consent and autonomous dialing is
$500–$1,500/call TCPA exposure. That risk analysis is unchanged. The
operator's forward-looking decision is that a *compliant, number-provisioned,
consent-respecting* voice capability could exist later — but the
preconditions for compliance (Twilio clearance) and for technical
capability (purchased numbers; Vapi can place only limited calls until
then) are not yet satisfied. Recording an explicit, bounded
ALLOW-but-DEFERRED is the honest resolution of the draft: it neither
pretends the capability is live nor leaves the block dangling. The gate
that keeps us safe in the interim is, deliberately, **still VR-G5**.

**Consequences:**
- **No code or rule change. `VR-G5` remains blocking and UNCONDITIONAL.**
  `backend/common/codex/validator.py` still refuses every
  `action_kind == "voice_dial"`; `backend/voice/autonomous.py`'s
  double-fence (flag + Codex gate) is untouched and remains
  effectively dead-code-by-design — `attempt_autonomous_dial` continues
  to be refused by the Codex on every path. No `SAMUS_AUTODIAL_ENABLED`
  flag exists or is added (ADR-002).
- **Activation is a future operator action, not a config toggle reachable
  today.** Lifting the deferral will itself require a *new* ADR that (a)
  records Twilio compliance clearance, (b) records number provisioning,
  (c) specifies the consent basis per dial, and (d) defines how `VR-G5`
  is narrowed (e.g. operator-initiated, per-call, consent-gated) without
  re-opening autonomous dialing. This ADR explicitly does **not** grant
  any of that.
- Until then: the callsheet/voicemail-draft path
  (`backend/prospecting/callsheet.py`, the voice service's voicemail
  artifact) is the only voice work product; the operator console
  (`backend/voice/console.py`) may place single, human-initiated Vapi
  calls only to the extent the unprovisioned/compliance-pending account
  allows.
- **Failure mode guarded against:** the chief risk of an "ALLOW" verdict
  is that it is misread as "dialing is on." This ADR is written to make
  that misreading impossible — the decision is ALLOW *in principle*,
  activation DEFERRED, autonomous dialing DISABLED, gate VR-G5 INTACT.

**Alternatives rejected:**
- **REJECT (option B — delete the draft / modify code so voice_dial no
  longer triggers VR-G5).** Rejected: the trigger is correct (ADR-002),
  and the operator wants the principle of a future compliant capability
  on record rather than erased.
- **ALLOW and activate now.** Rejected hard: Twilio compliance is not
  cleared and no numbers are provisioned; activating would be both
  non-functional (Vapi limited-call only) and a live TCPA exposure.
  Forbidden until the preconditions in this ADR are met and a successor
  ADR lifts the deferral.
- **Add a `SAMUS_AUTODIAL_ENABLED` / voice bypass flag staged for "when
  ready."** Rejected categorically — ADR-002 forbids the flag's
  existence, and a staged bypass is the exact shortcut-becomes-default
  failure mode (ADR-005).

**Supersedes:** none — defers to and preserves ADR-002 (and its
enforcement via `VR-G5`, chapter 04 §G5, chapter 12 rule table) in full.
**Superseded by:** none — a future ADR lifting the deferral (post-Twilio
compliance + number provisioning) would supersede this one.

**Resolution provenance:** Promoted from
`docs/codex/_drafts/ADR-013_voice-dial.draft.md` (auto-drafted 2026-06-05
by `backend/common/codex/adr_drafter.py` under `VR-G5`). Provisional
draft number 013 renumbered to ADR-014 to avoid collision with the
outreach_send promotion (per `next_real_adr_number` sequencing). Operator
decision: ALLOW in principle, activation DEFERRED/GATED. Resolved draft
retained at `docs/codex/_resolved/ADR-013_voice-dial.resolved.md`.

---

## ADR-016 | 2026-07-02 | LIFT the voice_dial deferral — governed autonomous dial policy (default-OFF)

**Decision:** Supersede ADR-014's activation deferral. `voice_dial` stays BLOCKED
by `VR-G5` by default, but the Codex now ALLOWS it under a single, fully-attested
**governed autonomous dial policy**, gated behind `governed_autonomous_dial_enabled`
(default **OFF**). The block stands unless EVERY condition holds:

1. the policy is armed (`governed_autonomous_dial_enabled=true`);
2. `payload.policy == "governed_autonomous_dial"` (explicit policy invocation);
3. `payload.stake_sentence` present (operator-authored, consistent with G1);
4. the actuation path attests it enforced the fences — `within_call_hours`,
   `cooldown_ok`, `under_daily_cap`, `dnc_ok` are each literally `True`;
5. no banned phrase (G2 still applies over the payload text).

Samus's control-tick idle-production drive may self-initiate dials ONLY through
this policy: the voice actuation computes + enforces the fences BEFORE building
the attested action, and the Codex ratifies the attestation. Absent any single
condition the Codex returns the same VR-G5 block as before. The `voice_dial`
capability defaults exactly as it does today (blocked); arming is an explicit,
reversible operator act (flip the flag off = instant hard block again).

**Why:** ADR-014 accepted autonomous dialing in principle but deferred activation
pending Twilio compliance + number provisioning. Those are now met (two
Twilio-owned numbers live, Morgan bound). The operator has decided that pacing
production toward its goals must be the agent's own reasoning, not an external
prompter — so the agent needs a governed path to self-initiate calls, fenced by
the same protections a responsible human dialer would honor (call-hours, per-number
cooldown, a daily cap, DNC, and an operator-authored stake).

**Consequences:** A new `governed_autonomous_dial_enabled` Settings flag (default
False). `validator._check_blocking` makes `voice_dial` conditional (default-closed
`_governed_dial_permitted`). `voice.autonomous.attempt_autonomous_dial` builds the
attested payload (enforcing the fences) and, when the Codex allows, places one live
call via the dialer's `create_call` path. Dormant on ship; nothing dials until the
flag is armed. Fail-closed everywhere: missing attestation, unarmed flag, or an
unavailable Codex all block.

**Alternatives rejected:** (a) Keep VR-G5's unconditional block — rejected: leaves
calling permanently operator-triggered, which the operator explicitly does not want.
(b) External Windows/scheduled dialer — rejected: trades one external prompter for
another; the decision must live in the agent. (c) Unfenced autonomous dialing —
rejected: TCPA/reputation/cost risk; the whole point is the fence set.

**Supersedes:** ADR-014's activation deferral (preserves ADR-002's intent via the
mandatory fence set — a dial is only ever placed inside all of them).
**Superseded by:** ADR-017 (adds the mandatory `consent_ok` fence — see there for
the honest reconciliation with ADR-002; ADR-016's claim to "preserve ADR-002's
intent via the fence set alone" was incomplete, since call-hours/cooldown/cap/DNC
do not create consent).

---

## ADR-017 | 2026-07-02 | Consent-gate the governed dial — cold numbers get a voicemail draft, not a live call

**Decision:** Extend ADR-016. The governed autonomous dial policy gains a FIFTH
mandatory fence, `consent_ok`, and the actuator + Codex both require it literally
`True` alongside `within_call_hours`, `cooldown_ok`, `under_daily_cap`, `dnc_ok`.
A live autonomous call is placed ONLY to a number with a lawful consent basis
(inbound/opted-in lead, or existing customer relationship). A prospect WITHOUT
that basis — e.g. a cold Apollo-sourced number — is never live-dialed by the
agent; the idle-production drive routes it to the ADR-002 **voicemail-draft
artifact** for Alex instead.

**Why (the honest reconciliation with ADR-002):** ADR-002 removed the auto-dialer
because Apollo cold numbers carry no prior express written consent, and TCPA
liability ($500–$1,500/call) attaches to the *absence of consent* — fences on
timing/frequency do not neutralize it. ADR-016 relaxed VR-G5 but its fence set
did not address consent, so on its own it re-introduced exactly the liability
ADR-002 identified. The operator's decision (2026-07-02): autonomous LIVE calls
only where consent exists; everything else stays a voicemail draft. `consent_ok`
makes that a structural precondition of the policy, not a producer-level courtesy
— so no code path can place a live cold call even if the drive is armed.

**Consequences:** `_REQUIRED_FENCES` in `backend/voice/governed_dial.py` and the
`_governed_dial_permitted` check in `validator.py` both add `consent_ok`. A
prospect's consent basis is classified BEFORE actuation; `consent_ok=False` (or
absent) blocks the live dial (local `FENCE` block, Codex never consulted) and the
producer generates a voicemail draft instead. ADR-002 remains fully in force for
every non-consented number. Dormant on ship; arming still an explicit operator act.

**Alternatives rejected:** (a) Unconditional live governed dial for all fenced
prospects incl. cold numbers — rejected: re-introduces the ADR-002 liability. (b)
Voicemail-draft only, no autonomous live calls ever — rejected: forgoes the
legitimate autonomous close on warm/inbound leads where consent is present. (c)
Keep consent as a producer-only check — rejected: it must be a policy-level fence
so no future caller can bypass it.

**Supersedes:** none (extends ADR-016; upholds ADR-002 for non-consented numbers).
**Superseded by:** ADR-018 (adds an operator-authorized B2B consent basis that
satisfies `consent_ok` for business-listing prospects under the operator's
express authority; ADR-017's fence stays mandatory — ADR-018 supplies a lawful
True path for it, it does not remove or weaken it).

---

## ADR-018 | 2026-07-03 | Operator-authorized B2B consent basis for the governed dial

**Decision:** The `consent_ok` fence (ADR-017) remains mandatory and unchanged.
ADR-018 adds ONE lawful way to satisfy it for cold B2B outreach: the operator's
DAILY PRE-SHIFT ATTESTATION. By the operator's own design, running the pre-shift
preparation is the operator's acknowledgement of what the agent will execute
that business day — so a successful pre-shift briefing for the current business
day stamps a durable attestation record, and while (a) the governed-dial policy
is armed (`governed_autonomous_dial_enabled=true`, the ADR-016 master switch) AND
(b) today's pre-shift attestation is present, a prospect that is a BUSINESS
record (Google Places business listing — company name + a business phone)
classifies `consent_ok=True` under the operator's authority as the principal of
HustleForge LLC. The attestation is FRESH DAILY and expires: no pre-shift run
today ⇒ no attestation ⇒ every prospect falls back to voicemail draft (ADR-002).
This makes consent an affirmative, recurring operator act rather than a
set-and-forget flag. Every OTHER fence stays hard and unchanged:
`within_call_hours` (TCPA 8–21 prospect-local), `dnc_ok` (DNC/suppression
fail-closed), `cooldown_ok` (7-day per-number floor), `under_daily_cap` (the
per-prospect not-called-today frequency check), and the operator-authored
`stake_sentence` (G1) with no banned phrase (G2). The VOLUME ceiling on live
calls is NOT a new fixed cap — it is the existing CODB / affordability reasoner:
a live governed dial is a PAID cost tier (per-call Vapi + Twilio), so the
campaign portfolio's affordability gating (posture conserve/lean/invest +
`marketing_budget_usd` headroom after the CODB safety reserve) bounds how many
calls fire, exactly as it bounds every other paid channel. Under a conserve
posture or an exhausted budget the voice channel falls back to free voicemail
drafts (no live dial). Two additional operator
directives are structural: **no call-backs** (governed autonomous calls never
enqueue a voicemail/no-answer re-dial — `SAMUS_VOICE_NO_CALLBACK_ENABLED`) and
**in-call self-monitoring** (mid-call adaptation + the intraday session monitor
are armed so the agent audits and improves outcomes live). Arming is an
explicit, reversible operator act; flipping the flag off restores ADR-017's
cold-number-voicemail-only behavior instantly.

**Why:** ADR-017 correctly made `consent_ok` mandatory because TCPA liability
attaches to the absence of consent, and it left ONE question open — what
constitutes a lawful basis. For consumer calling, opt-in. For business-to-
business calling to a company's published business number, the operator, as the
principal who bears the liability, may authorize it as a matter of their own
authority and risk acceptance — which is exactly the human authority the HOTL
model reserves for the operator. The operator gave that authorization expressly
(2026-07-03): live B2B calls within business hours, all other guardrails honored,
no call-backs, live self-monitoring, full logging. ADR-018 encodes that as a
scoped, revocable consent basis rather than a bypass, so the fence still governs
every dial and no future caller can reach a live call outside these conditions.

**Consequences:** `run_pre_shift_briefing` writes a durable per-business-day
attestation (`backend/common/preshift_attestation.py` →
`<state_root>/cognition/preshift_attestation_<business_date>.json`). The
attestation is consumed by the CANONICAL cold-dial executor,
`backend/gateway/cold_dial_task.py` — an in-container loop (sibling of
control_tick / morning_ritual) that, when production is armed AND today is
attested AND inside the Pacific dial window AND under the daily-cap headroom AND
a call list exists, restores the operator's PROVEN cold path by delegating to
`dialer.dial_call_list` over the signed mesh (voice holds the Vapi creds; the
gateway does not). It does NOT fork a parallel dialer and does not bypass a
single per-prospect fence (TCPA hours, DNC fail-closed, per-number cooldown,
already-called-today, per-run cap). The idle-drive's own voice lane stays
consent-routed (ADR-002/017): a cold prospect there is a voicemail DRAFT, never a
live call — so there is exactly ONE cold live-dial lane, no double-dial. (An
earlier revision briefly re-classified cold B2B as "consented" inside the
idle-drive, creating a second overlapping live-dial path; that was removed
2026-07-08 in favour of the single `cold_dial_task` lane.) The no-callback
directive is honoured via `SAMUS_VOICE_NO_CALLBACK_ENABLED` / a call's
`metadata.no_callback`, which makes the end-of-call handler skip the retry
enqueue.
The end-of-call handler skips the retry-queue enqueue when `no_callback` is set
or `SAMUS_VOICE_NO_CALLBACK_ENABLED` is on. Mid-call (`SAMUS_VOICE_MIDCALL_
ENABLED`) + session monitor (`SAMUS_VOICE_SESSION_MONITOR`) armed. All existing
voice logging (voice_events.jsonl full ledger, hashed voice_audit.jsonl,
dial_runs, transcript ingest) is unchanged and captures every call. Dormant on
ship; nothing dials until the operator arms the flags.

**Alternatives rejected:** (a) Disable the `consent_ok` fence — rejected: a
fence removed is a fence no future caller must satisfy; keep it and supply a
lawful True path instead. (b) Apply operator authority to ALL numbers including
detected mobiles — the operator chose the broad B2B business-number scope
(2026-07-03) with all other fences hard; a mobile-exclusion refinement can be a
later ADR if desired. (c) Wait for a warm/opt-in pipeline — rejected by the
operator: it forgoes the authorized B2B calling they expressly want now.

**Supersedes:** none (extends ADR-016/017; upholds ADR-002's protection for any
number the operator has NOT authorized and for every non-business record).
**Superseded by:** none.

---

## ADR-019 | 2026-07-10 | ALLOW governed voice_dial when all fences pass — confirms ADR-016/017/018 policy resolves VR-G5

**Decision:** The `voice_dial` action blocked by `VR-G5` (auto-draft
`ADR-018_voice-dial.draft.md`, triggered 2026-07-03) is resolved as
**ALLOW**. A `voice_dial` with `policy == "governed_autonomous_dial"` and
all 5 mandatory fences attested (`stake_sentence` present,
`within_call_hours == True`, `cooldown_ok == True`,
`under_daily_cap == True`, `dnc_ok == True`, `consent_ok == True`)
satisfies VR-G5 through the governed dial policy enacted in
ADR-016/017/018. This ADR confirms that the governed dial policy — when
fully attested — is the legitimate resolution path for VR-G5 blocks on
individual dial attempts.

**Why:** ADR-016 lifted ADR-014's activation deferral by introducing the
governed autonomous dial policy (default-OFF). ADR-017 added the mandatory
`consent_ok` fence. ADR-018 supplied the operator-authorized B2B consent
basis via daily pre-shift attestation. The auto-draft fired on 2026-07-03
— the same day ADR-018 was enacted — because the validator code had not
yet incorporated the governed policy's conditional allowance at the time
of the proposed action. The draft represents a correctly-fenced dial that
the policy was designed to permit. Resolving it as ALLOW confirms that
the policy chain (ADR-016→017→018) works end-to-end and that VR-G5's
conditional path (`_governed_dial_permitted`) is the intended resolution
mechanism for attested governed dials.

**Consequences:**
- No code or rule change. `VR-G5` remains blocking by default; the
  `_governed_dial_permitted` conditional in `validator.py` continues to
  check every fence. An unarmed policy
  (`governed_autonomous_dial_enabled=false`) or any missing/false fence
  still blocks.
- This ADR establishes precedent: future `voice_dial` auto-drafts that
  carry a fully-attested governed-dial payload and fire while the policy
  is armed should be resolved by reference to this ADR and
  ADR-016/017/018, not treated as novel violations.
- The daily pre-shift attestation (ADR-018) remains the consent gate —
  no attestation = no `consent_ok` = VR-G5 blocks.

**Alternatives rejected:**
- **REJECT (delete draft, modify code).** Rejected: the action is
  legitimate under the governed policy. Suppressing draft-generation for
  governed dials is a separate code improvement, not a reason to reject
  the action.
- **ALLOW without confirming the fence set.** Rejected: the ALLOW is
  bound to the full fence attestation. A partial attestation must still
  block.

**Supersedes:** none — confirms and operationalizes ADR-016/017/018.
**Superseded by:** none.

**Resolution provenance:** Promoted from
`docs/codex/_drafts/ADR-018_voice-dial.draft.md` (auto-drafted
2026-07-03 by `backend/common/codex/adr_drafter.py` under `VR-G5`).
Draft number ADR-018 renumbered to ADR-019 to avoid collision with the
real ADR-018 (operator-authorized B2B consent basis). Operator decision:
ALLOW. Resolved draft retained at
`docs/codex/_resolved/ADR-018_voice-dial.resolved.md`.

---

## ADR-020 | 2026-07-10 | Add an internal, no-handoff tax-categorization and tax-minimization engine to the finance workcell

**Decision:** Samus gains a tax-categorization + tax-strategy capability
that (1) tags every bank transaction and CODB line item with a Schedule C
tax category via a versioned, codified ruleset (no LLM-freeform guessing),
(2) discovers applicable deductions and tax breaks and surfaces them as
reviewable recommendations, (3) projects running federal + CA estimated
tax liability against real quarterly due dates, and (4) recommends
spending/saving/disbursement timing that minimizes tax liability without
ever risking runway. No CPA is in the loop by design — Samus is the sole
tax-categorization authority for this LLC.

**Operator inputs (answered 2026-07-10):**
1. Entity classification: **disregarded entity** (single-member LLC,
   Schedule C on personal 1040, no 8832/2553 filed)
2. Filing status: **Head of Household**
3. Home-office: **300 sqft, simplified method** ($5/sqft = $1,500/yr max)
4. 1099 contractors: **none in 2026**
5. Scope: **federal + CA only**

**Why:** The operator explicitly rejected a CPA-handoff model and wants
this handled internally, end-to-end — and the LLC already has a live,
overdue compliance obligation (CA LLC-12 + $800 franchise tax). Existing
finance surfaces (CODB, bank_activity, Stripe) already capture every
dollar the company moves; the missing layer is tax-relevant
classification and strategy, not new financial data ingestion.

**Consequences:** New `tax_category` field on `BankTransaction` and
`CodbItem` (backward-compatible default `""`). New versioned ruleset
`backend/finance/tax_rules_<year>.yaml`. New pure-logic
`backend/finance/tax_categorizer.py` (every category assignment cites
the matched rule — `evidence_source.py` precedent). New pure-math
`backend/finance/estimated_tax.py` (SE tax, HoH brackets, CA franchise
tax tiers, quarterly due dates). New RECOMMEND-ONLY
`backend/cognitive/tax_reasoner.py` mirroring `codb_reasoner.py` — reads
finance data through existing accessors, writes every recommendation
through `GuidanceLedger` (PROPOSE → operator ACCEPT/REJECT), never calls
a disbursement, filing, or payment path. New `TAX` section in
`backend/morning.py` daily brief. Any action that would move real money
or file anything with the IRS/FTB routes through the existing HOTL
approval queue at high severity. No new top-level package; every new
module is additive under `backend/finance/` or `backend/cognitive/`.

**Alternatives rejected:** (a) CPA-handoff — rejected per explicit
operator instruction. (b) Full autonomy including auto-filing and
auto-disbursement — rejected: filing and money-movement are irreversible,
legally consequential actions. (c) LLM-freeform transaction
categorization — rejected for auditability: a wrong deduction claim is a
real financial/legal risk; the `evidence_source.py` precedent already
established this principle. (d) New top-level `backend/tax/` workcell —
rejected as unnecessary parallel-system risk.

**Supersedes:** none.
**Superseded by:** none.

**Resolution provenance:** Promoted from
`docs/codex/_drafts/ADR-020_tax_categorization_engine.draft.md`
(manually authored 2026-07-07 by Claude Opus 4.7 at operator request).
Operator decision: ALLOW. All 5 required operator inputs answered
2026-07-10. Resolved draft retained at
`docs/codex/_resolved/ADR-020_tax_categorization_engine.resolved.md`.

---

## Template for new ADRs

```
## ADR-NNN | YYYY-MM-DD | <One-sentence decision>

**Decision:** <what was decided>

**Why:** <the reason — the load-bearing one>

**Consequences:** <what changes in the system>

**Alternatives rejected:** <options considered and why they lost>

**Supersedes:** <prior ADR if any>
**Superseded by:** <future ADR if and when>
```

Drop new entries at the bottom. Never renumber. If an ADR is wrong,
write a new one that supersedes it.
