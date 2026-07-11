# 12 — The Validation Layer

> *The Codex stops being a document the moment it becomes a runtime check.*

This chapter documents the **Codex Validation Layer** — the package and
the discipline that makes the rest of this Codex active rather than
descriptive. Without this layer, chapters 01–11 are commentary about what
*should* be true. With it, they are what the system *enforces* before any
load-bearing action runs.

---

## Why this exists

The Council's verdict ([chapter 02](02_council_verdict.md)) reframed
proactivity. The Stake Sentence ([chapter 03](03_stake_sentence.md))
operationalized the reframe at the human-input layer. The Guardrails
([chapter 04](04_guardrails.md)) catalog the safety gates. Decisions Log
([chapter 08](08_decisions_log.md)) records why each gate exists.

But documentation drifts. Code grows. New surfaces get added. The risk
the Codex was written to prevent — *losing ourselves in the complexity of
our own genius* — is the risk of shipping a change that violates a rule
we wrote but forgot.

The Validation Layer closes that loop: every proposed outbound or
load-bearing action passes through `check_action()` before it executes.
Violations halt and demand an ADR.

---

## The package

`backend/common/codex/` is the implementation. Public API exposed at the
top level:

```python
from backend.common.codex import (
    REGISTRY,           # singleton registry of parsed rules
    CodexRegistry,      # class for testing or alternate dirs
    ProposedAction,     # what a workcell proposes
    Verdict,            # what the validator returns
    check_action,       # the gate
    CodexViolation,     # raised when blocked (validator raises explicitly)
    CodexUnavailable,   # raised when the registry hasn't loaded
    CodexParseError,    # raised when a chapter fails to parse
)
```

### Modules

| Module | WHY |
|---|---|
| `parser.py` | Reads chapters 04 / 08 / 10 / 11 + imports banned phrases from `stake_sentence_guard.py`. Regex-driven, strict on section presence. |
| `registry.py` | Module-level singleton (`REGISTRY`) with `load()` / `reload()` / `is_loaded()`. Latches on parse failure so a single broken Codex doesn't loop. |
| `models.py` | Pydantic models: `Guardrail`, `ADR`, `ShutdownSignal`, `BannedPhrase`, `ProposedAction`, `Verdict`. |
| `validator.py` | The `check_action(ProposedAction) -> Verdict` function. Encodes the v1 ruleset (below). |
| `adr_drafter.py` | Auto-writes `docs/codex/_drafts/ADR-NNN_<slug>.draft.md` when a check blocks. |
| `exceptions.py` | `CodexUnavailable`, `CodexViolation`, `CodexParseError`. |

---

## The v1 ruleset

### Blocking rules (`Verdict.allowed = False`)

| ID | Rule | Trigger | Reason cited |
|---|---|---|---|
| **VR-G5** | No autonomous voice dial | `action_kind == "voice_dial"` | ADR-002 forbids autonomous dialing |
| **VR-ADR-003** | No subscription creation | `action_kind == "subscription_create"` | ADR-003 defers productization until ≥10 paid closes |
| **VR-G1** | Outreach requires Stake Sentence | `action_kind == "outreach_send"` AND payload.stake_sentence empty | Guardrail G1 |
| **VR-G2** | No banned phrase in user-authored text | Banned-phrase substring (case-insensitive) in any of: `stake_sentence`, `subject`, `body`, `voicemail_script`, `opener` | Guardrail G2 (matched phrase named) |
| **VR-G6** | Gap Report must declare evidence_sources | `action_kind == "gap_report_render"` AND key `evidence_sources` ABSENT (empty list permitted) | Guardrail G6 — flipped from VW-G6 by ADR-012 |
| **VR-G7** | Reward update must subtract harm | `action_kind == "reward_function_update"` AND `subtracts_harm != True` | Guardrail G7 — flipped from VW-G7 by ADR-012 |
| **VR-G8** | Outbound must carry legitimacy signal | `action_kind == "outreach_send"` AND `legitimacy_signal` missing/empty | Guardrail G8 — flipped from VW-G8 by ADR-012 |
| **VR-ADR-008** | No runtime mutation of banned-phrase list | `action_kind == "other"` AND `payload.target == "STAKE_SENTENCE_BANNED_PHRASES"` | ADR-008 |

### Warning rules (`Verdict.allowed = True` + warning text)

**v1.1: none.** All intent-only guardrails (G6 / G7 / G8) were flipped to
blocking in ADR-012 (2026-05-30) once their backing implementations
shipped. The `_check_warnings` function is retained for future
warning-only rules but currently returns an empty list.

### Observation-only hooks

| Hook | Trigger | Purpose |
|---|---|---|
| Apollo audit | `action_kind == "other"` AND `payload.target == "apollo_call"` | Every Apollo API call passes through `check_action` so calls are visible to the validator for audit. No blocking rule — `apollo_budget` is the enforcer (G11). |

### Aggregation

When multiple rules fire on a single action, the alphabetic-first rule ID
becomes the primary blocking violation; the rest are added to
`Verdict.warnings` so a single action audit shows everything that was
wrong.

---

## The boot contract

**Every workcell loads the Codex at app construction.** This is wired
into `backend/common/app_factory.py::_ensure_codex_loaded`, called from
`create_base_app`. If the Codex can't be parsed, the workcell **refuses
to boot**. There is no env var to disable this — the boot failure is the
correct response to a Codex parse error.

In tests, `tests/conftest.py` loads the registry once at session start.
A failed load is logged but does not halt the test session (so tests
that don't touch integrated paths can still run); tests that DO call
into integrated paths will raise `CodexUnavailable` per the fail-closed
contract.

---

## The hooks (v1)

Three integration sites in v1. More can be added as the Codex grows.

### 1. `backend/outreach/campaign.py::compose_body`

After the existing Stake-Sentence guard and CAN-SPAM checks, the Codex
runs a full `check_action` on the composed payload. Catches:
- Banned phrases in `subject` or `body` (which the stake-only guard
  wouldn't see).
- Missing `legitimacy_signal` (VW-G8 advisory — logged, not blocked).

On block: raises `OutreachStakeMissing` with the violated rule ID and
the path to the auto-drafted ADR.

### 2. `backend/seo/report.py::render_seo_report_markdown`

Codex `check_action` runs at the top of every Gap Report render. Today
this is **advisory-only** — VW-G6 logs a warning when no
`evidence_sources` are attached. When G6 flips to enforced, the rule
ID changes from `VW-G6` to `VR-G6` and the same callsite blocks
rather than warns.

### 3. Operator console

| Route | Purpose |
|---|---|
| `GET /api/console/codex/pending_adrs` | List ADR drafts auto-generated by violations |
| `POST /api/console/codex/reload` | Re-parse the Codex after editing chapters (without restarting workcell) |

---

## When a violation fires

The end-to-end loop:

1. **A workcell proposes an action.** It constructs a `ProposedAction`
   with `service`, `capability`, `action_kind`, `payload`, `proposed_by`,
   `correlation_id` and calls `check_action(action)`.
2. **The validator runs.** Iterates the v1 ruleset. Collects every
   matching rule.
3. **If anything blocks**, the validator:
   - Calls `adr_drafter.draft_adr_for_violation(...)` to write
     `docs/codex/_drafts/ADR-NNN_<slug>.draft.md`.
   - Returns `Verdict(allowed=False, violated_rule_id=..., reason=...,
     drafted_adr_path=...)`.
4. **The workcell raises.** The caller never executes the proposed
   action. The exception carries enough context for the operator to
   find the draft.
5. **The operator decides.** Either:
   - **ALLOW** — author a real ADR in `08_decisions_log.md` documenting
     why the action is permitted, what new gates accompany it, what new
     failure modes it introduces. Move the draft to `_resolved/`.
     Reload via `POST /api/console/codex/reload`. Re-run the workcell.
   - **REJECT** — modify the code so the action no longer triggers the
     violation. Delete the draft.

This is the "stop and require an ADR" discipline. The validation layer
is the enforcer; the human is still the decider.

---

## Why blocking auto-drafts an ADR (and doesn't just log)

A log entry is a passive artifact. The operator may or may not see it,
may or may not act on it. A draft file in `_drafts/` is an active
artifact — it's a thing on disk waiting for resolution. The console
endpoint surfaces the count. The next read of the Codex by Alex (or by
the next pass of automation) sees that there are unresolved violations
and treats them as such.

The draft itself is named so it sorts: `ADR-NNN_<slug>.draft.md`. The
`NNN` increments off the highest ADR number found in the registry, so a
resolved draft can move to `08_decisions_log.md` keeping its number
without renumbering.

---

## What's tracked, what's gitignored

- **Tracked:** `docs/codex/_drafts/.gitkeep` — so the directory exists in
  every clone.
- **Gitignored:** `docs/codex/_drafts/*.draft.md` — drafts are local
  artifacts of a specific operator session. Sharing them via git would
  mean every clone carries every contributor's open violations.
- **Tracked (when resolved):** `docs/codex/_resolved/ADR-NNN.md` — the
  archived draft, after its decision has been promoted into
  `08_decisions_log.md`. Kept for forensic audit.

---

## Boundaries of v1

The validator is intentionally **narrow** in v1:

- It checks **discrete actions**, not ongoing system state. It cannot
  detect "the codebase has drifted from chapter 06."
- It enforces **named rules**, not implicit values. "This sentence feels
  spammy" is not a rule it can check; a banned-phrase substring is.
- It runs **at action time**, not at code-edit time. A PR that removes a
  guardrail check from the codebase is not blocked by the validator;
  that's what `09_failure_modes.md` F13 (Codex stops getting updated)
  protects against — humans re-reading the Codex.
- It assumes **the Codex is the source of truth**. If a chapter is
  wrong, the validator is wrong. Update the chapter first.

This is the chapter-first-then-code discipline restated as code.

---

## Extending the layer

When you add a new `action_kind`:

1. Define it in `models.py::ProposedAction.action_kind` (extend the
   `Literal[...]`).
2. Add the rule(s) to `validator.py::check_action`, named `VR-*` for
   blocking and `VW-*` for warning.
3. Add tests in `tests/test_codex_validator_*.py`.
4. Add the rule to the table in this chapter.
5. Add the hook at the proposing call site.
6. If the new rule embodies a decision worth its own ADR, write it.

When you flip an intent-only guardrail to enforced (e.g. when G6 ships):

1. Update `04_guardrails.md` — strip the "intent-only" language from
   the status block.
2. Rename `VW-G6` → `VR-G6` in `validator.py` and switch from warning
   to blocking.
3. Update this chapter's rule table.
4. Write an ADR in `08_decisions_log.md` documenting the flip.

---

## The meta-recursive bit

This Validation Layer was itself a Codex decision (ADR-011). Writing
that ADR was the first action gated by the discipline the layer
implements. It is fitting that the Codex's enforcement was born from
its own enforcement.

If you ever find yourself questioning whether to add a check to this
layer, the question is not "is this worth a check?" The question is
"is this worth an ADR?" If yes, add both.
